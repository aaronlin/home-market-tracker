from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import tempfile
from typing import Any

from models import RegionalHistory, RegionalMonth


SCHEMA_VERSION = 1


def _time(value: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise ValueError('invalid_timestamp')
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def _number(value: Any, *, integer: bool = False, positive: bool = False) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        finite = math.isfinite(value)
    except OverflowError:
        return None
    if not finite or value < 0 or positive and value == 0 or integer and int(value) != value:
        return None
    return int(value) if integer else float(value)


def parse_regional(payload: Any, zip_code: str, fetched_at: str, as_of: datetime,
                   *, from_cache: bool = False, max_age_days: int = 45) -> RegionalHistory:
    if not re.fullmatch(r'\d{5}', zip_code):
        raise ValueError('invalid_zip')
    if not isinstance(payload, dict) or str(payload.get('zipCode', payload.get('id', ''))) != zip_code:
        raise ValueError('regional_zip_mismatch')
    fetched = _time(fetched_at)
    now = as_of.astimezone(timezone.utc)
    if fetched > now:
        raise ValueError('future_capture')
    sale = payload.get('saleData')
    if not isinstance(sale, dict) or not isinstance(sale.get('history'), dict):
        raise ValueError('regional_history_unavailable')
    updated_raw = sale.get('lastUpdatedDate')
    if updated_raw is not None and not isinstance(updated_raw, str):
        raise ValueError('invalid_source_date')
    updated = _time(updated_raw) if updated_raw else None
    if updated and updated > now:
        raise ValueError('future_source_date')
    complete_before = min(fetched.strftime('%Y-%m'), now.strftime('%Y-%m'),
                          updated.strftime('%Y-%m') if updated else fetched.strftime('%Y-%m'))
    months = []
    seen = set()
    for month, raw_month in sorted(sale['history'].items()):
        if not isinstance(month, str) or not re.fullmatch(r'\d{4}-(?:0[1-9]|1[0-2])', month):
            raise ValueError('invalid_history_month')
        if month > fetched.strftime('%Y-%m') or not isinstance(raw_month, dict):
            raise ValueError('invalid_history_period')
        raw_date = raw_month.get('date')
        if raw_date is not None and (_time(raw_date).strftime('%Y-%m') != month or _time(raw_date).day != 1):
            raise ValueError('history_date_mismatch')
        groups = raw_month.get('dataByPropertyType', [])
        if not isinstance(groups, list):
            raise ValueError('invalid_property_groups')
        for raw in groups:
            if not isinstance(raw, dict):
                raise ValueError('invalid_property_group')
            kind = raw.get('propertyType')
            if kind not in {'Single Family', 'Townhouse'}:
                continue
            key = (month, kind)
            if key in seen:
                raise ValueError('duplicate_property_month')
            seen.add(key)
            months.append(RegionalMonth(month, kind,
                median_asking_price=_number(raw.get('medianPrice'), positive=True),
                median_dom=_number(raw.get('medianDaysOnMarket')),
                new_listings=_number(raw.get('newListings'), integer=True),
                total_listings=_number(raw.get('totalListings'), integer=True),
                complete=month < complete_before))
    source_clock = updated or fetched
    stale = (now - source_clock).days > max_age_days or (now - fetched).days > max_age_days
    return RegionalHistory(zip_code, fetched_at, updated_raw, months,
                           'stale' if stale else 'ok', 'source_outdated' if stale else None, from_cache)


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    name = None
    try:
        with tempfile.NamedTemporaryFile('w', encoding='utf-8', dir=path.parent, delete=False) as handle:
            name = handle.name
            json.dump(value, handle, ensure_ascii=False, sort_keys=True, allow_nan=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
    finally:
        if name and os.path.exists(name):
            os.unlink(name)


@contextmanager
def _cache_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def load_regional(provider: Any, zip_code: str | None, cache_root: Path, as_of: datetime,
                  *, history_months: int = 36, max_age_days: int = 45) -> RegionalHistory:
    as_of = as_of.astimezone(timezone.utc)
    stamp = as_of.isoformat()
    if not zip_code or not re.fullmatch(r'\d{5}', zip_code):
        return RegionalHistory(zip_code or '', stamp, issue='subject_zip_unavailable')
    fetch = getattr(provider, 'fetch_market_history', None)
    if not callable(fetch):
        return RegionalHistory(zip_code, stamp, issue='regional_provider_unavailable')
    previous = None
    try:
        with _cache_lock(cache_root / zip_code / '.lock'):
            path = cache_root / zip_code / f'latest-{history_months}.json'
            try:
                envelope = json.loads(path.read_text(encoding='utf-8'))
                if (not isinstance(envelope, dict) or type(envelope.get('schema_version')) is not int
                        or envelope.get('schema_version') != SCHEMA_VERSION
                        or envelope.get('zip_code') != zip_code or envelope.get('history_months') != history_months or envelope.get('data_type') != 'Sale'):
                    raise ValueError('invalid_cache_identity')
                checksum = hashlib.sha256(json.dumps(envelope['payload'], sort_keys=True, allow_nan=False).encode()).hexdigest()
                if checksum != envelope.get('payload_sha256'):
                    raise ValueError('cache_checksum_mismatch')
                previous = parse_regional(envelope['payload'], zip_code, envelope['fetched_at'], as_of,
                                          from_cache=True, max_age_days=max_age_days)
                if _time(previous.fetched_at).strftime('%Y-%m') == as_of.strftime('%Y-%m'):
                    return previous
            except (OSError, ValueError, KeyError, TypeError):
                previous = None
            try:
                payload = fetch(zip_code, history_months)
                result = parse_regional(payload, zip_code, stamp, as_of, max_age_days=max_age_days)
            except Exception:
                if previous:
                    return replace(previous, status='stale', issue='regional_refresh_failed')
                return RegionalHistory(zip_code, stamp, issue='regional_fetch_failed')
            envelope = {'schema_version': SCHEMA_VERSION, 'zip_code': zip_code,
                        'history_months': history_months, 'data_type': 'Sale', 'fetched_at': stamp,
                        'payload': payload,
                        'payload_sha256': hashlib.sha256(json.dumps(payload, sort_keys=True, allow_nan=False).encode()).hexdigest()}
            digest = hashlib.sha256(json.dumps(envelope, sort_keys=True, allow_nan=False).encode()).hexdigest()
            _atomic_json(path.parent / 'responses' / f'{as_of:%Y%m%dT%H%M%S}-{digest[:16]}.json', envelope)
            _atomic_json(path, envelope)
            return result
    except (OSError, ValueError, TypeError):
        return RegionalHistory(zip_code, stamp, issue='regional_cache_unavailable')
