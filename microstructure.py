import csv
import json
import math
import os
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE = "https://api.upbit.com/v1"
OUT = Path("data")
KST = timezone(timedelta(hours=9))
WINDOW_MINUTES = 15
HISTORY_POINTS = 192  # 48 hours at 15-minute cadence
MAX_TRADE_PAGES = int(os.getenv("UPBIT_MAX_TRADE_PAGES", "12"))
PAGE_SIZE = 500


def get(path, params=None, tries=5):
    url = BASE + path + ("?" + urllib.parse.urlencode(params) if params else "")
    error = None
    for attempt in range(tries):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "upbit-scanner/2.0"})
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.load(response)
        except Exception as exc:
            error = exc
            time.sleep(0.7 * (attempt + 1))
    raise error


def pct_change(current, previous):
    if current is None or previous in (None, 0):
        return None
    return (current / previous - 1) * 100


def safe_float(value):
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def chunks(items, size):
    for index in range(0, len(items), size):
        yield items[index:index + size]


def fetch_batched(path, markets):
    rows = []
    for group in chunks(markets, 100):
        rows.extend(get(path, {"markets": ",".join(group)}))
        time.sleep(0.13)
    return {row["market"]: row for row in rows}


def trade_timestamp_ms(row):
    value = row.get("timestamp", row.get("trade_timestamp"))
    return int(value) if value is not None else 0


def fetch_trades_15m(market, now_ms):
    cutoff = now_ms - WINDOW_MINUTES * 60 * 1000
    collected = []
    seen = set()
    cursor = None
    reached_cutoff = False
    for _ in range(MAX_TRADE_PAGES):
        params = {"market": market, "count": PAGE_SIZE}
        if cursor is not None:
            params["cursor"] = cursor
        page = get("/trades/ticks", params)
        if not page:
            reached_cutoff = True
            break
        oldest_cursor = None
        for trade in page:
            trade_id = trade.get("sequential_id")
            key = trade_id if trade_id is not None else (trade_timestamp_ms(trade), trade.get("trade_price"), trade.get("trade_volume"))
            if key in seen:
                continue
            seen.add(key)
            timestamp = trade_timestamp_ms(trade)
            if timestamp < cutoff:
                reached_cutoff = True
                continue
            collected.append(trade)
            if trade_id is not None:
                oldest_cursor = trade_id if oldest_cursor is None else min(oldest_cursor, trade_id)
        oldest_timestamp = min((trade_timestamp_ms(row) for row in page), default=0)
        if oldest_timestamp < cutoff or len(page) < PAGE_SIZE:
            reached_cutoff = True
            break
        if oldest_cursor is None or oldest_cursor == cursor:
            break
        cursor = oldest_cursor
        time.sleep(0.13)

    bid_value = 0.0
    ask_value = 0.0
    unknown_value = 0.0
    for trade in collected:
        value = float(trade["trade_price"]) * float(trade["trade_volume"])
        side = trade.get("ask_bid")
        if side == "BID":
            bid_value += value
        elif side == "ASK":
            ask_value += value
        else:
            unknown_value += value
    ratio = bid_value / ask_value if ask_value > 0 else None
    timestamps = [trade_timestamp_ms(row) for row in collected]
    return {
        "bid_trade_value_15m": bid_value,
        "ask_trade_value_15m": ask_value,
        "buy_sell_ratio_15m": ratio,
        "trade_count_15m": len(collected),
        "trade_unknown_value_15m": unknown_value,
        "trade_coverage_15m": "complete" if reached_cutoff else "partial",
        "trade_oldest_timestamp": min(timestamps) if timestamps else None,
        "trade_newest_timestamp": max(timestamps) if timestamps else None,
    }


def orderbook_values(row):
    bid = safe_float(row.get("total_bid_size"))
    ask = safe_float(row.get("total_ask_size"))
    total = (bid or 0) + (ask or 0)
    return {
        "total_bid_size": bid,
        "total_ask_size": ask,
        "orderbook_imbalance": ((bid - ask) / total) if bid is not None and ask is not None and total else None,
    }


def day_path(ticker):
    opening = safe_float(ticker.get("opening_price"))
    high = safe_float(ticker.get("high_price"))
    current = safe_float(ticker.get("trade_price"))
    high_return = pct_change(high, opening)
    current_return = pct_change(current, opening)
    drawdown = ((high - current) / high * 100) if high else None
    retrace = None
    if high_return is not None and high_return > 0 and current_return is not None:
        retrace = max(0.0, (high_return - current_return) / high_return * 100)
    return {
        "day_open": opening,
        "day_high": high,
        "day_high_return_pct": high_return,
        "current_day_return_pct": current_return,
        "drawdown_from_day_high_pct": drawdown,
        "gain_retrace_pct": retrace,
    }


def load_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return default


def load_v5():
    path = OUT / "latest_dual_scan_v5.csv"
    if not path.exists():
        return {}
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return {row["market"]: row for row in csv.DictReader(handle)}


def point_before(points, current_time, minutes):
    target = current_time - minutes * 60
    eligible = [point for point in points if safe_float(point.get("snapshot_epoch")) is not None and float(point["snapshot_epoch"]) <= target + 120]
    return max(eligible, key=lambda point: float(point["snapshot_epoch"]), default=None)


def delta(current, previous, field):
    current_value = safe_float(current.get(field))
    previous_value = safe_float(previous.get(field)) if previous else None
    return current_value - previous_value if current_value is not None and previous_value is not None else None


def classify_direction(row):
    ratio = safe_float(row.get("buy_sell_ratio_15m"))
    bid_value = safe_float(row.get("bid_trade_value_15m")) or 0
    ask_value = safe_float(row.get("ask_trade_value_15m")) or 0
    ratio_delta = safe_float(row.get("delta_buy_sell_ratio"))
    bid_delta = safe_float(row.get("delta_bid_trade_value_15m"))
    ask_delta = safe_float(row.get("delta_ask_trade_value_15m"))
    value_delta = safe_float(row.get("delta_value_accel_pct"))
    price_delta = safe_float(row.get("delta_price_pct_15m"))

    row["buy_pressure_trend"] = "rising" if (bid_delta or 0) > 0 else "falling" if (bid_delta or 0) < 0 else "flat_or_na"
    row["sell_pressure_trend"] = "rising" if (ask_delta or 0) > 0 else "falling" if (ask_delta or 0) < 0 else "flat_or_na"
    if bid_value > 0 and ask_value == 0:
        row["buy_sell_state"] = "buy_only"
    elif ask_value > 0 and bid_value == 0:
        row["buy_sell_state"] = "sell_only"
    elif ratio is None:
        row["buy_sell_state"] = "unavailable"
    elif ratio >= 1 and (ratio_delta or 0) > 0:
        row["buy_sell_state"] = "buy_advantage_accelerating"
    elif ratio >= 1:
        row["buy_sell_state"] = "buy_advantage_weakening"
    elif (ratio_delta or 0) < 0:
        row["buy_sell_state"] = "sell_advantage_accelerating"
    else:
        row["buy_sell_state"] = "sell_advantage_weakening"

    row["value_accel_direction"] = "accelerating" if (value_delta or 0) > 0 else "decelerating" if (value_delta or 0) < 0 else "flat_or_na"
    if (price_delta or 0) <= 0.5 and (value_delta or 0) > 0 and (ratio_delta or 0) > 0 and (bid_delta or 0) > 0:
        row["flow_signal"] = "leading_buy_inflow"
    elif (price_delta or 0) > 0 and (value_delta or 0) < 0 and (ask_delta or 0) > 0:
        row["flow_signal"] = "rally_weakening"
    else:
        row["flow_signal"] = "neutral"


def apply_gates(row, base_row, prior_points=None):
    high_return = safe_float(row.get("day_high_return_pct")) or 0
    current_return = safe_float(row.get("current_day_return_pct")) or 0
    retrace = safe_float(row.get("gain_retrace_pct")) or 0
    ratio = safe_float(row.get("buy_sell_ratio_15m"))
    ratio_delta = safe_float(row.get("delta_buy_sell_ratio"))
    ask_delta = safe_float(row.get("delta_ask_trade_value_15m"))
    bid_delta = safe_float(row.get("delta_bid_trade_value_15m"))
    value_delta = safe_float(row.get("delta_value_accel_pct"))
    current = safe_float(row.get("price"))
    day_high = safe_float(row.get("day_high"))
    higher_low = bool(base_row.get("higher_low"))

    completed = high_return >= 15 and retrace >= 60 and current_return <= 6
    had_completed = any(point.get("candidate_gate", "").startswith("🔴 1차 급등 완료") for point in (prior_points or []))
    reignition = bool(
        had_completed and higher_low and ratio is not None and ratio >= 1.05 and (ratio_delta or 0) > 0
        and (bid_delta or 0) > 0 and (ask_delta or 0) < 0 and (value_delta or 0) > 0
        and current is not None and day_high is not None and current >= day_high * 0.97
    )
    row["first_surge_retrace_gate"] = completed
    row["first_surge_retrace_seen"] = completed or had_completed
    row["second_ignition_reentry"] = reignition
    if reignition:
        row["candidate_gate"] = "⚡ 2차 시동/재점화"
    elif completed:
        row["candidate_gate"] = "🔴 1차 급등 완료·상승분 반납 / 신규매수 금지"
    else:
        row["candidate_gate"] = "pass"


def compact_point(row):
    fields = [
        "snapshot_kst", "snapshot_epoch", "price", "value_accel_pct",
        "bid_trade_value_15m", "ask_trade_value_15m", "buy_sell_ratio_15m",
        "total_bid_size", "total_ask_size", "orderbook_imbalance",
        "day_high_return_pct", "current_day_return_pct", "drawdown_from_day_high_pct",
        "gain_retrace_pct", "launch_score", "trade_count_15m", "trade_coverage_15m",
        "candidate_gate", "flow_signal",
    ]
    return {field: row.get(field) for field in fields}


def write_csv(path, rows):
    if not rows:
        return
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()), extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def enrich_existing(rows_by_market):
    scan_path = OUT / "latest_scan.json"
    scan = load_json(scan_path, {})
    for base_row in scan.get("rows", []):
        micro = rows_by_market.get(base_row.get("market"))
        if micro:
            base_row.update(micro)
    if scan:
        scan["microstructure_kst"] = max((row["snapshot_kst"] for row in rows_by_market.values()), default=None)
        scan["microstructure_evaluated"] = len(rows_by_market)
        scan_path.write_text(json.dumps(scan, ensure_ascii=False, indent=2), encoding="utf-8")
        write_csv(OUT / "latest_scan.csv", scan.get("rows", []))

    v5_path = OUT / "latest_dual_scan_v5.csv"
    if v5_path.exists():
        with v5_path.open(encoding="utf-8-sig", newline="") as handle:
            v5_rows = list(csv.DictReader(handle))
        for row in v5_rows:
            micro = rows_by_market.get(row.get("market"))
            if micro:
                row.update(micro)
        write_csv(v5_path, v5_rows)


def append_event_archive(rows, snapshot_epoch):
    path = OUT / "signal_event_history.jsonl"
    cutoff = snapshot_epoch - 60 * 24 * 3600
    retained = []
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                item = json.loads(line)
                if safe_float(item.get("snapshot_epoch")) and float(item["snapshot_epoch"]) >= cutoff:
                    retained.append(item)
            except (ValueError, TypeError):
                continue
    for row in rows:
        score = safe_float(row.get("launch_score")) or 0
        value = safe_float(row.get("value_accel_pct")) or 0
        if score >= 14 or value >= 300 or row.get("candidate_gate") != "pass" or row.get("flow_signal") != "neutral":
            retained.append({"market": row["market"], **compact_point(row)})
    path.write_text("\n".join(json.dumps(item, ensure_ascii=False, separators=(",", ":")) for item in retained) + ("\n" if retained else ""), encoding="utf-8")


def main():
    OUT.mkdir(exist_ok=True)
    market_rows = get("/market/all", {"isDetails": "false"})
    markets = [row["market"] for row in market_rows if row["market"].startswith("KRW-")]
    ticker = fetch_batched("/ticker", markets)
    orderbooks = fetch_batched("/orderbook", markets)
    v5 = load_v5()
    scan = load_json(OUT / "latest_scan.json", {})
    base = {row["market"]: row for row in scan.get("rows", [])}
    history_path = OUT / "microstructure_history.json"
    history = load_json(history_path, {"markets": {}})
    history_markets = history.setdefault("markets", {})
    now = datetime.now(KST)
    now_ms = int(now.timestamp() * 1000)
    now_epoch = int(now.timestamp())
    snapshot_kst = now.isoformat()
    rows = []
    errors = []

    for index, market in enumerate(markets, 1):
        try:
            trade = fetch_trades_15m(market, now_ms)
            row = {
                "market": market,
                "snapshot_kst": snapshot_kst,
                "snapshot_epoch": now_epoch,
                "price": safe_float(ticker.get(market, {}).get("trade_price")),
                "value_accel_pct": safe_float(v5.get(market, {}).get("value_accel_pct")),
                "launch_score": safe_float(v5.get(market, {}).get("launch_score")),
                **trade,
                **orderbook_values(orderbooks.get(market, {})),
                **day_path(ticker.get(market, {})),
            }
            points = history_markets.get(market, [])
            for minutes in (15, 30, 45, 60):
                old = point_before(points, now_epoch, minutes)
                row[f"buy_sell_ratio_change_{minutes}m"] = delta(row, old, "buy_sell_ratio_15m")
                row[f"bid_trade_value_change_{minutes}m"] = pct_change(row.get("bid_trade_value_15m"), safe_float(old.get("bid_trade_value_15m")) if old else None)
                row[f"ask_trade_value_change_{minutes}m"] = pct_change(row.get("ask_trade_value_15m"), safe_float(old.get("ask_trade_value_15m")) if old else None)
                row[f"value_accel_change_{minutes}m"] = delta(row, old, "value_accel_pct")
                row[f"price_change_{minutes}m"] = pct_change(row.get("price"), safe_float(old.get("price")) if old else None)
            previous = max(points, key=lambda point: safe_float(point.get("snapshot_epoch")) or 0, default=None)
            row["delta_buy_sell_ratio"] = delta(row, previous, "buy_sell_ratio_15m")
            row["delta_bid_trade_value_15m"] = delta(row, previous, "bid_trade_value_15m")
            row["delta_ask_trade_value_15m"] = delta(row, previous, "ask_trade_value_15m")
            row["delta_value_accel_pct"] = delta(row, previous, "value_accel_pct")
            row["delta_price_pct_15m"] = pct_change(row.get("price"), safe_float(previous.get("price")) if previous else None)
            classify_direction(row)
            apply_gates(row, base.get(market, {}), points)
            rows.append(row)
            points.append(compact_point(row))
            history_markets[market] = points[-HISTORY_POINTS:]
        except Exception as exc:
            errors.append({"market": market, "error": str(exc)})
        if index % 10 == 0:
            print(f"microstructure {index}/{len(markets)}", flush=True)
        time.sleep(0.13)

    rows.sort(key=lambda row: row["market"])
    history["updated_kst"] = snapshot_kst
    history["cadence_minutes"] = WINDOW_MINUTES
    history["retained_points_per_market"] = HISTORY_POINTS
    history_path.write_text(json.dumps(history, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    write_csv(OUT / "latest_microstructure.csv", rows)
    latest = {
        "kst": snapshot_kst,
        "market_count": len(markets),
        "evaluated": len(rows),
        "errors": errors,
        "source": {
            "trades": "/v1/trades/ticks ask_bid (BID/ASK)",
            "orderbook": "/v1/orderbook total_bid_size/total_ask_size",
            "ticker": "/v1/ticker opening_price/high_price/trade_price",
            "trade_window_minutes": WINDOW_MINUTES,
            "max_trade_pages": MAX_TRADE_PAGES,
            "estimation_used": False,
        },
        "coverage": {
            "complete_15m": sum(row["trade_coverage_15m"] == "complete" for row in rows),
            "partial_15m": sum(row["trade_coverage_15m"] == "partial" for row in rows),
        },
        "rows": rows,
    }
    (OUT / "latest_microstructure.json").write_text(json.dumps(latest, ensure_ascii=False, indent=2), encoding="utf-8")
    append_event_archive(rows, now_epoch)
    enrich_existing({row["market"]: {key: value for key, value in row.items() if key != "market"} for row in rows})
    print(json.dumps({"kst": snapshot_kst, "market_count": len(markets), "evaluated": len(rows), "errors": len(errors), **latest["coverage"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
