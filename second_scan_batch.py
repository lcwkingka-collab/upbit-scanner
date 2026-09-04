#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""One-click historical second scanner for known surge cases.

Default mode auto-discovers each market's strongest recent surge day/window,
then uses second_scan_api.py to fetch tick-aware 1-second data around that window.
Designed for GitHub Actions/manual phone use. Read-only: no Telegram/radar changes.
"""
from __future__ import annotations

import argparse
import csv
import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Tuple

import second_scan_api as ss

KST = timezone(timedelta(hours=9))
OUT = Path("second_scan_output")
DEFAULT_SUCCESS9 = [
    "KRW-ENA", "KRW-XPL", "KRW-PEPE", "KRW-ZRO", "KRW-LIT",
    "KRW-BIO", "KRW-ARB", "KRW-AZTEC", "KRW-DRV",
]


def get(path: str, params: dict):
    return ss.http_json(path, params)


def parse_day_kst(row: dict) -> datetime:
    raw = row.get("candle_date_time_kst")
    if raw:
        return datetime.fromisoformat(raw).replace(tzinfo=KST)
    raw = row.get("candle_date_time_utc")
    return datetime.fromisoformat(raw).replace(tzinfo=timezone.utc).astimezone(KST)


def recent_daily_candidates(market: str, days: int = 90, topn: int = 4) -> List[dict]:
    rows = get("/candles/days", {"market": market, "count": min(days, 200)})
    scored = []
    for r in rows:
        try:
            o = float(r["opening_price"])
            h = float(r["high_price"])
            l = float(r["low_price"])
            c = float(r["trade_price"])
            value = float(r.get("candle_acc_trade_price") or 0)
        except Exception:
            continue
        if o <= 0 or l <= 0:
            continue
        up_from_open = (h / o - 1) * 100
        range_pct = (h / l - 1) * 100
        close_ret = (c / o - 1) * 100
        # Favor days with actual upside expansion, but retain value/range context.
        score = up_from_open * 1.5 + max(close_ret, 0) * 0.5 + range_pct * 0.25
        scored.append({
            "day": parse_day_kst(r).date().isoformat(),
            "score": score,
            "up_from_open_pct": up_from_open,
            "range_pct": range_pct,
            "close_ret_pct": close_ret,
            "trade_value_krw": value,
        })
    scored.sort(key=lambda x: x["score"], reverse=True)
    return scored[:topn]


def fetch_minutes_for_day(market: str, day_s: str) -> List[dict]:
    day = datetime.strptime(day_s, "%Y-%m-%d").replace(tzinfo=KST)
    start = day
    end = day + timedelta(days=1)
    out: Dict[int, dict] = {}
    cursor = end
    guard = 0
    while cursor > start and guard < 20:
        guard += 1
        rows = get("/candles/minutes/1", {
            "market": market,
            "to": ss.iso_z(cursor),
            "count": 200,
        })
        if not rows:
            break
        oldest = None
        for r in rows:
            raw = r.get("candle_date_time_utc")
            if not raw:
                continue
            dt = datetime.fromisoformat(raw).replace(tzinfo=timezone.utc)
            oldest = dt if oldest is None or dt < oldest else oldest
            if start.astimezone(timezone.utc) <= dt < end.astimezone(timezone.utc):
                out[int(dt.timestamp())] = r
        if oldest is None or oldest <= start.astimezone(timezone.utc):
            break
        cursor = oldest
        time.sleep(ss.RATE_SLEEP)
    return [out[k] for k in sorted(out)]


def strongest_10m_window(minutes: List[dict]) -> Tuple[datetime, datetime, dict]:
    if len(minutes) < 3:
        raise RuntimeError("not enough minute candles")
    pts = []
    for r in minutes:
        dt = datetime.fromisoformat(r["candle_date_time_utc"]).replace(tzinfo=timezone.utc)
        pts.append((dt, float(r["opening_price"]), float(r["high_price"]), float(r["trade_price"]), float(r.get("candle_acc_trade_price") or 0)))
    best = None
    for i in range(len(pts)):
        start_dt, start_open, _, _, _ = pts[i]
        if start_open <= 0:
            continue
        jmax = min(len(pts), i + 11)
        seg = pts[i:jmax]
        high = max(x[2] for x in seg)
        end_idx = next(k for k, x in enumerate(seg) if x[2] == high)
        ret = (high / start_open - 1) * 100
        value = sum(x[4] for x in seg[:end_idx+1])
        item = (ret, value, i, end_idx)
        if best is None or item[:2] > best[:2]:
            best = item
    if best is None:
        raise RuntimeError("no valid minute window")
    ret, value, i, end_idx = best
    start_dt = pts[i][0].astimezone(KST)
    peak_dt = pts[i + end_idx][0].astimezone(KST)
    # Pull 3 min before and 5 min after to capture pre-acceleration and continuation.
    scan_start = start_dt - timedelta(minutes=3)
    scan_end = peak_dt + timedelta(minutes=5)
    # Keep within 1 hour second-scan safety limit.
    if scan_end - scan_start > timedelta(minutes=30):
        scan_end = scan_start + timedelta(minutes=30)
    meta = {
        "window_start_kst": start_dt.isoformat(),
        "window_peak_kst": peak_dt.isoformat(),
        "window_return_pct": ret,
        "window_trade_value_krw": value,
        "scan_from_kst": scan_start.isoformat(),
        "scan_to_kst": scan_end.isoformat(),
    }
    return scan_start, scan_end, meta


def discover_case(market: str, days: int) -> Tuple[datetime, datetime, dict]:
    day_candidates = recent_daily_candidates(market, days=days, topn=4)
    best = None
    errors = []
    for d in day_candidates:
        try:
            mins = fetch_minutes_for_day(market, d["day"])
            start, end, meta = strongest_10m_window(mins)
            candidate = (meta["window_return_pct"], d["score"], start, end, d, meta)
            if best is None or candidate[:2] > best[:2]:
                best = candidate
        except Exception as exc:
            errors.append(f"{d['day']}: {exc}")
    if best is None:
        raise RuntimeError("auto-discovery failed: " + "; ".join(errors))
    _, _, start, end, day_meta, win_meta = best
    return start, end, {"daily": day_meta, "window": win_meta}


def flatten_rows(case_result: dict, discovery: dict) -> List[dict]:
    out = []
    t0 = case_result.get("t0_first_2_5x") or {}
    t0_epoch = None
    if t0.get("timestamp_kst"):
        try:
            t0_epoch = int(datetime.fromisoformat(t0["timestamp_kst"]).timestamp())
        except Exception:
            pass
    for r in case_result.get("rows", []):
        row = dict(r)
        row["t_from_first_2_5x_sec"] = None if t0_epoch is None else int(r["epoch_sec"] - t0_epoch)
        row["auto_day"] = discovery.get("daily", {}).get("day")
        row["auto_window_return_pct"] = discovery.get("window", {}).get("window_return_pct")
        out.append(row)
    return out


def write_outputs(results: List[dict], rows: List[dict], errors: List[dict]) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at_kst": datetime.now(KST).isoformat(),
        "case_count": len(results),
        "errors": errors,
        "cases": results,
    }
    (OUT / "success9_second_scan.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    if rows:
        fields = []
        seen = set()
        for row in rows:
            for k in row:
                if k not in seen:
                    seen.add(k); fields.append(k)
        with (OUT / "success9_second_scan.csv").open("w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
            w.writeheader(); w.writerows(rows)
    summary_rows = []
    for x in results:
        c = x["scan"]
        t0 = c.get("t0_first_2_5x") or {}
        summary_rows.append({
            "market": x["market"],
            "auto_day": x["discovery"].get("daily", {}).get("day"),
            "auto_window_return_pct": x["discovery"].get("window", {}).get("window_return_pct"),
            "scan_from_kst": c.get("from_kst"),
            "scan_to_kst": c.get("to_kst"),
            "tick_size": c.get("tick_size"),
            "seconds_with_trades": c.get("seconds_with_trades"),
            "raw_trade_enrichment": c.get("raw_trade_enrichment"),
            "t0_timestamp_kst": t0.get("timestamp_kst"),
            "t0_price": t0.get("price"),
            "t0_tick_pct": t0.get("tick_pct"),
            "t0_value_x": t0.get("value_x"),
            "t0_offsets_json": json.dumps(t0.get("offsets", {}), ensure_ascii=False),
        })
    if summary_rows:
        with (OUT / "success9_summary.csv").open("w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=list(summary_rows[0].keys()))
            w.writeheader(); w.writerows(summary_rows)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--markets", default=",".join(DEFAULT_SUCCESS9))
    p.add_argument("--days", type=int, default=90)
    p.add_argument("--no-trades", action="store_true", help="skip raw trade enrichment")
    args = p.parse_args()
    markets = [ss.normalize_market(x) for x in args.markets.split(",") if x.strip()]
    results, all_rows, errors = [], [], []
    for idx, market in enumerate(markets, 1):
        print(f"[{idx}/{len(markets)}] discover {market}", flush=True)
        try:
            start, end, discovery = discover_case(market, args.days)
            print(f"  -> {start.isoformat()} ~ {end.isoformat()}", flush=True)
            scan = ss.analyze_market(market, start, end, enrich_trades=not args.no_trades)
            results.append({"market": market, "discovery": discovery, "scan": scan})
            all_rows.extend(flatten_rows(scan, discovery))
        except Exception as exc:
            errors.append({"market": market, "error": str(exc)})
            print(f"  ERROR {exc}", flush=True)
        time.sleep(0.2)
    write_outputs(results, all_rows, errors)
    print(json.dumps({"cases": len(results), "errors": errors, "output": str(OUT)}, ensure_ascii=False), flush=True)
    if not results:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
