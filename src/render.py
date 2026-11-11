from __future__ import annotations

import html
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from typing import Dict, List
from statistics import median
from zoneinfo import ZoneInfo
from urllib.parse import urlencode

from market_metrics import asking_gap, cohort_for, source_dom
from heat import assess_regional

from models import ComparableSale, HomeRun, TrackerRun
from state import TrackerState


@dataclass(frozen=True)
class HomeEvidence:
    home_run: HomeRun
    sales: List[ComparableSale]
    active: List[ComparableSale]
    window_days: int
    stale_active: int
    unknown_type: int
    evidence_type: str | None = None
    groups: List[HomeEvidence] = field(default_factory=list)


def _date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value[:10]).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _evidence(home_run: HomeRun, as_of: datetime, evidence_type: str | None = None) -> HomeEvidence:
    types = home_run.home.comparable_property_types or [home_run.home.property_type]
    if evidence_type is None and len(types) > 1:
        groups = [_evidence(home_run, as_of, kind) for kind in types]
        return HomeEvidence(home_run, [row for group in groups for row in group.sales],
                            [row for group in groups for row in group.active],
                            max(group.window_days for group in groups),
                            sum(group.stale_active for group in groups), groups[0].unknown_type,
                            groups=groups)
    if evidence_type is None and types[0] != home_run.home.property_type:
        evidence_type = types[0]
    filter_home = replace(home_run.home, property_type=evidence_type) if evidence_type else home_run.home
    day = as_of.replace(hour=0, minute=0, second=0, microsecond=0)
    pool = [comp for comp in home_run.ranked_comps
            if not (comp.raw.get("_sale_record") or {}).get("_identity_conflict")
            and not comp.raw.get("_listing_identity_conflict")]
    primary = [comp for comp in pool if cohort_for(filter_home, comp) == "primary"]
    cap = min(home_run.home.days_old if home_run.home.days_old > 0 else 365, 365)

    def sales_in(rows, window):
        eligible = [comp for comp in rows if comp.recorded_sale and comp.recorded_sale.price > 0
                    and _date(comp.recorded_sale.transaction_date)
                    and day - timedelta(days=window) <= _date(comp.recorded_sale.transaction_date) <= day]
        return sorted(eligible, key=lambda comp: (comp.recorded_sale.transaction_date, comp.rank_score or 0), reverse=True)

    def active_in(rows):
        active, stale = [], 0
        for comp in rows:
            if (comp.status or "").lower() != "active" or not comp.listing_price or comp.listing_price <= 0:
                continue
            observed = _date(comp.last_seen_date)
            if observed and 0 <= (day - observed).days <= 7:
                active.append(comp)
            else:
                stale += 1
        active.sort(key=lambda comp: (comp.last_seen_date or "", comp.rank_score or 0), reverse=True)
        return active, stale

    for window in sorted({min(90, cap), min(180, cap), cap}):
        sales = sales_in(primary, window)
        if len(sales) >= 3:
            break
    active, stale = active_in(primary)
    return HomeEvidence(home_run, sales, active, window, stale,
                        sum(cohort_for(home_run.home, comp) == "unknown" for comp in (home_run.candidates or pool) if not comp.raw.get("_is_subject")), evidence_type)


def _type_label(view: HomeEvidence) -> str:
    return "SFH" if view.evidence_type == "Single Family" else view.evidence_type or "同房型"


def _summary(view: HomeEvidence) -> str:
    if view.groups:
        return "不同房型分開參考，不合併推算房價。" + " ".join(
            _type_label(group) + "：" + _summary(group) for group in view.groups)
    if not view.sales:
        return ("缺少此房型可用成交紀錄，目前無法據此判斷房屋價值或市場冷熱。" if view.evidence_type else
                "缺少同房型可用成交紀錄，目前無法據此判斷房屋價值或市場冷熱。")
    if len(view.sales) < 3:
        return "成交樣本偏少，適合逐筆參考，不宜推論房價走勢。"
    return "可逐筆比較成交與掛牌證據；樣本不代表完整市場或這間房的估值。"


def _money(value: int | float | None) -> str:
    return f"${value:,.0f}" if value is not None else "未提供"


def _number(value: int | float | None) -> str:
    return f"{value:g}" if value is not None else "未提供"


def _attributes(comp: ComparableSale) -> str:
    parts = [f"{_number(comp.beds)} 房／{_number(comp.baths)} 衛", f"{comp.sqft:,} sqft" if comp.sqft else "室內面積未提供"]
    if comp.distance_miles is not None:
        parts.append(f"距離 {comp.distance_miles:.2f} 英里")
    if comp.hoa_fee is not None:
        parts.append(f"來源管理費 {_money(comp.hoa_fee)}／月")
    if comp.hoa_match:
        parts.append("符合已設定的同社區提示")
    return " · ".join(parts)


def _classifications(comp: ComparableSale) -> str:
    values = comp.source_property_types
    if not values:
        return "來源房型未提供"
    if len(set(values.values())) == 1:
        return "來源房型：" + next(iter(values.values()))
    names = {"redfin": "Redfin", "property": "房屋紀錄", "listing": "掛牌", "avm": "AVM 候選"}
    return "來源房型：" + "；".join(f"{names.get(key, key)} {value}" for key, value in values.items())


def _redfin_link(comp: ComparableSale) -> tuple[str, str]:
    if comp.redfin_reference:
        return comp.redfin_reference.url, f"Redfin 房源頁：{comp.address}（地址已核對）"
    if comp.redfin_url:
        return comp.redfin_url, f"Redfin 房源頁：{comp.address}（來源提供，請核對地址）"
    query = urlencode({"q": f'site:redfin.com "{comp.address}"'})
    return "https://www.google.com/search?" + query, f"用 Google 搜尋 Redfin：{comp.address}"


def _details(comp: ComparableSale, kind: str, as_of: datetime) -> List[str]:
    if kind == "sale":
        sale = comp.recorded_sale
        lines = [f"成交價 {_money(sale.price)} · 成交日期 {sale.transaction_date}", _attributes(comp), ("來源：Redfin 成交紀錄" if sale.source == "redfin" else "來源：RentCast 房屋成交紀錄"), _classifications(comp)]
        if comp.sqft and comp.sqft > 0:
            lines.append(f"成交單價 {_money(sale.price / comp.sqft)}／sqft")
        gap = asking_gap(comp)
        if gap:
            sign = "+" if gap.amount >= 0 else "-"
            lines.extend([f"最後已知售前開價參考 {_money(gap.observation.price)}（開價紀錄日期 {gap.observation.observed_date}，來源 {gap.observation.source}）",
                          f"成交對開價參考落差 {sign}${abs(gap.amount):,.0f}（{gap.percent:+.1f}%）；非已確認最終／原始開價比。"])
            if gap.observation.later_price_unknown:
                lines.append("其後有改價紀錄但金額未公開；以上以最後已知價格計算，非最終開價。")
        else:
            lines.append("開價落差：無可核對的售前掛牌參考，不計算。")
        timeline = comp.listing_timeline
        if timeline:
            start, pending, sold = map(_date, (timeline.listed_date, timeline.pending_date, timeline.sold_date))
            if pending:
                lines.append(f"上市至首次 Pending：{(pending - start).days} 天（{timeline.listed_date} → {timeline.pending_date}；依來源事件日期計算，來源 {timeline.source}）。")
            else:
                lines.append("上市至首次 Pending：未提供可核對的 Pending 日期。")
            lines.append(f"上市至成交：{(sold - start).days} 天（{timeline.listed_date} → {timeline.sold_date}；含簽約後交割期間，來源 {timeline.source}）。")
        else:
            lines.extend(["上市至首次 Pending：未提供可核對的掛牌時間線。",
                          "上市至成交：未提供可核對的掛牌時間線。"])
        dom = source_dom(gap.observation.days_on_market) if gap else None
        if dom is not None:
            lines.append(f"對應掛牌參考的來源 DOM：{dom} 天；非到成交所需天數。")
        elif timeline:
            lines.append("來源 DOM：未提供；上述日期差不是來源 DOM。")
        else:
            lines.append("來源 DOM：未提供；不以掛牌到成交日期推算。")
    else:
        observed = _date(comp.last_seen_date)
        age = (as_of.date() - observed.date()).days
        lines = [f"開價 {_money(comp.listing_price)} · 最後觀察 {observed.date().isoformat()}（{age} 天前）", _attributes(comp), _classifications(comp)]
        price_source = "Redfin MLS" if comp.field_sources.get("listing_price") == "redfin" else "RentCast 掛牌"
        date_source = "Redfin MLS" if comp.field_sources.get("last_seen_date") == "redfin" else "RentCast 掛牌"
        lines.append(f"開價來源：{price_source}；觀察日期來源：{date_source}。")
        dom = source_dom(comp.days_on_market)
        dom_label = "來源 DOM"
        if comp.field_sources.get("days_on_market", "rentcast") == "rentcast":
            lines.append("DOM 來源：RentCast 掛牌。")
        if comp.field_sources.get("days_on_market") == "redfin" and comp.redfin_reference:
            metric = comp.redfin_reference.field_evidence.get("days_on_market", {}).get("metric")
            dom_label = "Redfin 累計 DOM" if metric == "cumulative_dom" else "Redfin 掛牌週期 DOM"
        dom_date = (comp.source_snapshots.get("rentcast", {}).get("last_seen_date") if comp.field_sources.get("days_on_market") == "rentcast" else comp.last_seen_date) or comp.last_seen_date
        lines.append(f"{dom_label}：{dom} 天（觀察日期 {str(dom_date)[:10]}；非成交速度）。" if dom is not None else "來源 DOM：未提供有效數值。")
    lines.extend(_reference_details(comp))
    if comp.notes:
        lines.append("備註：" + "；".join(comp.notes))
    if comp.exclude_from_median:
        lines.append("備註：已設定不納入統計；本期僅逐筆列示。")
    return lines


def _reference_details(comp: ComparableSale) -> List[str]:
    reference = comp.redfin_reference
    if reference is None:
        return []
    method = "公開頁面" if reference.source == "page" else "搜尋索引（僅導覽）"
    lines = [f"Redfin {method}快照，查核 {reference.checked_at}；查核日不代表資料更新日。"]
    labels = {"property_type": "房型", "status": "掛牌狀態", "last_seen_date": "掛牌觀察日期",
              "beds": "房數", "baths": "衛浴", "sqft": "室內 sqft", "lot_size": "土地 sqft",
              "year_built": "年份", "hoa_fee": "月管理費", "listing_price": "開價", "days_on_market": "來源 DOM"}
    original = comp.source_snapshots.get("rentcast", {})
    differences, adopted = [], []
    for field, label in labels.items():
        if comp.field_sources.get(field) != "redfin":
            continue
        before = original.get(field)
        after = getattr(comp, field, None)
        if field == "property_type":
            before = next((original.get("source_property_types", {}).get(key) for key in ("property", "listing", "avm")
                           if original.get("source_property_types", {}).get(key)), None)
            after = comp.source_property_types.get("redfin")
        if after is not None:
            adopted.append(label)
        if before != after:
            differences.append(f"{label} RentCast {before if before is not None else '未提供'} → Redfin {after if after is not None else '未提供'}")
    original_sale = original.get("recorded_sale")
    if comp.field_sources.get("recorded_sale") == "redfin" and comp.recorded_sale:
        current = comp.recorded_sale
        if not original_sale or (original_sale["price"], original_sale["transaction_date"]) != (current.price, current.transaction_date):
            before = f"{_money(original_sale['price'])}／{original_sale['transaction_date']}" if original_sale else "未提供"
            differences.insert(0, f"成交 RentCast {before} → Redfin {_money(current.price)}／{current.transaction_date}")
    if adopted:
        lines.append("採用 Redfin：" + "、".join(adopted) + "；其他欄位依各自來源。")
    if differences:
        lines.append("主要差異：" + "；".join(differences[:2]) + ("；完整差異已保存在本機。" if len(differences) > 2 else "。"))
    static_dates = {details.get("observed_at") for key, details in reference.field_evidence.items()
                    if key in {"beds", "baths", "sqft", "property_type", "lot_size", "year_built", "hoa_fee"} and details.get("observed_at")}
    if any(comp.field_sources.get(field) == "redfin" for field in ("beds", "baths", "sqft", "property_type")):
        lines.append("Redfin 屬性來源日期：" + ("、".join(sorted(static_dates)) if static_dates else "未提供") + "。")
    translations = {
        "RentCast has a later": "RentCast 的掛牌或成交主張較新，與保存的 Redfin 事件尚未核對；目前掛牌指標暫不採用。",
        "Same-date source status": "同日的狀態或 MLS 週期互有衝突，目前掛牌指標暫不採用。",
        "Same-date transaction MLS": "同日成交的 MLS 識別不同，事件身分尚未核對，暫不計成交指標。",
        "RentCast reports a later transaction": "RentCast 有較新的成交主張，Redfin 尚未對應確認；保留兩邊紀錄，暫不計成交指標。",
        "RentCast reports a transaction after": "RentCast 的成交主張晚於保存的 Redfin 掛牌觀察，尚未確認該次成交。",
        "RentCast reports a later or same-day": "歷史成交後又有 RentCast 掛牌主張，新的掛牌週期尚未核對。",
        "Later RentCast observation matches": "同次 MLS 掛牌的狀態與開價一致，採 Redfin 資料日期；兩次觀察皆保留。",
        "The sources describe different": "兩邊記錄的是不同日期交易；採用較新的 Redfin MLS 歷史成交，兩次事件皆保留。",
        "Same-date compatible sale prices": "同次成交價格不同，依來源政策採 Redfin MLS，保留 RentCast 原值。",
    }
    for note in comp.reconciliation_notes:
        lines.append("來源說明：" + next((text for prefix, text in translations.items() if note.startswith(prefix)), "來源事件尚待核對，原始說明與雙來源資料已保存在本機。"))
    metric_evidence = reference.field_evidence.get("days_on_market", {})
    if metric_evidence.get("metric") == "time_on_redfin":
        days = source_dom(reference.values.get("days_on_market"))
        if days is not None:
            lines.append(f"Time on Redfin：{days} 天（來源日期 {metric_evidence.get('observed_at', '未提供')}；依 MLS 號碼計算，未當成一般 DOM）。")
    if reference.facts:
        lines.append("Redfin 補充：" + reference.facts[0])
    return lines


def _coverage_lines(view: HomeEvidence) -> List[str]:
    candidates = [comp for comp in (view.home_run.candidates or view.home_run.comps) if not comp.raw.get("_is_subject")]
    total = len(candidates)
    matches = [comp for comp in candidates if comp.redfin_reference is not None]
    adopted = sum(any(source == "redfin" and field != "redfin_url" and field not in comp.unresolved_fields
                      for field, source in comp.field_sources.items()) for comp in matches)
    if total:
        lines = [f"Redfin 已核對連結 {len(matches)}/{total} 筆取得的獨立候選；其中 {adopted} 筆採用 Redfin 欄位。"]
    else:
        lines = ["本次未取得獨立候選，沒有可供核對的 Redfin 連結或欄位。"]
    dates = sorted({comp.redfin_reference.checked_at for comp in matches})
    if dates:
        lines.append("Redfin 查核日期：" + (dates[0] if len(dates) == 1 else dates[0] + " 至 " + dates[-1]) + "；保存快照，非每週自動更新。")
    else:
        lines.append("目前沒有 Redfin 核對快照；新房源查找與頁面更新尚未自動化。")
    excluded = sum(cohort_for(view.home_run.home, comp) == "excluded_condo" for comp in candidates)
    unresolved = sum(bool(comp.unresolved_fields) for comp in candidates)
    if excluded or unresolved:
        lines.append(f"已排除 Condo／房型衝突 {excluded} 筆；另有事件待核對 {unresolved} 筆，受影響指標不計算。")
    if view.home_run.home.property_type.lower() in {"townhouse", "townhome"}:
        lines.append("Townhouse 為來源房型分類，產權與社區可比性未查核；已知 Condo 或分類衝突不納入。")
    return lines


def _search_scope(home_run: HomeRun) -> str:
    home = home_run.home
    parts = [label + str(value).replace(":", " 至 ") for label, value in
             (("房數 ", home.listing_bedrooms), ("衛浴 ", home.listing_bathrooms)) if value]
    return ("掛牌發現條件：" + "；".join(parts) + "。成交紀錄先依地理與日期廣搜，再套用最終條件。") if parts else ""


def _sales_caption(view: HomeEvidence) -> str:
    return ("【來源完整度未知】" if not view.home_run.retrieval else "【部分搜尋】" if any(not entry.complete for entry in view.home_run.retrieval) else "") + f"近 {view.window_days} 天 · {len(view.sales)} 筆符合條件" + (" · 已擴大回溯期間" if view.window_days > 90 else "")


def _counts(view: HomeEvidence) -> str:
    if view.groups:
        return "；".join(_counts(group) for group in view.groups)
    return ("【來源完整度未知】" if not view.home_run.retrieval else "【部分搜尋】" if any(not entry.complete for entry in view.home_run.retrieval) else "") + f"{_type_label(view)} {view.window_days} 天內成交紀錄 {len(view.sales)} 筆 · 近 7 天觀察到的待售開價 {len(view.active)} 筆"


def _subject_summary(home_run: HomeRun) -> str:
    subject = home_run.subject_property or {}
    raw_type = subject.get("propertyType") or home_run.home.property_type
    property_type = {"single family": "獨棟住宅", "townhouse": "連棟住宅", "condo": "公寓"}.get(raw_type.lower(), raw_type)
    parts = [property_type]
    if subject.get("bedrooms") is not None and subject.get("bathrooms") is not None:
        parts.append(f"{_number(subject['bedrooms'])} 房／{_number(subject['bathrooms'])} 衛")
    for key, label in (("squareFootage", "室內"), ("lotSize", "土地")):
        if subject.get(key) is not None:
            parts.append(f"{label} {subject[key]:,} sqft")
    if subject.get("yearBuilt") is not None:
        parts.append(f"{subject['yearBuilt']} 年建")
    return " · ".join(parts)


def _dom_caption(rows: List[ComparableSale]) -> str:
    available = sum(source_dom(comp.days_on_market) is not None for comp in rows)
    return f"來源 DOM 有效值 {available}/{len(rows)} 筆；不是掛牌至簽約或成交的天數。"


def _overview_dom_lines(view: HomeEvidence) -> List[str]:
    if view.groups:
        return [line for group in view.groups for line in _overview_dom_lines(group)]
    prefix = (_type_label(view) + " " if view.evidence_type else "") + "目前待售來源 DOM："
    total = len(view.active)
    groups: Dict[tuple[str, str], List[int]] = {}
    for comp in view.active:
        value = source_dom(comp.days_on_market)
        if value is None or comp.exclude_from_median:
            continue
        source = comp.field_sources.get("days_on_market", "rentcast")
        if source == "rentcast":
            metric = "listing_dom"
        elif source == "redfin" and comp.redfin_reference:
            metric = comp.redfin_reference.field_evidence.get("days_on_market", {}).get("metric")
            if metric not in {"listing_dom", "cumulative_dom"}:
                continue
        else:
            continue
        groups.setdefault((source, metric), []).append(value)
    if not groups:
        lines = [prefix + f"未提供（有效 0/{total} 筆）。"]
    else:
        labels = {("rentcast", "listing_dom"): "RentCast 掛牌 DOM",
                  ("redfin", "listing_dom"): "Redfin 掛牌週期 DOM",
                  ("redfin", "cumulative_dom"): "Redfin 累計 DOM"}
        lines = [prefix + f"{labels[key]} 中位數 {_number(median(values))} 天（有效 {len(values)}/{total} 筆；非成交速度）。"
                 for key, values in sorted(groups.items())]
    excluded = sum(comp.exclude_from_median for comp in view.active)
    if excluded:
        lines.append(f"{_type_label(view)}待售 {excluded} 筆已設定不納入中位數，仍逐筆列示。")
    return lines


@dataclass(frozen=True)
class MarketSummarySection:
    title: str
    headline: str
    lines: tuple[str, ...]
    note: str


def _market_summary_sections(view: HomeEvidence, as_of: datetime) -> List[MarketSummarySection]:
    sections = []
    for group in view.groups or [view]:
        kind = group.evidence_type or group.home_run.home.property_type
        label = "SFH" if kind == "Single Family" else kind
        baseline = group.home_run.capture_baseline
        complete = bool(group.home_run.retrieval) and all(entry.complete for entry in group.home_run.retrieval)
        local_note = "資料充分度：尚無可比基期；本期筆數不代表完整市場。"
        if not complete or baseline and baseline.status == "partial":
            local = "本期搜尋不完整或完整度未知，暫停附近跨期比較。"
            local_note = "資料充分度：不足；缺漏不能視為庫存下降。"
        elif baseline and baseline.status == "changed_cohort":
            local = "搜尋條件或來源版本已變更，正在建立新的可比基準。"
        elif (baseline and baseline.status == "comparable" and baseline.previous_captured_at
              and kind in baseline.previous_active_counts and kind in baseline.current_active_counts):
            previous = baseline.previous_active_counts[kind]
            current = baseline.current_active_counts[kind]
            local = (f"符合條件待售由 {previous} 筆變為 {current} 筆"
                     f"（{baseline.previous_captured_at[:10]} → {as_of.date().isoformat()}）。")
            local_note = "資料充分度：僅可比較相同條件快照；筆數變化不等於新上市或成交。"
        else:
            local = "尚無條件一致的跨期基準，暫不判斷附近升溫或降溫。"
        sections.append(MarketSummarySection(label + " 附近固定條件觀察", local, (), local_note))
        history = group.home_run.regional_history
        regional = assess_regional(history, kind, as_of)
        scope = f"ZIP {history.zip_code}" if history and history.zip_code else "ZIP 未取得"
        sections.append(MarketSummarySection(
            label + " 區域掛牌背景", regional.headline,
            (f"{scope} · {regional.period} 對照 {regional.comparison_period}。", *regional.evidence),
            " ".join((regional.sufficiency, *regional.limitations, regional.source_line))))
    return sections


def _plain_market_summary(view: HomeEvidence, as_of: datetime) -> List[str]:
    lines = []
    for section in _market_summary_sections(view, as_of):
        lines.extend([section.title, section.headline, *section.lines, section.note])
    return lines


def _html_market_summary(view: HomeEvidence, as_of: datetime) -> str:
    sections = []
    for section in _market_summary_sections(view, as_of):
        sections.append(f'<h3 style="margin:18px 0 4px;font-size:15px;color:#172f32;">{_esc(section.title)}</h3>'
                        f'<p style="margin:0 0 6px;font-size:15px;color:#172f32;">{_esc(section.headline)}</p>'
                        + "".join(f'<p style="margin:4px 0 0;font-size:14px;color:#526866;font-variant-numeric:tabular-nums;">{_esc(line)}</p>' for line in section.lines)
                        + f'<p style="margin:6px 0 0;font-size:13px;color:#526866;">{_esc(section.note)}</p>')
    return "".join(sections)


def _health_lines(view: HomeEvidence) -> List[str]:
    lines = ["搜尋完成僅代表讀完本次條件；來源分類或尺寸錯誤，仍可能讓物件未被取得。"]
    metadata = view.home_run.retrieval
    reference_count = sum(comp.redfin_reference is not None for comp in view.home_run.comps)
    if reference_count:
        lines.append(f"目前保留的候選中 {reference_count} 筆附 Redfin 查核快照；可用欄位優先採 Redfin，本機保留雙來源與差異。未建立新房源自動查找或網頁即時更新。")
    if not metadata:
        lines.append("未保存查詢頁數與範圍，來源搜尋完整性未知。")
    elif any(not entry.complete for entry in metadata):
        lines.append("有搜尋達到本期頁數上限；以下僅為部分已取得資料，不能視為完整市場。")
    else:
        lines.append("本次搜尋分頁已讀完；這不保證供應商涵蓋所有交易。")
    for entry in metadata:
        if entry.source == "properties":
            days = entry.filters.get("saleDateRange", "未記錄")
            lines.append(f"房屋紀錄搜尋近 {days} 天，取得 {entry.rows} 筆原始紀錄；再依尺寸、房型與日期判定可比資格。")
    dates = sorted({entry.fetched_at[:10] for entry in metadata if entry.fetched_at})
    if dates:
        lines.append("來源查詢日期：" + "、".join(dates) + "。")
    if view.unknown_type:
        lines.append(f"另有 {view.unknown_type} 筆候選房型未提供，未併入" + ("所選住宅房型證據。" if view.groups or view.evidence_type else "同房型證據。"))
    if not view.sales:
        lines.append(("所選住宅房型" if view.groups else "同房型") + "符合條件的成交為零，是目前資料與篩選結果，不代表周邊一年沒有成交。")
    if view.stale_active:
        lines.append(f"另有 {view.stale_active} 筆標示待售但觀察日期過舊、缺漏或異常，未列入目前待售。")
    return lines


def _plain_rows(rows: List[ComparableSale], kind: str, as_of: datetime) -> List[str]:
    lines = []
    for comp in rows[:3]:
        href, label = _redfin_link(comp)
        lines.extend([comp.address, *_details(comp, kind, as_of), f"{label}：{href}", ""])
    if len(rows) > 3:
        lines.append(f"列出最近 3 筆，共 {len(rows)} 筆。")
    return lines


def _criteria(home_run: HomeRun) -> str:
    home = home_run.home
    groups = []
    for heading, fields in (
        ("初步尺寸篩選", (("室內 sqft", home.comp_square_footage), ("土地 sqft", home.comp_lot_size))),
        ("最終額外限制", (("房數", home.ranked_comp_bedrooms), ("衛浴", home.ranked_comp_bathrooms),
                         ("室內 sqft", home.ranked_comp_square_footage), ("土地 sqft", home.ranked_comp_lot_size))),
    ):
        parts = [label + " " + str(value).replace(":", " 至 ") for label, value in fields if value]
        if parts:
            groups.append(heading + "：" + "；".join(parts))
    if not groups:
        return "相似條件：未另設尺寸／房數硬限制，依來源房型分類。"
    return "相似條件（須同時符合）：" + "。".join(groups) + "。"


def _sale_title(view: HomeEvidence) -> str:
    if view.evidence_type == "Single Family":
        return "SFH 周邊獨棟市場參考"
    return f"{_type_label(view)} 成交參考" if view.evidence_type else "同房型成交紀錄"


def _active_title(view: HomeEvidence) -> str:
    return f"{_type_label(view)} 目前待售開價" if view.evidence_type else "同房型目前待售開價"


def _plain_home(view: HomeEvidence, as_of: datetime) -> str:
    home = view.home_run.home
    lines = [home.name, home.address, f"追蹤房屋： {_subject_summary(view.home_run)}", f"搜尋半徑 {home.max_radius_miles:g} 英里。", _search_scope(view.home_run), _criteria(view.home_run), _summary(view)]
    for group in view.groups or [view]:
        lines.extend(["", _sale_title(group), _sales_caption(group)])
        if not group.sales:
            lines.append("查詢範圍內未取得同時具成交價、成交日期及相符來源房型的可用紀錄；不以開價補足。零筆符合紀錄不代表周邊沒有成交。")
        lines.extend(_plain_rows(group.sales, "sale", as_of))
        lines.extend(["", _active_title(group), "僅列來源近 7 天仍觀察為待售的物件；開價不等於成交價。", _dom_caption(group.active)])
        if not group.active:
            lines.append("未取得符合條件且近期仍被觀察為待售的物件；不代表附近沒有待售房屋。")
        lines.extend(_plain_rows(group.active, "active", as_of))
    lines.extend(["", "資料完整度", *_health_lines(view)])
    return "\n".join(lines)


def _esc(value: object) -> str:
    return html.escape(str(value), quote=True)


def _html_rows(comps: List[ComparableSale], kind: str, as_of: datetime) -> str:
    rows = []
    for comp in comps[:3]:
        detail = _details(comp, kind, as_of)
        href, label = _redfin_link(comp)
        rows.append('<tr><td style="padding:16px 0;border-bottom:1px solid #dce3df;overflow-wrap:anywhere;word-break:break-word;">'
                    f'<p style="margin:0 0 6px;font-size:16px;font-weight:600;color:#172f32;">{_esc(comp.address)}</p>'
                    f'<p style="margin:0 0 5px;font-size:16px;color:#172f32;">{_esc(detail[0])}</p>'
                    + "".join(f'<p style="margin:4px 0 0;font-size:14px;line-height:1.7;color:#526866;">{_esc(line)}</p>' for line in detail[1:]) + f'<p style="margin:10px 0 0;font-size:14px;"><a href="{_esc(href)}" style="color:#1d6560;text-decoration:underline;text-underline-offset:3px;display:inline-block;padding:10px 0;">{_esc(label)}</a></p></td></tr>')
    return '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;table-layout:fixed;"><tbody>' + "".join(rows) + '</tbody></table>'


def _html_home(view: HomeEvidence, as_of: datetime) -> str:
    home = view.home_run.home
    def paragraph(text):
        return f'<p style="margin:8px 0;font-size:14px;color:#526866;">{_esc(text)}</p>'
    def section(title, caption, rows, kind, empty):
        result = [f'<h3 style="margin:28px 0 4px;font-size:18px;color:#172f32;">{_esc(title)}</h3>', paragraph(caption)]
        if rows:
            result.append(_html_rows(rows, kind, as_of))
            if len(rows) > 3:
                result.append(paragraph(f"列出最近 3 筆，共 {len(rows)} 筆。"))
        elif empty:
            result.append(f'<p style="padding:16px;background:#f3f5f1;color:#526866;">{_esc(empty)}</p>')
        return "".join(result)
    parts = [f'<section style="margin-top:36px;"><h2 style="margin:0 0 8px;font-size:22px;line-height:1.4;color:#172f32;">{_esc(home.name)}</h2>',
             paragraph(home.address), paragraph("追蹤房屋：" + _subject_summary(view.home_run)),
             paragraph(f"搜尋半徑 {home.max_radius_miles:g} 英里。"), paragraph(_search_scope(view.home_run)) if _search_scope(view.home_run) else "", paragraph(_criteria(view.home_run))]
    if view.groups:
        parts.append(paragraph(_summary(view)))
    for group in view.groups or [view]:
        parts.extend([
            section(_sale_title(group), _sales_caption(group), group.sales, "sale", "查詢範圍內未取得同時具成交價、成交日期及相符來源房型的可用紀錄；不以開價補足。零筆符合紀錄不代表周邊沒有成交。"),
            section(_active_title(group), "僅列來源近 7 天仍觀察為待售的物件；開價不等於成交價。 " + _dom_caption(group.active), group.active, "active", "未取得符合條件且近期仍被觀察為待售的物件；不代表附近沒有待售房屋。")])
    parts.extend(['<h3 style="margin:28px 0 4px;font-size:16px;color:#172f32;">資料完整度</h3>', *[paragraph(line) for line in _health_lines(view)], '</section>'])
    return "".join(parts)


def render_digest(run: TrackerRun, previous: TrackerState) -> Dict[str, str]:
    as_of = datetime.fromisoformat(run.generated_at.replace("Z", "+00:00"))
    if as_of.tzinfo is None:
        as_of = as_of.replace(tzinfo=timezone.utc)
    as_of = as_of.astimezone(timezone.utc)
    local_time = as_of.astimezone(ZoneInfo("America/Los_Angeles")).strftime("%Y/%m/%d %H:%M")
    views = [_evidence(home_run, as_of) for home_run in run.homes]
    title = f"房市近況｜{len(views)} 間房的成交與待售資料"
    time_label = f"報告產生：{local_time}（太平洋時間）"
    has_history = any(home.regional_history is not None or home.capture_baseline is not None for home in run.homes)
    baseline = ("附近快照變化與區域掛牌背景分開呈現；資料不足時不判斷趨勢，不將首次觀察視為新增上市或成交。"
                if has_history else "變化追蹤尚未啟用；本期不提供新增、降價或房價漲跌比較。")
    disclaimer = "提供買賣判斷的市場證據，不是房屋估值或買賣建議。各欄位與成交紀錄依逐筆標示的來源；下架不代表成交。開價參考落差不是已確認的最終／原始開價比，也不代表議價幅度。"
    text = [title, time_label, "", "本期速覽"]
    overview = []
    for view in views:
        text.extend([view.home_run.home.name, _counts(view), *_overview_dom_lines(view), _summary(view), *_plain_market_summary(view, as_of), *_coverage_lines(view), ""])
        overview.append(f'<tr><td style="padding:16px 0;border-bottom:1px solid #dce3df;"><h2 style="margin:0 0 6px;font-size:17px;color:#172f32;">{_esc(view.home_run.home.name)}</h2><p style="margin:0 0 6px;font-size:15px;color:#1d6560;">{_esc(_counts(view))}</p>'
                        + "".join(f'<p style="margin:6px 0 0;font-size:14px;color:#1d6560;">{_esc(line)}</p>' for line in _overview_dom_lines(view))
                        + f'<p style="margin:6px 0 0;font-size:15px;color:#526866;">{_esc(_summary(view))}</p>'
                        + _html_market_summary(view, as_of)
                        + "".join(f'<p style="margin:6px 0 0;font-size:14px;color:#526866;">{_esc(line)}</p>' for line in _coverage_lines(view)) + '</td></tr>')
    text.extend([baseline, "", *[_plain_home(view, as_of) for view in views], "", disclaimer])
    body = ('<!doctype html><html lang="zh-Hant"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>' + _esc(title) + '</title></head>'
            '<body style="margin:0;padding:0;background:#f3f5f1;font-family:-apple-system,BlinkMacSystemFont,\'Segoe UI\',\'Noto Sans TC\',Arial,sans-serif;font-size:16px;line-height:1.75;color:#172f32;">'
            '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;"><tr><td align="center" style="padding:20px 12px;">'
            '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;max-width:680px;background:#ffffff;table-layout:fixed;"><tr><td style="padding:28px 22px;overflow-wrap:anywhere;word-break:break-word;">'
            f'<h1 style="margin:0 0 8px;font-size:26px;line-height:1.4;font-weight:650;color:#172f32;">房屋市場近況</h1><p style="margin:0 0 20px;font-size:14px;color:#526866;">{_esc(time_label)}</p>'
            '<h2 style="margin:0;font-size:18px;color:#172f32;">本期速覽</h2><table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;table-layout:fixed;">' + "".join(overview) + '</table>'
            f'<p style="margin:16px 0 0;font-size:14px;color:#526866;">{_esc(baseline)}</p>' + "".join(_html_home(view, as_of) for view in views)
            + f'<p style="margin:36px 0 0;padding-top:20px;border-top:1px solid #dce3df;font-size:13px;color:#526866;">{_esc(disclaimer)}</p>'
            '</td></tr></table></td></tr></table></body></html>')
    return {"subject": title, "text_body": "\n".join(text), "html_body": body}
