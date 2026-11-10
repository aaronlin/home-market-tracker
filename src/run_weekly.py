#!/usr/bin/env python3
from __future__ import annotations

import argparse
from dataclasses import asdict, replace
import sys
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

from app_config import SetupError, load_dotenv, load_settings, resolve_repo_path
from deliver import send_email
from models import HomeConfig, HomeRun, RetrievalInfo, TrackerRun, canonical_property_type
from normalize import extract_value_estimate, normalize_comps, normalize_candidates, qualify_comps, qualification_reasons, normalize_nearby_listings
from provider import PropertyDataProvider, RentCastProvider, write_raw_snapshot
from render import render_digest
from state import load_state, save_state
from redfin import load_redfin_references, apply_redfin
from regional import load_regional
from capture_history import capture_baseline


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Track comparable home sales near configured homes.")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--preview", action="store_true", help="Fetch comps and write preview files without sending email or updating state.")
    mode.add_argument("--send", action="store_true", help="Fetch comps, send the weekly email, and update state after a successful send.")
    args = parser.parse_args(argv)

    try:
        load_dotenv()
        settings = load_settings()
        provider = RentCastProvider(str((settings.get("rentcast") or {}).get("base_url", "https://api.rentcast.io/v1")))
        if args.preview:
            return run_preview(settings, provider)
        if args.send:
            return run_send(settings, provider)
        raise SetupError("No command selected.")
    except SetupError as exc:
        print(f"Setup needed: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"Home market tracker failed: {exc}", file=sys.stderr)
        return 1


def run_preview(settings: Dict[str, Any], provider: PropertyDataProvider) -> int:
    run = build_run(settings, provider)
    previous = load_state(_state_path(settings))
    digest = render_digest(run, previous)
    preview_root = _preview_root(settings)
    preview_root.mkdir(parents=True, exist_ok=True)
    stem = _safe_stem(run.generated_at)
    html_path = preview_root / f"{stem}.html"
    text_path = preview_root / f"{stem}.txt"
    html_path.write_text(digest["html_body"], encoding="utf-8")
    text_path.write_text(digest["text_body"], encoding="utf-8")
    print(f"Preview subject: {_subject(settings, digest['subject'])}")
    print(f"HTML: {html_path}")
    print(f"Text: {text_path}")
    return 0


def run_send(settings: Dict[str, Any], provider: PropertyDataProvider) -> int:
    run = build_run(settings, provider, persist_history=True)
    previous = load_state(_state_path(settings))
    digest = render_digest(run, previous)
    send_result = send_email(_subject(settings, digest["subject"]), digest["html_body"], digest["text_body"], dry_run=False)
    if send_result:
        raise SetupError(send_result)
    save_state(_state_path(settings), run, previous)
    print(f"Sent: {_subject(settings, digest['subject'])}")
    return 0


def build_run(settings: Dict[str, Any], provider: PropertyDataProvider, *, persist_history: bool = False) -> TrackerRun:
    home_runs: List[HomeRun] = []
    generated = TrackerRun.now([]).generated_at
    tracker = settings.get("tracker") or {}
    market_options = _market_options(tracker)
    references = load_redfin_references(resolve_repo_path(tracker.get("redfin_reference_path", "data/references/redfin.json")))
    for home in _home_configs(settings):
        payload = provider.fetch_home_market(home)
        write_raw_snapshot(_raw_snapshot_root(settings), home.id, payload, generated)
        candidates = apply_redfin(normalize_candidates(home, payload), references)
        comps = qualify_comps(home, candidates)
        nearby_listings = qualify_comps(home, apply_redfin(normalize_nearby_listings(home, payload), references))
        selected_ids = {comp.id for comp in comps}
        evidence = {"schema_version": 1, "generated_at": generated, "home_id": home.id,
                    "purpose": "Source comparison evidence; third-party claims are not verified ground truth.",
                    "rentcast_payload": payload,
                    "candidates": [{"passes_local_filters": comp.id in selected_ids,
                                    "exclusion_reasons": qualification_reasons(home, comp),
                                    "source_differences": _source_differences(comp),
                                    "effective": asdict(comp)} for comp in candidates]}
        write_raw_snapshot(resolve_repo_path(tracker.get("evidence_root", "data/evidence")), home.id, evidence, generated)
        avm_payload = payload.get("avm") if isinstance(payload.get("avm"), dict) else payload
        subject_property = avm_payload.get("subjectProperty") if isinstance(avm_payload.get("subjectProperty"), dict) else {}
        home_runs.append(
            HomeRun(
                home=home,
                comps=comps,
                value_estimate=extract_value_estimate(payload),
                subject_property=subject_property,
                nearby_listings=nearby_listings,
                retrieval=[RetrievalInfo(**entry) for entry in payload.get("_retrieval", [])],
                candidates=candidates,
            )
        )
    if market_options['enabled']:
        as_of = datetime.fromisoformat(generated)
        base = _raw_snapshot_root(settings).parent
        formal_captures = resolve_repo_path(market_options.get('capture_root', base / 'history' / 'cohorts'))
        research = _preview_root(settings) / 'research'
        cache = (resolve_repo_path(market_options.get('cache_root', base / 'cache' / 'markets'))
                 if persist_history else research / 'markets')
        enriched = []
        for home_run in home_runs:
            regional = load_regional(provider, _regional_zip(home_run), cache, as_of,
                                     history_months=market_options['history_months'],
                                     max_age_days=market_options['max_age_days'])
            baseline = capture_baseline(home_run, as_of, formal_captures, persist=persist_history,
                                        research_root=research / 'cohorts')
            enriched.append(replace(home_run, regional_history=regional, capture_baseline=baseline))
        home_runs = enriched
    return TrackerRun(homes=home_runs, generated_at=generated)


def _market_options(tracker: Dict[str, Any]) -> Dict[str, Any]:
    raw = tracker.get('market_history', {})
    if not isinstance(raw, dict):
        raise SetupError('tracker.market_history must be a mapping.')
    options = {'enabled': True, 'history_months': 36, 'max_age_days': 45, **raw}
    if not isinstance(options['enabled'], bool):
        raise SetupError('market_history.enabled must be boolean.')
    for key, maximum in (('history_months', 120), ('max_age_days', 365)):
        value = options[key]
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
            raise SetupError(f'market_history.{key} must be an integer between 1 and {maximum}.')
    for key in ('capture_root', 'cache_root'):
        if key in options and (not isinstance(options[key], str) or not options[key].strip()):
            raise SetupError(f'market_history.{key} must be a nonempty path.')
    return options


def _market_zip_override(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not re.fullmatch(r'\d{5}', value):
        raise SetupError('market_zip_code must contain exactly five digits.')
    return value


def _regional_zip(home_run: HomeRun) -> str | None:
    if home_run.home.market_zip_code:
        return home_run.home.market_zip_code
    raw = str(home_run.subject_property.get('zipCode') or '')
    match = re.fullmatch(r'(\d{5})(?:-\d{4})?', raw)
    if match:
        return match[1]
    match = re.search(r'\b[A-Za-z]{2}\s+(\d{5})(?:-\d{4})?$', home_run.home.address)
    return match[1] if match else None


def _source_differences(comp) -> Dict[str, Any]:
    original = comp.source_snapshots.get("rentcast", {})
    effective = asdict(comp)
    changes = {}
    for field, source in comp.field_sources.items():
        if field == "property_type":
            before, after = original.get("source_property_types", {}), comp.source_property_types
        else:
            before, after = original.get(field), effective.get(field)
        if before != after:
            changes[field] = {"rentcast": before, "effective": after, "selected_source": source}
    return changes


def _home_configs(settings: Dict[str, Any]) -> List[HomeConfig]:
    tracker = settings.get("tracker") or {}
    defaults = tracker.get("comp_defaults") or {}
    comp_annotations = _comp_annotations(tracker.get("comp_annotations") or {})
    homes = tracker.get("homes") or []
    if not homes:
        raise SetupError("No tracker.homes configured.")
    result = []
    for raw_home in homes:
        property_type = str(raw_home.get("property_type") or defaults.get("property_type") or "Single Family")
        result.append(
            HomeConfig(
                id=str(raw_home["id"]),
                name=str(raw_home["name"]),
                address=str(raw_home["address"]),
                property_type=property_type,
                comparable_property_types=_comparable_property_types(raw_home, property_type),
                market_zip_code=_market_zip_override(raw_home.get("market_zip_code")),
                max_radius_miles=float(raw_home.get("max_radius_miles") or defaults.get("max_radius_miles") or 1.5),
                days_old=int(raw_home.get("days_old") or defaults.get("days_old") or 180),
                comp_count=int(raw_home.get("comp_count") or defaults.get("comp_count") or 10),
                purchase_price=_optional_int(raw_home.get("purchase_price")),
                bedrooms=_optional_str(raw_home.get("bedrooms")),
                bathrooms=_optional_str(raw_home.get("bathrooms")),
                square_footage=_optional_str(raw_home.get("square_footage")),
                lot_size=_optional_str(raw_home.get("lot_size")),
                year_built=_optional_str(raw_home.get("year_built")),
                comp_square_footage=_optional_str(raw_home.get("comp_square_footage")),
                comp_lot_size=_optional_str(raw_home.get("comp_lot_size")),
                listing_bedrooms=_optional_str(raw_home.get("listing_bedrooms")),
                listing_bathrooms=_optional_str(raw_home.get("listing_bathrooms")),
                display_count=_optional_int(raw_home.get("display_count")),
                close_listing_radius_miles=_optional_float(raw_home.get("close_listing_radius_miles")),
                ranked_comp_bedrooms=_optional_str(raw_home.get("ranked_comp_bedrooms")),
                ranked_comp_bathrooms=_optional_str(raw_home.get("ranked_comp_bathrooms")),
                ranked_comp_square_footage=_optional_str(raw_home.get("ranked_comp_square_footage")),
                ranked_comp_lot_size=_optional_str(raw_home.get("ranked_comp_lot_size")),
                comp_annotations=comp_annotations,
                same_hoa_addresses=[str(value) for value in raw_home.get("same_hoa_addresses") or []],
                same_hoa_patterns=[str(value) for value in raw_home.get("same_hoa_patterns") or []],
            )
        )
    return result


def _comparable_property_types(raw_home: Dict[str, Any], property_type: str) -> List[str]:
    if "comparable_property_types" not in raw_home:
        return []
    values = raw_home["comparable_property_types"]
    if (not isinstance(values, list) or not values
            or any(not isinstance(value, str) or value not in {"Single Family", "Townhouse"} for value in values)):
        raise SetupError("comparable_property_types must be a nonempty list of Single Family or Townhouse.")
    subject_type = canonical_property_type(property_type)
    if subject_type not in {"Single Family", "Townhouse"}:
        raise SetupError("comparable_property_types requires a Single Family or Townhouse subject.")
    return list(dict.fromkeys([subject_type, *values]))


def _comp_annotations(raw_annotations: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    normalized = {}
    for address, value in raw_annotations.items():
        if isinstance(value, dict):
            normalized[" ".join(str(address).lower().replace(",", " ").split())] = value
    return normalized


def _optional_str(value) -> str | None:
    if value in {None, ""}:
        return None
    return str(value)


def _optional_int(value) -> int | None:
    if value in {None, ""}:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _optional_float(value) -> float | None:
    if value in {None, ""}:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _state_path(settings: Dict[str, Any]) -> Path:
    return resolve_repo_path((settings.get("tracker") or {}).get("state_path", "data/state/home_market.yaml"))


def _preview_root(settings: Dict[str, Any]) -> Path:
    return resolve_repo_path((settings.get("tracker") or {}).get("preview_root", "data/previews"))


def _raw_snapshot_root(settings: Dict[str, Any]) -> Path:
    return resolve_repo_path((settings.get("tracker") or {}).get("raw_snapshot_root", "data/raw"))


def _subject(settings: Dict[str, Any], subject: str) -> str:
    prefix = str((settings.get("tracker") or {}).get("subject_prefix", "[房市週報]")).strip()
    return f"{prefix} {subject}".strip()


def _safe_stem(value: str) -> str:
    return value.replace(":", "").replace("+", "Z")


if __name__ == "__main__":
    raise SystemExit(main())
