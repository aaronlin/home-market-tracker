from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import tempfile
from typing import Any

from models import CaptureBaseline, HomeRun
from redfin import address_key


SCHEMA_VERSION = 1
SOURCE_POLICY_VERSION = 1


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                    ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def cohort_fingerprint(home_run: HomeRun) -> str:
    spec = asdict(home_run.home)
    for key in ('name', 'purchase_price', 'display_count'):
        spec.pop(key, None)
    return _digest({'schema_version': SCHEMA_VERSION, 'source_policy_version': SOURCE_POLICY_VERSION,
                    'cohort': spec})


def _reference_fingerprint(home_run: HomeRun) -> str:
    claims = {}
    for comp in home_run.candidates or home_run.comps:
        reference = comp.redfin_reference
        if reference:
            claims[address_key(comp.address)] = {key: reference.raw.get(key) for key in
                ('values', 'recorded_sale', 'listing_observations', 'sale_history', 'ownership',
                 'field_evidence', 'source_group', 'source')}
    return _digest(claims)


def _date(value: str | None):
    try:
        return datetime.fromisoformat(str(value).replace('Z', '+00:00')).date()
    except (TypeError, ValueError):
        return None


def _observations(home_run: HomeRun, as_of: datetime) -> tuple[list[dict], dict[str, int]]:
    types = home_run.home.comparable_property_types or [home_run.home.property_type]
    qualifying = {address_key(comp.address) for comp in home_run.ranked_comps
                  if comp.market_property_type in types
                  and not (comp.raw.get('_sale_record') or {}).get('_identity_conflict')
                  and not comp.raw.get('_listing_identity_conflict')}
    counts = {kind: set() for kind in types}
    rows = {}
    for comp in home_run.candidates or home_run.comps:
        key = address_key(comp.address)
        qualifies = key in qualifying
        observed = _date(comp.last_seen_date)
        if (qualifies and comp.status == 'Active' and comp.listing_price and comp.listing_price > 0
                and observed and 0 <= (as_of.date() - observed).days <= 7):
            counts[comp.market_property_type].add(key)
        listing = comp.raw.get('_listing_record') or {}
        mls = listing.get('mlsNumber') or comp.raw.get('mlsNumber')
        if not mls:
            mls = next((item.mls_number for item in comp.listing_observations if item.mls_number), None)
        row = {'property_key': key, 'original_comp_id': comp.id, 'qualifies': qualifies,
               'static_claims': {'property_type': comp.market_property_type, 'beds': comp.beds,
                                 'baths': comp.baths, 'sqft': comp.sqft, 'lot_size': comp.lot_size,
                                 'sources': {field: comp.field_sources.get(field) for field in
                                             ('property_type', 'beds', 'baths', 'sqft', 'lot_size')}},
               'status': comp.status, 'asking_price': comp.listing_price,
               'listed_date': comp.listed_date, 'last_seen_date': comp.last_seen_date,
               'mls_number': mls,
               'episode_key': _digest([key, mls, comp.listed_date]),
               'episode_identity_complete': bool(mls and comp.listed_date),
               'recorded_sale': asdict(comp.recorded_sale) if comp.recorded_sale else None,
               'listing_observations': [asdict(item) for item in comp.listing_observations]}
        rows[key] = row
    return [rows[key] for key in sorted(rows)], {kind: len(keys) for kind, keys in counts.items()}


def _complete(home_run: HomeRun) -> bool:
    types = home_run.home.comparable_property_types or [home_run.home.property_type]
    expected = {(f'listings_{status}', kind) for status in ('active', 'inactive') for kind in types}
    expected.add(('properties', None))
    actual = {(entry.source, entry.filters.get('propertyType')) for entry in home_run.retrieval}
    return bool(home_run.retrieval) and expected <= actual and all(entry.complete for entry in home_run.retrieval)


def _valid_record(value: Any, home_id: str, types: list[str]) -> bool:
    if (not isinstance(value, dict) or type(value.get('schema_version')) is not int
            or value.get('schema_version') != SCHEMA_VERSION
            or type(value.get('source_policy_version')) is not int
            or value.get('source_policy_version') != SOURCE_POLICY_VERSION
            or value.get('home_id') != home_id or value.get('complete') is not True
            or not isinstance(value.get('captured_at'), str)
            or _date(value['captured_at']) is None):
        return False
    counts, observations = value.get('active_counts'), value.get('observations')
    if (not isinstance(counts, dict) or set(counts) != set(types)
            or not isinstance(observations, list)
            or any(type(number) is not int or not 0 <= number <= len(observations)
                   for number in counts.values())):
        return False
    keys = []
    for item in observations:
        if (not isinstance(item, dict) or not isinstance(item.get('property_key'), str)
                or not item['property_key'] or not isinstance(item.get('static_claims'), dict)):
            return False
        keys.append(item['property_key'])
    return len(keys) == len(set(keys))


def _atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    name = None
    try:
        with tempfile.NamedTemporaryFile('w', encoding='utf-8', dir=path.parent, delete=False) as handle:
            name = handle.name
            json.dump(value, handle, sort_keys=True, ensure_ascii=False, allow_nan=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
    finally:
        if name and os.path.exists(name):
            os.unlink(name)


def _capture_baseline(home_run: HomeRun, as_of: datetime, formal_root: Path,
                     *, persist: bool = False, research_root: Path | None = None) -> CaptureBaseline:
    as_of = as_of.astimezone(timezone.utc)
    fingerprint = cohort_fingerprint(home_run)
    rows, counts = _observations(home_run, as_of)
    stamp = as_of.astimezone(timezone.utc).isoformat()
    complete = _complete(home_run)
    home_key = hashlib.sha256(home_run.home.id.encode()).hexdigest()[:24]
    directory = formal_root / home_key
    body = {'schema_version': SCHEMA_VERSION, 'source_policy_version': SOURCE_POLICY_VERSION,
            'home_id': home_run.home.id, 'fingerprint': fingerprint, 'captured_at': stamp,
            'complete': complete, 'reference_fingerprint': _reference_fingerprint(home_run),
            'retrieval': [asdict(entry) for entry in home_run.retrieval],
            'active_counts': counts, 'observations': rows}
    capture_id = _digest(body)
    target_root = formal_root if persist else research_root
    baseline = CaptureBaseline(fingerprint, 'partial' if not complete else 'first_capture',
                               current_active_counts=counts,
                               issue='retrieval_incomplete' if not complete else 'no_comparable_baseline')
    try:
        lock_root = target_root or research_root
        if lock_root is None:
            return baseline
        lock_path = lock_root / home_key / '.lock'
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            prior = []
            saw_other_cohort = False
            for path in directory.glob('captures/*.json'):
                try:
                    item = json.loads(path.read_text(encoding='utf-8'))
                    types = home_run.home.comparable_property_types or [home_run.home.property_type]
                    if (not _valid_record(item, home_run.home.id, types)
                            or path.stem != _digest(item)
                            or _date(item['captured_at']) >= as_of.date()):
                        continue
                    if item.get('fingerprint') != fingerprint:
                        saw_other_cohort = True
                        continue
                    prior.append(item)
                except (OSError, ValueError, TypeError, KeyError):
                    continue
            if complete and prior:
                previous = max(prior, key=lambda item: item['captured_at'])
                before = {item['property_key']: item['static_claims'] for item in previous['observations']}
                after = {item['property_key']: item['static_claims'] for item in rows}
                corrected = (previous.get('reference_fingerprint') != body['reference_fingerprint']
                             or any(before[key] != after[key] for key in before.keys() & after.keys()))
                baseline = CaptureBaseline(fingerprint, 'changed_cohort' if corrected else 'comparable',
                    previous['captured_at'], previous['active_counts'], counts,
                    'source_claims_changed' if corrected else None)
            elif complete and saw_other_cohort:
                baseline = CaptureBaseline(fingerprint, 'changed_cohort', current_active_counts=counts,
                                           issue='cohort_changed')
            if target_root:
                _atomic_json(target_root / home_key / 'captures' / f'{capture_id}.json', body)
            return baseline
    except (OSError, ValueError, TypeError, KeyError):
        return CaptureBaseline(fingerprint, 'partial', current_active_counts=counts,
                               issue='capture_store_unavailable')


def capture_baseline(home_run: HomeRun, as_of: datetime, formal_root: Path,
                     *, persist: bool = False, research_root: Path | None = None) -> CaptureBaseline:
    try:
        return _capture_baseline(home_run, as_of, formal_root, persist=persist, research_root=research_root)
    except (OSError, ValueError, TypeError, KeyError, OverflowError):
        return CaptureBaseline('', 'partial', issue='capture_store_unavailable')
