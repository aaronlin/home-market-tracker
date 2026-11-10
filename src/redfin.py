"""Validated local Redfin evidence; loading and source selection never fetch pages."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, replace
from datetime import date
import json
import math
from pathlib import Path
import re
from typing import Any, Dict, Iterable
from urllib.parse import urlsplit

from app_config import SetupError
from models import ComparableSale, ListingObservation, RecordedSale, RedfinReference
from normalize import _redfin_url


_NUMERIC = {
    'beds': (False, True), 'baths': (False, True), 'sqft': (True, False),
    'lot_size': (True, True), 'year_built': (True, False), 'hoa_fee': (True, True),
    'listing_price': (True, False), 'days_on_market': (True, True),
}
_VALUE_KEYS = set(_NUMERIC) | {'property_type', 'status', 'observed_at', 'listed_date'}
_GROUPS = {'mls_listing', 'mls_history', 'public_record', 'estimate', 'unknown'}


def address_key(value: str) -> str:
    """Normalize punctuation and explicit unit markers, never remove a unit or ZIP."""
    value = re.sub(r'\b(?:apt|apartment|unit|suite)\b\.?\s*|#\s*', ' unit ', value.lower())
    value = re.sub(r'\bmc\s+([a-z]+)\b', r'mc\1', value)
    words = re.sub(r'[.,]', ' ', value).split()
    aliases = {'north': 'n', 'south': 's', 'east': 'e', 'west': 'w',
               'street': 'st', 'avenue': 'ave', 'lane': 'ln', 'road': 'rd',
               'drive': 'dr', 'court': 'ct', 'boulevard': 'blvd',
               'place': 'pl', 'terrace': 'ter', 'circle': 'cir'}
    return ' '.join(word if index and words[index - 1] == 'unit' else aliases.get(word, word)
                    for index, word in enumerate(words))


def load_redfin_references(path: str | Path) -> Dict[str, RedfinReference]:
    path = Path(path)
    if not path.exists():
        return {}
    try:
        content = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(content, dict) or content.get('version') != 1 or isinstance(content.get('version'), bool):
            raise ValueError('expected version 1 reference index')
        records = content.get('records')
        if not isinstance(records, list):
            raise ValueError('records must be a list')
        result: Dict[str, RedfinReference] = {}
        ids: Dict[str, str] = {}
        for index, record in enumerate(records):
            try:
                reference = _reference(record)
                key = address_key(reference.address)
                property_id = reference.url.rsplit('/', 1)[-1]
                if key in result:
                    raise ValueError('duplicate address')
                if property_id in ids and ids[property_id] != key:
                    raise ValueError('one property ID maps to different addresses')
                result[key] = reference
                ids[property_id] = key
            except (ValueError, TypeError, KeyError) as exc:
                raise ValueError(f'record {index + 1}: {exc}') from exc
        return result
    except (OSError, UnicodeError, ValueError, TypeError) as exc:
        raise SetupError(f'Invalid Redfin reference index {path}: {exc}') from exc


def _date(value: Any, label: str, latest: date | None = None) -> str:
    if not isinstance(value, str):
        raise ValueError(f'{label} must be YYYY-MM-DD')
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f'{label} must be YYYY-MM-DD') from exc
    if parsed.isoformat() != value or parsed > (latest or date.today()):
        raise ValueError(f'{label} is invalid or future-dated')
    return value


def _number(value: Any, label: str, integer: bool, allow_zero: bool) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or isinstance(value, float) and not math.isfinite(value):
        raise ValueError(f'{label} must be a finite number')
    if value < 0 or not allow_zero and value == 0 or integer and value != int(value):
        raise ValueError(f'{label} is outside its valid range')
    return int(value) if integer else value


def _text(record: dict, key: str) -> str:
    value = record.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f'{key} must be nonempty text')
    return value.strip()


def _field_evidence(record: dict, latest: date) -> dict:
    evidence = record.get('field_evidence', {})
    if not isinstance(evidence, dict) or set(evidence) - (_VALUE_KEYS | {'recorded_sale', 'ownership'}):
        raise ValueError('field_evidence contains unsupported fields')
    for field, entry in evidence.items():
        if not isinstance(entry, dict) or entry.get('source_group', record.get('source_group', 'unknown')) not in _GROUPS:
            raise ValueError(f'{field} evidence requires a recognized source_group')
        for key in ('observed_at', 'event_date'):
            if entry.get(key) is not None:
                _date(entry[key], f'{field} evidence {key}', latest)
        for key in ('source_label', 'unit', 'billing_period', 'metric', 'mls_number'):
            if entry.get(key) is not None and (not isinstance(entry[key], str) or not entry[key].strip()):
                raise ValueError(f'{field} evidence {key} must be text')
    return deepcopy(evidence)


def _history_observations(record: dict, sale: RecordedSale | None, latest: date) -> list[ListingObservation]:
    """Pair structured MLS history, never prices extracted from free-form facts."""
    history = record.get('sale_history', [])
    if not isinstance(history, list):
        raise ValueError('sale_history must be a list')
    events = []
    kinds = {'listed', 'price_changed', 'pending', 'contingent', 'sold', 'delisted', 'cancelled', 'rental_listed', 'rented'}
    for raw in history:
        if not isinstance(raw, dict) or raw.get('event') not in kinds:
            raise ValueError('sale_history requires a recognized event')
        event = dict(raw)
        event['source_group'] = raw.get('source_group', 'mls_history')
        if event['source_group'] not in _GROUPS:
            raise ValueError('history source_group is unsupported')
        event['date'] = _date(raw.get('date'), 'history date', latest)
        if raw.get('event') in {'rental_listed', 'rented'}:
            continue
        event['mls_number'] = _text(raw, 'mls_number')
        if raw['event'] in {'listed', 'price_changed', 'sold'}:
            event['price'] = (_number(raw['price'], 'history price', True, False)
                              if raw.get('price') is not None else None)
        if event not in events:
            events.append(event)
    # Redfin may repeat one dated MLS event with its price masked in one row.
    # Coalesce only when the same source, MLS, date and event also supplies a price.
    priced_keys = {(e['event'], e['date'], e['mls_number'], e['source_group'])
                   for e in events if e.get('price') is not None}
    events = [e for e in events if not ('price' in e and e['price'] is None
              and (e['event'], e['date'], e['mls_number'], e['source_group']) in priced_keys)]
    sale_group = (record.get('recorded_sale') or {}).get('source_group',
        record.get('field_evidence', {}).get('recorded_sale', {}).get('source_group', record.get('source_group', 'unknown')))
    if not sale or record.get('source') != 'page' or sale_group != 'mls_history':
        return []
    closes = [e for e in events if e['event'] == 'sold' and e['date'] == sale.transaction_date
              and e['price'] == sale.price]
    if len(closes) != 1:
        return []
    close = closes[0]
    sale_mls = (record.get('recorded_sale') or {}).get('mls_number')
    if sale_mls and sale_mls != close['mls_number']:
        return []
    episode = [e for e in events if e['mls_number'] == close['mls_number'] and e['date'] <= close['date']]
    starts = [e for e in episode if e['event'] == 'listed']
    if len(starts) != 1:
        return []
    start = starts[0]['date']
    if any(e['event'] in {'delisted', 'cancelled'} or e['date'] < start
           or e['source_group'] not in {'mls_listing', 'mls_history'} for e in episode):
        return []
    if any(e['mls_number'] != close['mls_number'] and start <= e['date'] <= close['date'] for e in events):
        return []
    if sum(e['event'] == 'sold' for e in episode) != 1:
        return []
    asks = [e for e in episode if e['event'] in {'listed', 'price_changed'}]
    if any(e['price'] is None for e in asks):
        return []
    return [ListingObservation(e['price'], e['date'], start, close['date'], close['mls_number'], None, 'redfin')
            for e in asks]


def _reference(record: Any) -> RedfinReference:
    if not isinstance(record, dict):
        raise ValueError('reference must be an object')
    address, matched = _text(record, 'address'), _text(record, 'matched_address')
    if not re.search(r'\b[A-Za-z]{2}\s+\d{5}(?:-\d{4})?$', address):
        raise ValueError('address must include state and ZIP')
    if address_key(address) != address_key(matched):
        raise ValueError('address does not match source address including unit and ZIP')
    url = _redfin_url(record.get('url'))
    if url is None:
        raise ValueError('url must be a safe canonical Redfin property URL')
    components = address.rsplit(',', 2)
    segments = urlsplit(url).path.strip('/').split('/')
    if len(components) != 3 or len(segments) < 5:
        raise ValueError('URL must identify the matched street, city, state and ZIP')
    street, city, region = (part.strip() for part in components)
    state, zipcode = region.split()
    compact = lambda value: re.sub(r'[^a-z0-9]', '', value.lower())
    expected_street = re.sub(r'\bunit\s+[a-z0-9-]+\b', '', address_key(street)).strip()
    slug = segments[2]
    if (segments[0].upper() != state.upper() or compact(segments[1]) != compact(city)
            or not slug.endswith('-' + zipcode)
            or address_key(slug[:-len(zipcode)-1].replace('-', ' ')) != expected_street):
        raise ValueError('URL street, city, state or ZIP does not match source address')
    address_units = re.findall(r'\bunit\s+([a-z0-9-]+)\b', address_key(address))
    path_units = re.findall(r'/unit-([a-zA-Z0-9-]+)/', urlsplit(url).path)
    if len(address_units) > 1 or len(path_units) > 1 or address_units != [unit.lower() for unit in path_units]:
        raise ValueError('URL unit does not match address unit')
    checked = _date(record.get('checked_at'), 'checked_at')
    latest = date.fromisoformat(checked)
    source = record.get('source')
    if source not in {'page', 'search_index'}:
        raise ValueError('source must be page or search_index')
    source_group = record.get('source_group', 'unknown')
    if source_group not in _GROUPS:
        raise ValueError('source_group is unsupported')
    field_evidence = _field_evidence(record, latest)
    ownership = record.get('ownership', 'unknown')
    if ownership not in {'fee_simple', 'condominium', 'unknown'}:
        raise ValueError('ownership is unsupported')
    facts = record.get('facts', [])
    if not isinstance(facts, list) or len(facts) > 6 or any(not isinstance(fact, str) or not fact.strip() or len(fact) > 500 for fact in facts):
        raise ValueError('facts must contain at most six short nonempty strings')
    raw_values = record.get('values', {})
    if not isinstance(raw_values, dict) or set(raw_values) - _VALUE_KEYS:
        raise ValueError('values contains unsupported fields')
    values = {}
    for key, value in raw_values.items():
        if value is None:
            continue
        if key == 'days_on_market':
            try:
                values[key] = _number(value, key, *_NUMERIC[key])
            except ValueError:
                pass  # Keep unknown or invalid source day counts in raw evidence only.
        elif key in _NUMERIC:
            values[key] = _number(value, key, *_NUMERIC[key])
        elif key in {'observed_at', 'listed_date'}:
            values[key] = _date(value, key, latest)
        elif key == 'status':
            if value not in {'Active', 'Inactive', 'Pending', 'Sold'}:
                raise ValueError('status must be Active, Inactive, Pending, or Sold')
            values[key] = value
        else:
            if not isinstance(value, str) or value not in {'Single Family', 'Townhouse', 'Townhome', 'Condo', 'Apartment', 'Multi Family', 'Land'}:
                raise ValueError('property_type is unsupported')
            values[key] = 'Townhouse' if value == 'Townhome' else value
    if values.get('listed_date') and values.get('observed_at') and values['listed_date'] > values['observed_at']:
        raise ValueError('listed_date must not follow observed_at')
    sale = None
    if record.get('recorded_sale') is not None:
        raw_sale = record['recorded_sale']
        if not isinstance(raw_sale, dict):
            raise ValueError('recorded_sale must pair price and transaction_date')
        if raw_sale.get('source_group', field_evidence.get('recorded_sale', {}).get('source_group', source_group)) not in _GROUPS:
            raise ValueError('sale source_group is unsupported')
        sale = RecordedSale(_number(raw_sale.get('price'), 'sale price', True, False),
                            _date(raw_sale.get('transaction_date'), 'transaction_date', latest), 'redfin')
    observations = []
    raw_observations = record.get('listing_observations', [])
    if not isinstance(raw_observations, list):
        raise ValueError('listing_observations must be a list')
    for raw in raw_observations:
        if not isinstance(raw, dict):
            raise ValueError('listing observation must be an object')
        start = _date(raw.get('listed_date'), 'observation listed_date', latest)
        observed = _date(raw.get('observed_date'), 'observation observed_date', latest)
        removed = _date(raw.get('removed_date'), 'observation removed_date', latest)
        if not start <= observed <= removed:
            raise ValueError('listing observation dates must be chronological')
        mls = _text(raw, 'mls_number')
        if raw.get('source_group', source_group) not in _GROUPS:
            raise ValueError('listing observation source_group is unsupported')
        dom = raw.get('days_on_market') if raw.get('metric') in {'listing_dom', 'cumulative_dom'} else None
        observations.append(ListingObservation(
            _number(raw.get('price'), 'observation price', True, False), observed, start, removed,
            mls, _number(dom, 'observation DOM', True, True) if dom is not None else None, 'redfin'))
    observations.extend(_history_observations(record, sale, latest))
    return RedfinReference(address, matched, url, checked, source, list(facts), values, sale, observations,
                           deepcopy(record), source_group, field_evidence, ownership)


def _group(reference: RedfinReference, field: str) -> str:
    if field == 'recorded_sale':
        return (reference.raw.get('recorded_sale') or {}).get('source_group',
            reference.field_evidence.get(field, {}).get('source_group', reference.source_group))
    return reference.field_evidence.get(field, {}).get('source_group', reference.source_group)


def _claim_date(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value[:10]).isoformat()
    except ValueError:
        return None


def _original_comp(comp: ComparableSale) -> ComparableSale:
    if 'rentcast' not in comp.source_snapshots:
        return comp
    original = deepcopy(comp.source_snapshots['rentcast'])
    if original.get('recorded_sale'):
        original['recorded_sale'] = RecordedSale(**original['recorded_sale'])
    original['listing_observations'] = [ListingObservation(**item) for item in original.get('listing_observations', [])]
    return ComparableSale(**original)


def _observations(reference: RedfinReference) -> list[ListingObservation]:
    explicit_count = len(reference.raw.get('listing_observations', []))
    explicit = [observation for observation, raw in zip(reference.listing_observations[:explicit_count],
                                                       reference.raw.get('listing_observations', []))
                if raw.get('source_group', reference.source_group) in {'mls_listing', 'mls_history'}]
    return explicit + reference.listing_observations[explicit_count:]


def apply_redfin(comps: Iterable[ComparableSale], references: Dict[str, RedfinReference]) -> list[ComparableSale]:
    result = []
    for current in comps:
        reference = references.get(address_key(current.address))
        if reference is None or current.raw.get('_listing_identity_conflict'):
            result.append(current)
            continue
        comp = _original_comp(current)
        original = asdict(comp)
        for key in ('redfin_reference', 'field_sources', 'source_snapshots'):
            original.pop(key, None)
        snapshots = {'rentcast': original, 'redfin': deepcopy(reference.raw)}
        sources = {field: 'rentcast' for field in ('beds', 'baths', 'sqft', 'lot_size', 'year_built',
                   'hoa_fee', 'listing_price', 'status', 'days_on_market', 'listed_date', 'removed_date',
                   'last_seen_date', 'recorded_sale', 'sold_price', 'property_type', 'listing_observations')}
        changes: dict[str, Any] = {'redfin_url': reference.url, 'redfin_reference': reference,
                                   'source_snapshots': snapshots}
        sources['redfin_url'] = 'redfin'
        unresolved = set(comp.unresolved_fields)
        notes = list(comp.reconciliation_notes)

        def withhold_listing(reason: str) -> None:
            unresolved.update(('status', 'listing_price', 'days_on_market'))
            for field in ('status', 'listing_price', 'days_on_market', 'listed_date', 'last_seen_date', 'removed_date'):
                changes[field] = None
                sources[field] = 'unresolved'
            changes['listing_observations'] = []
            sources['listing_observations'] = 'unresolved'
            notes.append(reason)

        def withhold_sale(reason: str) -> None:
            unresolved.update(('recorded_sale', 'sale_price', 'sale_date'))
            for field in ('recorded_sale', 'sold_price', 'market_price', 'price_per_sqft'):
                changes[field] = None
                sources[field] = 'unresolved'
            changes['listing_observations'] = []
            sources['listing_observations'] = 'unresolved'
            notes.append(reason)

        if reference.source == 'page':
            values = reference.values
            for field in ('beds', 'baths', 'sqft', 'lot_size', 'year_built'):
                if field in values and _group(reference, field) == 'mls_listing':
                    changes[field] = values[field]
                    sources[field] = 'redfin'
            if ('hoa_fee' in values and _group(reference, 'hoa_fee') == 'mls_listing'
                    and reference.field_evidence.get('hoa_fee', {}).get('billing_period') == 'monthly'
                    and reference.field_evidence.get('hoa_fee', {}).get('unit') in {None, 'USD', 'USD/month'}):
                changes['hoa_fee'] = values['hoa_fee']
                sources['hoa_fee'] = 'redfin'
            if 'property_type' in values:
                group = _group(reference, 'property_type')
                label = 'redfin' if group == 'mls_listing' else 'redfin_' + group
                changes['source_property_types'] = {**comp.source_property_types, label: values['property_type']}
                if group == 'mls_listing':
                    sources['property_type'] = 'redfin'

            rf_sale = reference.recorded_sale if _group(reference, 'recorded_sale') == 'mls_history' else None
            rf_observed = values.get('observed_at')
            rc_observed = _claim_date(comp.last_seen_date)
            rc_sale = comp.recorded_sale
            rc_sale_date = _claim_date(rc_sale.transaction_date) if rc_sale else None
            has_listing = (all(field in values for field in ('status', 'observed_at'))
                           and all(_group(reference, field) == 'mls_listing'
                                   for field in ('status', 'observed_at')))
            rf_mls = reference.field_evidence.get('status', {}).get('mls_number')
            latest_rc_observations = [observation for observation in comp.listing_observations
                                      if _claim_date(observation.observed_date) == rc_observed]
            rc_mls = {observation.mls_number for observation in latest_rc_observations if observation.mls_number}
            same_episode = (bool(rf_mls) and bool(latest_rc_observations) and rc_mls == {rf_mls}
                            and comp.status == values.get('status')
                            and comp.listing_price is not None and comp.listing_price > 0
                            and values.get('listing_price') == comp.listing_price
                            and _group(reference, 'listing_price') == 'mls_listing'
                            and all(observation.mls_number == rf_mls and observation.price == comp.listing_price
                                    for observation in latest_rc_observations)
                            and bool(comp.listed_date) and bool(values.get('listed_date'))
                            and _claim_date(comp.listed_date) == values['listed_date']
                            and all(_claim_date(observation.listed_date) == values['listed_date']
                                    for observation in latest_rc_observations))
            if has_listing:
                if (rc_observed and rc_observed > rf_observed and not same_episode) or (rc_sale_date and rc_sale_date > rf_observed):
                    withhold_listing('RentCast has a later listing or sale claim than the saved Redfin MLS observation; current listing metrics are unresolved.')
                elif rc_observed == rf_observed and (comp.status and comp.status != values['status']
                        or rf_mls and rc_mls and rc_mls != {rf_mls}):
                    withhold_listing('Same-date source status or MLS episode claims disagree; current listing metrics are unresolved.')
                else:
                    if rc_observed and rc_observed > rf_observed:
                        notes.append('Later RentCast observation matches the same MLS episode, status, and price; selected Redfin at its actual older observation date, with both observations retained.')
                    for field in ('listing_price', 'status', 'listed_date'):
                        supplied = field in values and _group(reference, field) == 'mls_listing'
                        changes[field] = values[field] if supplied else None
                        sources[field] = 'redfin' if supplied else 'unavailable'
                    changes['days_on_market'] = comp.days_on_market if same_episode else None
                    sources['days_on_market'] = 'rentcast' if same_episode else 'unavailable'
                    dom_evidence = reference.field_evidence.get('days_on_market', {})
                    if ('days_on_market' in values and _group(reference, 'days_on_market') == 'mls_listing'
                            and dom_evidence.get('metric') in {'listing_dom', 'cumulative_dom'}
                            and dom_evidence.get('observed_at') == rf_observed):
                        changes['days_on_market'] = values['days_on_market']
                        sources['days_on_market'] = 'redfin'
                    changes['removed_date'] = None
                    sources['removed_date'] = 'redfin'
                    changes['last_seen_date'] = rf_observed
                    sources['last_seen_date'] = 'redfin'
                    changes['listing_observations'] = _observations(reference)
                    sources['listing_observations'] = 'redfin'

            if rf_sale is not None:
                rc_sale_mls = (comp.raw.get('_sale_record') or {}).get('mlsNumber')
                rf_sale_mls = (reference.raw.get('recorded_sale') or {}).get('mls_number')
                if rc_sale and rc_sale_date == rf_sale.transaction_date and rc_sale_mls and rf_sale_mls and rc_sale_mls != rf_sale_mls:
                    withhold_sale('Same-date transaction MLS identifiers disagree; episode identity and transaction metrics are unresolved.')
                elif rc_sale_date and rc_sale_date > rf_sale.transaction_date:
                    withhold_sale('RentCast reports a later transaction than the saved Redfin MLS sale; transaction metrics are unresolved and both events are retained.')
                else:
                    changes['recorded_sale'] = rf_sale
                    changes['sold_price'] = rf_sale.price
                    changes['listing_observations'] = _observations(reference) if 'status' not in unresolved else []
                    sources.update(recorded_sale='redfin', sold_price='redfin', listing_observations='redfin')
                    if rc_sale_date and rc_sale_date != rf_sale.transaction_date:
                        notes.append('The sources describe different dated transactions; Redfin MLS supplies the latest selected historical sale, with both events retained.')
                    elif rc_sale and rc_sale.price != rf_sale.price:
                        notes.append('Same-date compatible sale prices disagree; selected Redfin MLS history under the owner source policy.')
            elif rc_sale and has_listing and rc_sale.transaction_date > rf_observed:
                withhold_sale('RentCast reports a transaction after the saved Redfin MLS listing observation; that later transaction remains unconfirmed.')

            # A newer RentCast status after a Redfin historical close is a distinct, unconfirmed episode.
            if (rf_sale and rc_observed and rc_observed >= rf_sale.transaction_date and comp.status
                    and comp.status not in {'Sold', 'Inactive'} and not has_listing):
                withhold_listing('RentCast reports a later or same-day listing episode after the Redfin historical sale; current status remains unresolved.')
                if 'recorded_sale' not in unresolved:
                    changes['listing_observations'] = _observations(reference)
                    sources['listing_observations'] = 'redfin'

        if 'recorded_sale' not in unresolved and ('recorded_sale' in changes or 'sqft' in changes):
            sale = changes.get('recorded_sale', comp.recorded_sale)
            sqft = changes.get('sqft', comp.sqft)
            changes['price_per_sqft'] = sale.price / sqft if sale and sqft else None
            sources['price_per_sqft'] = 'derived_selected_sources'
        changes['field_sources'] = sources
        changes['unresolved_fields'] = sorted(unresolved)
        changes['reconciliation_notes'] = list(dict.fromkeys(notes))
        result.append(replace(comp, **changes))
    return result
