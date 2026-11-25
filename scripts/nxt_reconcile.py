import json
import os
import re
import urllib.request
from datetime import datetime, timedelta

import generate_stock_brief as gsb

NAVER_STOCK_TARGETS = [
    ("005930", "삼성전자", "samsung", "#2563eb"),
    ("000660", "SK하이닉스", "skhynix", "#EA002C"),
]


def _fetch_json(url: str, timeout: int = 10):
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0",
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _parse_int(val) -> int:
    return int(str(val).replace(",", "").strip())


def fetch_nxt_close(code: str, target_date_str: str, is_evening_today: bool = False):
    """Fetch finalized 20:00 NXT-integrated closing price for `code` on `target_date_str`."""
    try:
        if is_evening_today:
            basic = _fetch_json(f"https://m.stock.naver.com/api/stock/{code}/basic")
            over = basic.get("overMarketPriceInfo") or {}
            if (
                over.get("tradingSessionType") == "AFTER_MARKET"
                and over.get("overMarketStatus") == "CLOSE"
                and str(over.get("localTradedAt", "")).startswith(target_date_str)
            ):
                return {
                    "price": _parse_int(over["overPrice"]),
                    "change": _parse_int(over["compareToPreviousClosePrice"]),
                    "ratio": float(over["fluctuationsRatio"]),
                    "source": "naver_over_after_market",
                }

        rows = _fetch_json(f"https://m.stock.naver.com/api/stock/{code}/price?pageSize=7")
        if isinstance(rows, list):
            for row in rows:
                if row.get("localTradedAt") == target_date_str:
                    return {
                        "price": _parse_int(row["closePrice"]),
                        "change": _parse_int(row["compareToPreviousClosePrice"]),
                        "ratio": float(row["fluctuationsRatio"]),
                        "source": "naver_daily_price",
                    }
    except Exception as exc:
        print(f"[nxt_reconcile] Warning: failed to fetch {code} for {target_date_str}: {exc}")
    return None


def _is_band_hit(band_str: str, actual_ratio: float) -> bool:
    low_v, high_v = gsb.extract_range_from_str(band_str)
    if low_v is None or high_v is None:
        return False
    return low_v <= actual_ratio <= high_v


def _direction_from_ratio(ratio: float) -> str:
    if ratio > 0:
        return "up"
    if ratio < 0:
        return "down"
    return "neutral"


def _resolve_audit_date(mmdd_label: str, ref_kst: datetime):
    m = re.match(r"^\s*(\d{1,2})\.(\d{1,2})", str(mmdd_label))
    if not m:
        return None
    month, day = int(m.group(1)), int(m.group(2))
    year = ref_kst.year
    if month > ref_kst.month + 1:
        year -= 1
    try:
        return datetime(year, month, day, tzinfo=ref_kst.tzinfo)
    except ValueError:
        return None


def reconcile_evening_data(data: dict, target_date_str: str) -> bool:
    """Patch evening brief JSON in-memory with finalized 20:00 NXT closing prices."""
    closes = {}
    for code, _, key, _ in NAVER_STOCK_TARGETS:
        info = fetch_nxt_close(code, target_date_str, is_evening_today=True)
        if info:
            closes[code] = info

    if not closes:
        return False

    changed = False
    target_hits = []
    for t in data.get("targets", []):
        code = str(t.get("code", ""))
        info = closes.get(code)
        if info:
            hit = _is_band_hit(t.get("gap", ""), info["ratio"])
            new_band = f"실제: {info['ratio']:+.2f}% ({'⭕ 적중' if hit else '❌ 실패'})"
            if t.get("band") != new_band:
                t["band"] = new_band
                changed = True
            note = f"20:00 NXT 확정 {info['price']:,}원({info['change']:+,}원, {info['ratio']:+.2f}%)"
            desc = t.get("desc", "")
            if "20:00 NXT 확정" not in desc:
                t["desc"] = f"{note}. {desc}" if desc else note
                changed = True
            target_hits.append(hit)
        else:
            act = gsb.extract_single_pct(t.get("band", ""))
            if act is not None:
                target_hits.append(_is_band_hit(t.get("gap", ""), act))

    for s in data.get("stocks", []):
        name = str(s.get("name", ""))
        for code, kor_name, _, _ in NAVER_STOCK_TARGETS:
            if code in name or kor_name in name:
                info = closes.get(code)
                if info:
                    new_price = f"{info['price']:,}원 (NXT)"
                    new_chg = f"{info['ratio']:+.2f}%"
                    new_dir = _direction_from_ratio(info["ratio"])
                    if s.get("price") != new_price or s.get("change") != new_chg:
                        s["price"] = new_price
                        s["change"] = new_chg
                        s["direction"] = new_dir
                        changed = True

    audit = data.get("todayAudit")
    if isinstance(audit, dict):
        for t in data.get("targets", []):
            code = str(t.get("code", ""))
            info = closes.get(code)
            if not info:
                continue
            pred_str = str(t.get("gap", "")).replace("오전예측:", "").replace("오전예측(방향):", "").strip()
            val_str = f"예측 {pred_str} / 실제 {info['ratio']:+.2f}%(NXT {info['price']:,})"
            if code == "005930" and audit.get("samsung") != val_str:
                audit["samsung"] = val_str
                changed = True
            elif code == "000660" and audit.get("skhynix") != val_str:
                audit["skhynix"] = val_str
                changed = True
        if len(target_hits) == 3:
            new_res = "⭕" if all(target_hits) else "❌"
            if audit.get("result") in ("⭕", "❌") and audit.get("result") != new_res:
                audit["result"] = new_res
                changed = True

    return changed


def _extract_audit_pred_and_actual(field_str: str):
    parts = str(field_str).split("/")
    pred_part = parts[0].strip() if len(parts) > 0 else ""
    act_part = parts[1].strip() if len(parts) > 1 else ""
    return pred_part, act_part


def _patch_evening_html(html_path: str, history: list, updated_entry: dict, closes: dict) -> bool:
    if not os.path.exists(html_path):
        return False
    with open(html_path, "r", encoding="utf-8") as f:
        html = f.read()

    orig_html = html
    for code, kor_name, field_key, accent in NAVER_STOCK_TARGETS:
        info = closes.get(code)
        if not info:
            continue
        pred_part, _ = _extract_audit_pred_and_actual(updated_entry[field_key])
        new_ratio_str = f"{info['ratio']:+.2f}%"
        new_price_str = f"{info['price']:,}원"

        # 1. Update target card meter (if old percentage is present in card)
        card_pat = re.compile(
            rf'(<div class="target-name"[^>]*>{re.escape(kor_name)}<span[^>]*>{code}</span></div>\s*</div>\s*)'
            rf'(<div class="band-meter-wrap">.*?</div>\s*</div>\s*)'
            rf'(<div class="target-desc"[^>]*>)(.*?)(</div>)',
            re.S,
        )
        m = card_pat.search(html)
        if m:
            gap_match = re.search(r"마감:\s*(.*?)</span>", m.group(2))
            gap_label = gap_match.group(1).strip() if gap_match else pred_part
            new_meter = gsb.render_band_meter({"band": new_ratio_str, "gap": gap_label}, "evening").strip()
            old_desc = m.group(4)
            note = f"[20:00 NXT 확정: {new_price_str} ({info['change']:+,}원, {new_ratio_str})]"
            if "[20:00 NXT 확정:" in old_desc:
                new_desc = re.sub(r"\[20:00 NXT 확정:[^\]]+\]", note, old_desc)
            else:
                new_desc = f"{note} {old_desc}"
            html = html[: m.start()] + m.group(1) + new_meter + "\n    \n        " + m.group(3) + new_desc + m.group(5) + html[m.end() :]

        # 2. Update stock card in stocksGrid
        stock_pat = re.compile(
            rf'(<div class="stock-card"[^>]*onclick="openStockModal\(\'{re.escape(kor_name)} \({code}\)\', \')([^\']*)(\'[^>]*>.*?'
            rf'<span class="stock-chg [^"]*">)([^<]*)(</span>\s*</div>\s*<div class="stock-sub"[^>]*>)([^<]*)(</div>)',
            re.S,
        )
        sm = stock_pat.search(html)
        if sm:
            new_modal_price = f"{new_price_str} (NXT) ({new_ratio_str})"
            html = (
                html[: sm.start()]
                + sm.group(1)
                + new_modal_price
                + sm.group(3)
                + new_ratio_str
                + sm.group(5)
                + f"{new_price_str} (NXT)"
                + sm.group(7)
                + html[sm.end() :]
            )

        # 3. Re-render quant chart for samsung / skhynix
        new_chart = gsb.render_quant_section_chart(history, field_key, f"{kor_name} ({code})", accent).strip()
        chart_pat = re.compile(
            rf'<div style="margin-bottom:14px; background:{re.escape(accent)}22;.*?</div>\s*(?=<div style="margin-bottom:14px; background:|</div>\s*<div class="section-title">)',
            re.S,
        )
        if chart_pat.search(html):
            html = chart_pat.sub(lambda _: new_chart + "\n    \n      ", html, count=1)

    # 4. Update audit table row for this date
    row_pat = re.compile(
        rf'(<td style="font-weight:700; white-space:nowrap;">{re.escape(updated_entry["date"])}</td>\s*'
        rf'<td>.*?</td>\s*<td>)(.*?)(</td>\s*<td>)(.*?)(</td>\s*'
        rf'<td style="text-align:center;" class="eval-circle">)(.*?)(</td>)',
        re.S,
    )
    rm = row_pat.search(html)
    if rm:
        html = (
            html[: rm.start()]
            + rm.group(1)
            + updated_entry["samsung"]
            + rm.group(3)
            + updated_entry["skhynix"]
            + rm.group(5)
            + updated_entry["result"]
            + rm.group(7)
            + html[rm.end() :]
        )

    if html != orig_html:
        with open(html_path, "w", encoding="utf-8") as f:
            f.write(html)
        return True
    return False


def reconcile_previous_evening(now_kst: datetime, docs_dir: str, audit_history_path: str):
    """
    During morning runs, verify the most recent completed trading day's audit_history
    and evening HTML against finalized Naver 20:00 NXT closes, updating them if needed.
    Returns (changed: bool, prev_closes: dict).
    """
    if not os.path.exists(audit_history_path):
        return False, {}

    with open(audit_history_path, "r", encoding="utf-8") as f:
        history = json.load(f)

    today_date = now_kst.date()
    target_idx = None
    target_dt = None
    for idx in range(len(history) - 1, -1, -1):
        entry = history[idx]
        if entry.get("result") not in ("⭕", "❌"):
            continue
        dt = _resolve_audit_date(entry.get("date", ""), now_kst)
        if dt and dt.date() < today_date:
            target_idx = idx
            target_dt = dt
            break

    if target_idx is None or target_dt is None:
        return False, {}

    prev_date_str = target_dt.strftime("%Y-%m-%d")
    closes = {}
    for code, _, _, _ in NAVER_STOCK_TARGETS:
        info = fetch_nxt_close(code, prev_date_str, is_evening_today=False)
        if info:
            closes[code] = info

    if not closes:
        return False, {}

    entry = history[target_idx]
    entry_changed = False
    for code, _, field_key, _ in NAVER_STOCK_TARGETS:
        info = closes.get(code)
        if not info:
            continue
        pred_part, act_part = _extract_audit_pred_and_actual(entry.get(field_key, ""))
        expected_act = f"실제 {info['ratio']:+.2f}%(NXT {info['price']:,})"
        if f"NXT {info['price']:,}" not in act_part or f"{info['ratio']:+.2f}%" not in act_part:
            entry[field_key] = f"{pred_part} / {expected_act}" if pred_part else expected_act
            entry_changed = True

    if not entry_changed:
        return False, closes

    # Re-evaluate overall binary result (all 3 targets must hit)
    hits = []
    for k in ("kospi", "samsung", "skhynix"):
        pred_p, act_p = _extract_audit_pred_and_actual(entry.get(k, ""))
        act_v = gsb.extract_single_pct(act_p)
        if act_v is not None:
            hits.append(_is_band_hit(pred_p, act_v))
    if len(hits) == 3:
        entry["result"] = "⭕" if all(hits) else "❌"

    with open(audit_history_path, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=2)

    prev_html = os.path.join(docs_dir, target_dt.strftime("%Y-%m"), f"{prev_date_str}-evening.html")
    _patch_evening_html(prev_html, history, entry, closes)
    print(f"[nxt_reconcile] Reconciled {prev_date_str} evening close in audit_history.json & HTML.")
    return True, closes


def annotate_morning_data(data: dict, prev_closes: dict) -> bool:
    """If morning JSON notes '20:00 NXT 미갱신', append the finalized previous NXT close."""
    if not prev_closes:
        return False
    changed = False
    for t in data.get("targets", []):
        code = str(t.get("code", ""))
        info = prev_closes.get(code)
        desc = t.get("desc", "")
        if info and "미갱신" in desc:
            note = f"전일 20:00 NXT 확정 {info['price']:,}원({info['ratio']:+.2f}%)"
            if note not in desc:
                t["desc"] = f"{desc} [{note}]"
                changed = True
    return changed
