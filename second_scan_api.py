#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Phone-friendly historical second scanner for Upbit KRW markets.

Purpose
- Query a market/date/time window from a phone browser.
- Fetch 1-second candles (Upbit supports recent 3 months).
- Compute tick-size-aware price movement from the first returned second.
- Optionally enrich recent (<=7 UTC days) windows with raw trade BID/ASK,
  net buy value and trade count using /v1/trades/ticks.
- Download JSON or CSV directly from the browser.

Examples
  /health
  /scan?market=KRW-BIGTIME&date=2026-09-04&from=18:30:00&to=18:35:00
  /scan?market=BIGTIME&date=2026-09-04&from=18:30:00&to=18:35:00&format=csv
  /scan?markets=KRW-BTT,KRW-BIGTIME,KRW-BREV&date=2026-09-04&from=18:30:00&to=18:35:00

KST is the default input timezone. Output timestamps include KST.
This service is read-only and never touches Telegram/Radar state.
"""
from __future__ import annotations

import csv
import io
import json
import math
import os
import time
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Dict, Iterable, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

BASE = "https://api.upbit.com/v1"
HOST = os.getenv("SECOND_SCAN_HOST", "0.0.0.0")
PORT = int(os.getenv("SECOND_SCAN_PORT", "8790"))
KST = timezone(timedelta(hours=9))
UTC = timezone.utc
USER_AGENT = "upbit-second-scan/1.0"
MAX_WINDOW_SEC = int(os.getenv("SECOND_SCAN_MAX_WINDOW_SEC", "3600"))
RATE_SLEEP = float(os.getenv("SECOND_SCAN_RATE_SLEEP", "0.12"))


def http_json(path: str, params: Optional[dict] = None):
    query = "?" + urllib.parse.urlencode(params, doseq=True) if params else ""
    req = urllib.request.Request(BASE + path + query, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.load(resp)


def normalize_market(value: str) -> str:
    value = value.strip().upper()
    if not value:
        raise ValueError("empty market")
    return value if value.startswith("KRW-") else f"KRW-{value}"


def parse_kst(date_s: str, time_s: str) -> datetime:
    # Accept HH:MM[:SS]
    if len(time_s) == 5:
        time_s += ":00"
    return datetime.strptime(f"{date_s} {time_s}", "%Y-%m-%d %H:%M:%S").replace(tzinfo=KST)


def iso_z(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def fetch_tick_size(market: str) -> Optional[float]:
    try:
        rows = http_json("/orderbook/instruments", {"markets": market})
        for row in rows:
            if row.get("market") == market:
                return float(row.get("tick_size"))
    except Exception:
        return None
    return None


def fetch_second_candles(market: str, start: datetime, end: datetime) -> List[dict]:
    """Fetch every available 1-second candle in [start, end], ascending.

    Upbit returns max 200 and `to` is exclusive, so page backwards from end+1s.
    Seconds without trades do not have candles and therefore are absent.
    """
    out: Dict[int, dict] = {}
    cursor = end + timedelta(seconds=1)
    start_epoch = int(start.timestamp())
    guard = 0
    while cursor > start and guard < 1000:
        guard += 1
        rows = http_json("/candles/seconds", {
            "market": market,
            "to": iso_z(cursor),
            "count": 200,
        })
        if not rows:
            break
        oldest_dt = None
        for row in rows:
            ts_raw = row.get("timestamp")
            if ts_raw is not None:
                sec = int(ts_raw) // 1000
                dt = datetime.fromtimestamp(sec, UTC)
            else:
                raw = row.get("candle_date_time_utc")
                if not raw:
                    continue
                dt = datetime.fromisoformat(raw).replace(tzinfo=UTC)
                sec = int(dt.timestamp())
            oldest_dt = dt if oldest_dt is None or dt < oldest_dt else oldest_dt
            if sec < start_epoch or sec > int(end.timestamp()):
                continue
            out[sec] = row
        if oldest_dt is None or oldest_dt <= start:
            break
        cursor = oldest_dt
        time.sleep(RATE_SLEEP)
    return [out[k] for k in sorted(out)]


def can_fetch_trades(start: datetime, end: datetime) -> bool:
    # trades/ticks supports target dates up to 7 UTC days ago.
    now = datetime.now(UTC)
    return start.astimezone(UTC) >= now - timedelta(days=7, hours=1) and end <= now + timedelta(minutes=1)


def utc_day_offset(target: datetime) -> int:
    today = datetime.now(UTC).date()
    return (today - target.astimezone(UTC).date()).days


def fetch_raw_trades(market: str, start: datetime, end: datetime) -> List[dict]:
    """Fetch raw trades in [start,end] when within Upbit's <=7-day trade history.

    We page backwards using sequential_id. The first page is anchored with `to`
    on the target UTC day. A KST window can cross UTC midnight; query per UTC day.
    """
    if not can_fetch_trades(start, end):
        return []
    result: Dict[str, dict] = {}
    day = start.astimezone(UTC).date()
    last_day = end.astimezone(UTC).date()
    while day <= last_day:
        day_start = datetime.combine(day, datetime.min.time(), UTC)
        seg_start = max(start.astimezone(UTC), day_start)
        seg_end = min(end.astimezone(UTC), day_start + timedelta(days=1) - timedelta(microseconds=1))
        offset = (datetime.now(UTC).date() - day).days
        if not (0 <= offset <= 7):
            day += timedelta(days=1)
            continue
        params = {
            "market": market,
            "to": seg_end.strftime("%H:%M:%S"),
            "count": 500,
        }
        if offset > 0:
            params["days_ago"] = offset
        cursor = None
        guard = 0
        done = False
        while not done and guard < 200:
            guard += 1
            if cursor:
                params["cursor"] = cursor
            rows = http_json("/trades/ticks", params)
            if not rows:
                break
            oldest_dt = None
            for row in rows:
                # trade_date_utc + trade_time_utc are authoritative for historical rows.
                d = row.get("trade_date_utc")
                t = row.get("trade_time_utc")
                if not d or not t:
                    continue
                dt = datetime.strptime(f"{d} {t}", "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
                oldest_dt = dt if oldest_dt is None or dt < oldest_dt else oldest_dt
                if seg_start <= dt <= seg_end:
                    sid = str(row.get("sequential_id") or f"{d}T{t}-{row.get('trade_price')}-{row.get('trade_volume')}")
                    result[sid] = row
            if oldest_dt is None or oldest_dt < seg_start:
                done = True
                break
            cursor = str(rows[-1].get("sequential_id") or "")
            if not cursor:
                break
            time.sleep(RATE_SLEEP)
        day += timedelta(days=1)
    return list(result.values())


def aggregate_trades_by_second(trades: Iterable[dict]) -> Dict[int, dict]:
    agg: Dict[int, dict] = defaultdict(lambda: {
        "trade_count": 0,
        "bid_value_krw": 0.0,
        "ask_value_krw": 0.0,
    })
    for row in trades:
        d = row.get("trade_date_utc")
        t = row.get("trade_time_utc")
        if not d or not t:
            continue
        dt = datetime.strptime(f"{d} {t}", "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
        sec = int(dt.timestamp())
        price = float(row.get("trade_price") or 0)
        vol = float(row.get("trade_volume") or 0)
        value = price * vol
        side = str(row.get("ask_bid") or "")
        a = agg[sec]
        a["trade_count"] += 1
        if side == "BID":
            a["bid_value_krw"] += value
        elif side == "ASK":
            a["ask_value_krw"] += value
    for a in agg.values():
        total = a["bid_value_krw"] + a["ask_value_krw"]
        a["bid_ratio"] = a["bid_value_krw"] / total if total else None
        a["net_buy_krw"] = a["bid_value_krw"] - a["ask_value_krw"]
    return agg


def candle_sec_epoch(row: dict) -> int:
    if row.get("timestamp") is not None:
        return int(row["timestamp"]) // 1000
    return int(datetime.fromisoformat(row["candle_date_time_utc"]).replace(tzinfo=UTC).timestamp())


def analyze_market(market: str, start: datetime, end: datetime, enrich_trades: bool = True) -> dict:
    tick = fetch_tick_size(market)
    candles = fetch_second_candles(market, start, end)
    trades = fetch_raw_trades(market, start, end) if enrich_trades else []
    by_sec = aggregate_trades_by_second(trades)
    rows = []
    anchor_price = None
    prev_close = None
    baseline_values: List[float] = []

    for c in candles:
        sec = candle_sec_epoch(c)
        close = float(c.get("trade_price") or 0)
        open_ = float(c.get("opening_price") or close)
        high = float(c.get("high_price") or close)
        low = float(c.get("low_price") or close)
        value = float(c.get("candle_acc_trade_price") or 0)
        volume = float(c.get("candle_acc_trade_volume") or 0)
        if anchor_price is None:
            anchor_price = open_ or close
        tick_pct = (tick / close * 100.0) if tick and close else None
        ticks_from_anchor = ((close - anchor_price) / tick) if tick and anchor_price is not None else None
        ticks_from_prev = ((close - prev_close) / tick) if tick and prev_close is not None else None
        pct_from_anchor = ((close / anchor_price) - 1.0) * 100.0 if anchor_price else None
        pct_from_prev = ((close / prev_close) - 1.0) * 100.0 if prev_close else None
        baseline = sum(baseline_values[-10:]) / len(baseline_values[-10:]) if baseline_values else None
        value_x = value / baseline if baseline and baseline > 0 else None
        t = by_sec.get(sec, {})
        row = {
            "market": market,
            "timestamp_kst": datetime.fromtimestamp(sec, KST).isoformat(),
            "epoch_sec": sec,
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "value_1s_krw": value,
            "volume_1s": volume,
            "tick_size": tick,
            "tick_pct_at_close": tick_pct,
            "ticks_from_anchor": ticks_from_anchor,
            "ticks_from_prev": ticks_from_prev,
            "pct_from_anchor": pct_from_anchor,
            "pct_from_prev": pct_from_prev,
            "value_vs_prev10_active_sec_x": value_x,
            "trade_count": t.get("trade_count"),
            "bid_value_krw": t.get("bid_value_krw"),
            "ask_value_krw": t.get("ask_value_krw"),
            "bid_ratio": t.get("bid_ratio"),
            "net_buy_krw": t.get("net_buy_krw"),
        }
        rows.append(row)
        baseline_values.append(value)
        prev_close = close

    # Identify first observed >=2.5x relative to previous 10 active seconds.
    t0_idx = next((i for i, r in enumerate(rows) if (r.get("value_vs_prev10_active_sec_x") or 0) >= 2.5), None)
    t0 = None
    if t0_idx is not None:
        base = rows[t0_idx]
        t0_price = base["close"]
        offsets = {}
        for off in (0, 1, 2, 3, 5, 10):
            target_epoch = base["epoch_sec"] + off
            rr = next((r for r in rows[t0_idx:] if r["epoch_sec"] >= target_epoch), None)
            if rr:
                offsets[str(off)] = {
                    "timestamp_kst": rr["timestamp_kst"],
                    "close": rr["close"],
                    "ticks_from_t0": ((rr["close"] - t0_price) / tick) if tick else None,
                    "pct_from_t0": ((rr["close"] / t0_price) - 1.0) * 100.0 if t0_price else None,
                    "value_1s_krw": rr["value_1s_krw"],
                    "value_x": rr["value_vs_prev10_active_sec_x"],
                    "bid_ratio": rr["bid_ratio"],
                    "net_buy_krw": rr["net_buy_krw"],
                    "trade_count": rr["trade_count"],
                }
        t0 = {
            "index": t0_idx,
            "timestamp_kst": base["timestamp_kst"],
            "price": t0_price,
            "tick_size": tick,
            "tick_pct": (tick / t0_price * 100.0) if tick and t0_price else None,
            "value_x": base["value_vs_prev10_active_sec_x"],
            "offsets": offsets,
        }

    return {
        "market": market,
        "from_kst": start.isoformat(),
        "to_kst": end.isoformat(),
        "tick_size": tick,
        "seconds_with_trades": len(rows),
        "raw_trade_enrichment": bool(trades),
        "raw_trade_count": len(trades),
        "t0_first_2_5x": t0,
        "rows": rows,
    }


def parse_request(qs: dict) -> Tuple[List[str], datetime, datetime, bool]:
    raw_markets = qs.get("markets", qs.get("market", []))
    if not raw_markets:
        raise ValueError("market or markets is required")
    vals = []
    for raw in raw_markets:
        vals.extend(x for x in raw.split(",") if x.strip())
    markets = [normalize_market(x) for x in vals]
    if len(markets) > 20:
        raise ValueError("max 20 markets per request")
    date_s = qs.get("date", [""])[0]
    from_s = qs.get("from", [""])[0]
    to_s = qs.get("to", [""])[0]
    if not date_s or not from_s or not to_s:
        raise ValueError("date, from, to are required")
    start = parse_kst(date_s, from_s)
    end = parse_kst(date_s, to_s)
    if end < start:
        end += timedelta(days=1)
    seconds = (end - start).total_seconds()
    if seconds <= 0 or seconds > MAX_WINDOW_SEC:
        raise ValueError(f"window must be 1..{MAX_WINDOW_SEC} seconds")
    enrich = qs.get("trades", ["1"])[0].lower() not in {"0", "false", "no"}
    return markets, start, end, enrich


def to_csv(payload: dict) -> bytes:
    fields = [
        "market","timestamp_kst","epoch_sec","open","high","low","close",
        "value_1s_krw","volume_1s","tick_size","tick_pct_at_close",
        "ticks_from_anchor","ticks_from_prev","pct_from_anchor","pct_from_prev",
        "value_vs_prev10_active_sec_x","trade_count","bid_count","ask_count","bid_value_krw","ask_value_krw",
        "bid_ratio","net_buy_krw",
    ]
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=fields)
    w.writeheader()
    for market_result in payload["results"]:
        for row in market_result["rows"]:
            w.writerow({k: row.get(k) for k in fields})
    return buf.getvalue().encode("utf-8-sig")


class Handler(BaseHTTPRequestHandler):
    server_version = "UpbitSecondScan/1.0"

    def log_message(self, fmt, *args):
        print("[second-scan-http] " + (fmt % args), flush=True)

    def send_json(self, status: int, payload: dict):
        raw = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/health":
            self.send_json(200, {
                "ok": True,
                "service": "upbit-second-scan",
                "port": PORT,
                "max_window_sec": MAX_WINDOW_SEC,
                "note": "1s candles: recent 3 months; BID/ASK raw trades: recent 7 UTC days when available",
            })
            return
        if parsed.path != "/scan":
            self.send_json(404, {
                "error": "not_found",
                "example": "/scan?market=KRW-BIGTIME&date=2026-09-04&from=18:30:00&to=18:35:00",
            })
            return
        qs = parse_qs(parsed.query)
        try:
            markets, start, end, enrich = parse_request(qs)
            results = []
            for market in markets:
                results.append(analyze_market(market, start, end, enrich))
                time.sleep(RATE_SLEEP)
            payload = {
                "generated_at_kst": datetime.now(KST).isoformat(),
                "markets": markets,
                "from_kst": start.isoformat(),
                "to_kst": end.isoformat(),
                "results": results,
            }
            fmt = qs.get("format", ["json"])[0].lower()
            if fmt == "csv":
                raw = to_csv(payload)
                filename = f"second_scan_{start.strftime('%Y%m%d_%H%M%S')}.csv"
                self.send_response(200)
                self.send_header("Content-Type", "text/csv; charset=utf-8")
                self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
                self.send_header("Content-Length", str(len(raw)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(raw)
            else:
                self.send_json(200, payload)
        except Exception as exc:
            self.send_json(400, {"error": type(exc).__name__, "message": str(exc)})


def main():
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"[second-scan] listening on http://{HOST}:{PORT}", flush=True)
    print(f"[second-scan] max_window={MAX_WINDOW_SEC}s", flush=True)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
