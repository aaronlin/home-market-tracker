from __future__ import annotations

import html
from typing import Dict, List, Optional

from models import ComparableSale, HomeRun, TrackerRun
from normalize import new_comps, percent_change
from state import TrackerState


def render_digest(run: TrackerRun, previous: TrackerState) -> Dict[str, str]:
    subject = f"Home market tracker: {len(run.homes)} homes, {sum(_new_count(h, previous) for h in run.homes)} new rows"
    text_sections = [f"Generated: {run.generated_at}", ""]
    html_sections = [
        "<html><body>",
        f"<p><strong>Generated:</strong> {html.escape(run.generated_at)}</p>",
    ]
    for home_run in run.homes:
        text_sections.append(_render_home_text(home_run, previous))
        html_sections.append(_render_home_html(home_run, previous))
    html_sections.append("</body></html>")
    return {"subject": subject, "text_body": "\n\n".join(text_sections), "html_body": "\n".join(html_sections)}


def _render_home_text(home_run: HomeRun, previous: TrackerState) -> str:
    prev_home = previous.homes.get(home_run.home.id)
    selected = home_run.selected_comps
    fresh = new_comps(selected, prev_home.seen_comp_ids if prev_home else [])
    sale_change = percent_change(home_run.median_sale_price, prev_home.median_sale_price if prev_home else None)
    ppsf_change = percent_change(home_run.median_price_per_sqft, prev_home.median_price_per_sqft if prev_home else None)
    lines = [
        f"{home_run.home.name} - {home_run.home.address}",
        f"Configured filter: {_configured_filter(home_run)}",
        f"Subject property: {_subject_summary(home_run)}",
        f"Purchase price reference: {_money(home_run.home.purchase_price)}",
        f"Ranking: {_ranking_explanation(home_run)}",
        f"Candidate rows: {len(home_run.comps)}",
        f"Selected rows: {len(selected)}",
        f"Final comp set: {len(home_run.final_comps)} rows",
        f"New rows since last sent report: {len(fresh)}",
        f"Median final comp price: {_money(home_run.median_sale_price)} ({_pct(sale_change)} vs previous successful run)",
        f"Median final comp $/sqft: {_money(home_run.median_price_per_sqft)} ({_pct(ppsf_change)} vs previous successful run)",
    ]
    if not prev_home:
        lines.append("No prior sent report exists yet; all selected rows are currently treated as new.")
    lines.append("")
    if prev_home:
        lines.append("New since last sent report:")
        lines.extend(_text_new_rows(fresh, home_run) if fresh else ["  None"])
        lines.append("")
    lines.append("")
    lines.append("Selected comparable market rows:")
    lines.extend(_text_rows(selected) if selected else ["  None returned"])
    return "\n".join(lines)


def _render_home_html(home_run: HomeRun, previous: TrackerState) -> str:
    prev_home = previous.homes.get(home_run.home.id)
    selected = home_run.selected_comps
    fresh = new_comps(selected, prev_home.seen_comp_ids if prev_home else [])
    sale_change = percent_change(home_run.median_sale_price, prev_home.median_sale_price if prev_home else None)
    ppsf_change = percent_change(home_run.median_price_per_sqft, prev_home.median_price_per_sqft if prev_home else None)
    parts = [
        f"<h2>{html.escape(home_run.home.name)}</h2>",
        f"<p>{html.escape(home_run.home.address)}</p>",
        "<ul>",
        f"<li>Configured filter: {html.escape(_configured_filter(home_run))}</li>",
        f"<li>Subject property: {html.escape(_subject_summary(home_run))}</li>",
        f"<li>Purchase price reference: {_money(home_run.home.purchase_price)}</li>",
        f"<li>Ranking: {html.escape(_ranking_explanation(home_run))}</li>",
        f"<li>Candidate rows: {len(home_run.comps)}</li>",
        f"<li>Selected rows: {len(selected)}</li>",
        f"<li>Final comp set: {len(home_run.final_comps)} rows</li>",
        f"<li>New rows since last sent report: {len(fresh)}</li>",
        f"<li>Median final comp price: {_money(home_run.median_sale_price)} ({_pct(sale_change)} vs previous successful run)</li>",
        f"<li>Median final comp $/sqft: {_money(home_run.median_price_per_sqft)} ({_pct(ppsf_change)} vs previous successful run)</li>",
        "</ul>",
    ]
    if not prev_home:
        parts.append("<p><em>No prior sent report exists yet; all selected rows are currently treated as new.</em></p>")
    else:
        parts.append("<h3>New since last sent report</h3>")
        parts.append(_html_new_rows(fresh, home_run))
    parts.append("<h3>Selected comparable market rows</h3>")
    parts.append(_html_table(selected))
    return "\n".join(parts)


def _text_new_rows(comps: List[ComparableSale], home_run: HomeRun) -> List[str]:
    final_ids = {comp.id for comp in home_run.final_comps}
    rows = []
    for comp in comps:
        recommendation = "Add to final comp set" if comp.id in final_ids else "Monitor only"
        rows.append(f"  {comp.address} | {recommendation} | internal score {_num(comp.rank_score)} | market {_money(comp.sale_price)} | last seen {_plain(comp.last_seen_date)}")
    return rows


def _html_new_rows(comps: List[ComparableSale], home_run: HomeRun) -> str:
    if not comps:
        return "<p>None</p>"
    final_ids = {comp.id for comp in home_run.final_comps}
    rows = ["<ul>"]
    for comp in comps:
        recommendation = "Add to final comp set" if comp.id in final_ids else "Monitor only"
        rows.append(
            "<li>"
            f"{html.escape(comp.address)} - {html.escape(recommendation)}"
            f" (internal score {html.escape(_num(comp.rank_score))}, market {html.escape(_money(comp.sale_price))}, last seen {html.escape(_plain(comp.last_seen_date))})"
            "</li>"
        )
    rows.append("</ul>")
    return "".join(rows)


def _text_rows(comps: List[ComparableSale]) -> List[str]:
    rows = []
    include_hoa = _include_hoa_columns(comps)
    for comp in comps:
        extras = []
        if include_hoa:
            extras.append(f"HOA {_money(comp.hoa_fee)}")
            extras.append(f"same HOA {_yes_no(comp.hoa_match)}")
        if comp.notes:
            extras.append(f"notes {'; '.join(comp.notes)}")
        rows.append(
            "  "
            f"{comp.address} | internal score {_num(comp.rank_score)} | status {_plain(comp.status)} | dist {_num(comp.distance_miles)} mi | "
            f"{_beds_baths(comp)} | sqft {_plain(comp.sqft)} | lot {_plain(comp.lot_size)} | "
            f"listed {_money(comp.listing_price)} | sold {_money(comp.sold_price)} | market {_money(comp.sale_price)} | "
            f"$/sqft {_money(comp.price_per_sqft)} | DOM {_plain(comp.days_on_market)} | "
            f"listed date {_plain(comp.listed_date)} | removed {_plain(comp.removed_date)} | last seen {_plain(comp.last_seen_date)}"
            + (f" | {' | '.join(extras)}" if extras else "")
        )
    return rows


def _html_table(comps: List[ComparableSale]) -> str:
    if not comps:
        return "<p>None</p>"
    include_hoa = _include_hoa_columns(comps)
    include_notes = any(comp.notes for comp in comps)
    headers = ["Address", "Internal Score", "Status", "Dist", "Beds/Baths", "Sqft", "Lot", "Listed Price", "Sold Price", "Market Price", "$/sqft", "DOM", "Listed Date", "Removed Date", "Last Seen"]
    if include_hoa:
        headers.extend(["HOA", "HOA Match"])
    if include_notes:
        headers.append("Notes")
    rows = ["<table border=\"1\" cellpadding=\"4\" cellspacing=\"0\">", "<thead><tr>"]
    rows.extend(f"<th>{header}</th>" for header in headers)
    rows.append("</tr></thead><tbody>")
    for comp in comps:
        values = [
            comp.address,
            _num(comp.rank_score),
            _plain(comp.status),
            f"{_num(comp.distance_miles)} mi",
            _beds_baths(comp),
            _plain(comp.sqft),
            _plain(comp.lot_size),
            _money(comp.listing_price),
            _money(comp.sold_price),
            _money(comp.sale_price),
            _money(comp.price_per_sqft),
            _plain(comp.days_on_market),
            _plain(comp.listed_date),
            _plain(comp.removed_date),
            _plain(comp.last_seen_date),
        ]
        if include_hoa:
            values.extend([_money(comp.hoa_fee), _yes_no(comp.hoa_match)])
        if include_notes:
            values.append("; ".join(comp.notes))
        rows.append("<tr>" + "".join(f"<td>{html.escape(value)}</td>" for value in values) + "</tr>")
    rows.append("</tbody></table>")
    return "".join(rows)


def _new_count(home_run: HomeRun, previous: TrackerState) -> int:
    prev_home = previous.homes.get(home_run.home.id)
    return len(new_comps(home_run.selected_comps, prev_home.seen_comp_ids if prev_home else []))


def _money(value: Optional[float]) -> str:
    if value is None:
        return "n/a"
    return f"${value:,.0f}"


def _pct(value: Optional[float]) -> str:
    if value is None:
        return "n/a"
    return f"{value:+.1f}%"


def _ratio(value: Optional[float]) -> str:
    if value is None:
        return "n/a"
    return f"{value * 100:.1f}%"


def _num(value: Optional[float]) -> str:
    if value is None:
        return "n/a"
    return f"{value:.1f}"


def _plain(value) -> str:
    return "n/a" if value is None else str(value)


def _beds_baths(comp: ComparableSale) -> str:
    return f"{_plain(comp.beds)}/{_plain(comp.baths)}"


def _yes_no(value: bool) -> str:
    return "yes" if value else "no"


def _include_hoa_columns(comps: List[ComparableSale]) -> bool:
    return any(comp.hoa_fee is not None or comp.hoa_match for comp in comps)


def _ranking_explanation(home_run: HomeRun) -> str:
    if home_run.home.property_type.lower() == "townhouse":
        return "prioritizes townhouse type, same-HOA/cluster, sqft fit, distance, and recency"
    return "prioritizes property type, sqft/lot fit, distance, recency, and annotations"


def _configured_filter(home_run: HomeRun) -> str:
    home = home_run.home
    parts = [
        home.property_type,
        f"{home.max_radius_miles:g} mi",
        f"{home.days_old} days",
        f"{home.comp_count} comps",
    ]
    if home.bedrooms:
        parts.append(f"beds {home.bedrooms}")
    if home.bathrooms:
        parts.append(f"baths {home.bathrooms}")
    if home.square_footage:
        parts.append(f"sqft {home.square_footage}")
    if home.lot_size:
        parts.append(f"lot {home.lot_size}")
    if home.year_built:
        parts.append(f"built {home.year_built}")
    if home.comp_square_footage:
        parts.append(f"comp sqft {home.comp_square_footage}")
    if home.comp_lot_size:
        parts.append(f"comp lot {home.comp_lot_size}")
    if home.display_count:
        parts.append(f"display {home.display_count}")
    return ", ".join(parts)


def _subject_summary(home_run: HomeRun) -> str:
    subject = home_run.subject_property or {}
    property_type = subject.get("propertyType") or home_run.home.property_type
    beds = subject.get("bedrooms")
    baths = subject.get("bathrooms")
    sqft = subject.get("squareFootage")
    lot = subject.get("lotSize")
    built = subject.get("yearBuilt")
    return f"{property_type}, {_plain(beds)}/{_plain(baths)}, sqft {_plain(sqft)}, lot {_plain(lot)}, built {_plain(built)}"
