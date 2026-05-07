#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Dict, List

from app_config import SetupError, load_dotenv, load_settings, resolve_repo_path
from deliver import send_email
from models import HomeConfig, HomeRun, TrackerRun
from normalize import extract_value_estimate, normalize_comps
from provider import PropertyDataProvider, RentCastProvider, write_raw_snapshot
from render import render_digest
from state import load_state, save_state


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
    run = build_run(settings, provider)
    previous = load_state(_state_path(settings))
    digest = render_digest(run, previous)
    send_result = send_email(_subject(settings, digest["subject"]), digest["html_body"], digest["text_body"], dry_run=False)
    if send_result:
        raise SetupError(send_result)
    save_state(_state_path(settings), run, previous)
    print(f"Sent: {_subject(settings, digest['subject'])}")
    return 0


def build_run(settings: Dict[str, Any], provider: PropertyDataProvider) -> TrackerRun:
    home_runs: List[HomeRun] = []
    generated = TrackerRun.now([]).generated_at
    for home in _home_configs(settings):
        payload = provider.fetch_home_market(home)
        write_raw_snapshot(_raw_snapshot_root(settings), home.id, payload, generated)
        comps = normalize_comps(home, payload)
        avm_payload = payload.get("avm") if isinstance(payload.get("avm"), dict) else payload
        subject_property = avm_payload.get("subjectProperty") if isinstance(avm_payload.get("subjectProperty"), dict) else {}
        home_runs.append(
            HomeRun(
                home=home,
                comps=comps,
                value_estimate=extract_value_estimate(payload),
                subject_property=subject_property,
            )
        )
    return TrackerRun(homes=home_runs, generated_at=generated)


def _home_configs(settings: Dict[str, Any]) -> List[HomeConfig]:
    tracker = settings.get("tracker") or {}
    defaults = tracker.get("comp_defaults") or {}
    comp_annotations = _comp_annotations(tracker.get("comp_annotations") or {})
    homes = tracker.get("homes") or []
    if not homes:
        raise SetupError("No tracker.homes configured.")
    result = []
    for raw_home in homes:
        result.append(
            HomeConfig(
                id=str(raw_home["id"]),
                name=str(raw_home["name"]),
                address=str(raw_home["address"]),
                property_type=str(raw_home.get("property_type") or defaults.get("property_type") or "Single Family"),
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
                display_count=_optional_int(raw_home.get("display_count")),
                comp_annotations=comp_annotations,
                same_hoa_addresses=[str(value) for value in raw_home.get("same_hoa_addresses") or []],
                same_hoa_patterns=[str(value) for value in raw_home.get("same_hoa_patterns") or []],
            )
        )
    return result


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


def _state_path(settings: Dict[str, Any]) -> Path:
    return resolve_repo_path((settings.get("tracker") or {}).get("state_path", "data/state/home_market.yaml"))


def _preview_root(settings: Dict[str, Any]) -> Path:
    return resolve_repo_path((settings.get("tracker") or {}).get("preview_root", "data/previews"))


def _raw_snapshot_root(settings: Dict[str, Any]) -> Path:
    return resolve_repo_path((settings.get("tracker") or {}).get("raw_snapshot_root", "data/raw"))


def _subject(settings: Dict[str, Any], subject: str) -> str:
    prefix = str((settings.get("tracker") or {}).get("subject_prefix", "[Home Market]")).strip()
    return f"{prefix} {subject}".strip()


def _safe_stem(value: str) -> str:
    return value.replace(":", "").replace("+", "Z")


if __name__ == "__main__":
    raise SystemExit(main())
