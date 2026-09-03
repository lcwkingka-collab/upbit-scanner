import csv
import math
import time
import urllib.parse
import urllib.request
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE = "https://api.upbit.com/v1"
OUT = Path("data")


def get(path, params=None, tries=5):
    url = BASE + path + ("?" + urllib.parse.urlencode(params) if params else "")
    err = None
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "upbit-scanner/1.0"})
            with urllib.request.urlopen(req, timeout=30) as response:
                return json.load(response)
        except Exception as exc:
            err = exc
            time.sleep(0.7 * (attempt + 1))
    raise err


def avg(values, n, end=None):
    end = len(values) if end is None else end
    part = values[end - n:end]
    return sum(part) / n


def slope(values):
    n = len(values)
    mx = (n - 1) / 2
    my = sum(values) / n
    den = sum((i - mx) ** 2 for i in range(n))
    return sum((i - mx) * (v - my) for i, v in enumerate(values)) / den


def forecast(gaps, side):
    current, trend = gaps[-1], slope(gaps)
    if side == "up":
        if not (current > 0 and trend < 0):
            return None
        days = current / -trend
    else:
        if not (current < 0 and trend > 0):
            return None
        days = -current / trend
    return days if 0 < days <= 7.5 else None


def launch_score(row):
    score = 0
    score += 2 if row["ma15"] > row["ma50"] else 0
    score += 2 if row["ma50"] < row["ma120"] else 0
    score += 1 if row["slope15"] > 0.4 else 0
    score += 1 if row["slope15"] > 1 else 0
    score += 2 if 0 < row["gap120"] <= 8 else 1 if 8 < row["gap120"] <= 15 else 0
    score += 2 if row["conv120"] < 0 else 0
    score += 2 if row["f120up"] is not None else 0
    score += 1 if row["value_accel_pct"] > 20 else 0
    score += 1 if row["value_accel_pct"] > 80 else 0
    score += 1 if -3 < row["ret5_pct"] < 12 else 0
    score -= 2 if row["ret5_pct"] >= 18 else 0
    return score


def pct(a, b):
    return ((a / b) - 1) * 100 if b else None


def main():
    markets = [m for m in get("/market/all", {"isDetails": "false"}) if m["market"].startswith("KRW-")]
    v5_rows, v4_events, errors = [], [], []
    for index, market in enumerate(markets, 1):
        try:
            candles = list(reversed(get("/candles/days", {"market": market["market"], "count": 200})))
            close = [x["trade_price"] for x in candles]
            value = [x["candle_acc_trade_price"] for x in candles]
            dates = [x["candle_date_time_kst"][:10] for x in candles]
            n = len(close)

            if n >= 127:
                ma15, ma50, ma120 = avg(close, 15), avg(close, 50), avg(close, 120)
                p15, p50, p120 = avg(close, 15, n - 1), avg(close, 50, n - 1), avg(close, 120, n - 1)
                g50, g120 = [], []
                for offset in range(4, -1, -1):
                    end = n - offset
                    a15, a50, a120 = avg(close, 15, end), avg(close, 50, end), avg(close, 120, end)
                    g50.append((a50 - a15) / a50 * 100)
                    g120.append((a120 - a15) / a120 * 100)
                v5 = avg(value, 5)
                vprev = avg(value, 5, n - 5)
                row = {
                    "market": market["market"], "name": market["korean_name"], "price": close[-1],
                    "ma15": ma15, "ma50": ma50, "ma120": ma120,
                    "slope15": pct(ma15, p15), "gap50": g50[-1], "gap120": g120[-1],
                    "conv50": slope(g50), "conv120": slope(g120),
                    "f50up": forecast(g50, "up"), "f50down": forecast(g50, "down"),
                    "f120up": forecast(g120, "up"), "f120down": forecast(g120, "down"),
                    "golden_now": p15 <= p50 and ma15 > ma50,
                    "dead_now": p15 >= p50 and ma15 < ma50,
                    "up120_now": p15 <= p120 and ma15 > ma120,
                    "down120_now": p15 >= p120 and ma15 < ma120,
                    "value_accel_pct": pct(v5, vprev), "ret5_pct": pct(close[-1], close[-6]),
                }
                row["launch_score"] = launch_score(row)
                row["launch_grade"] = "A급" if row["launch_score"] >= 10 else "B급" if row["launch_score"] >= 7 else "관찰" if row["launch_score"] >= 5 else "-"
                v5_rows.append(row)

            if n >= 125:
                for i in range(120, n):
                    ma15, ma120 = avg(close, 15, i + 1), avg(close, 120, i + 1)
                    p15, p120 = avg(close, 15, i), avg(close, 120, i)
                    if not (p15 <= p120 and ma15 > ma120):
                        continue
                    ma50, entry = avg(close, 50, i + 1), close[i]
                    prev5, cur5 = value[max(0, i - 5):i], value[max(0, i - 4):i + 1]
                    event = {
                        "market": market["market"], "name": market["korean_name"], "date": dates[i],
                        "entry": entry, "ma15": ma15, "ma50": ma50, "ma120": ma120,
                        "ma15_above_50": ma15 > ma50, "ma50_above_120": ma50 > ma120,
                        "value_accel_pct": pct(sum(cur5) / len(cur5), sum(prev5) / len(prev5)) if prev5 else None,
                    }
                    for days in (3, 7, 14, 30):
                        future = close[i + 1:min(n, i + days + 1)]
                        event[f"ret{days}_pct"] = pct(max(future), entry) if future else None
                        event[f"dd{days}_pct"] = pct(min(future), entry) if future else None
                    v4_events.append(event)
        except Exception as exc:
            errors.append({"market": market["market"], "error": str(exc)})
        print(f"extended {index}/{len(markets)}", flush=True)
        time.sleep(0.12)

    OUT.mkdir(exist_ok=True)
    if v5_rows:
        with (OUT / "latest_dual_scan_v5.csv").open("w", newline="", encoding="utf-8-sig") as file:
            writer = csv.DictWriter(file, fieldnames=v5_rows[0].keys())
            writer.writeheader(); writer.writerows(v5_rows)
    if v4_events:
        with (OUT / "latest_ma120_backtest_v4.csv").open("w", newline="", encoding="utf-8-sig") as file:
            writer = csv.DictWriter(file, fieldnames=v4_events[0].keys())
            writer.writeheader(); writer.writerows(v4_events)

    kst = datetime.now(timezone(timedelta(hours=9))).isoformat()
    summary = {"kst": kst, "market_count": len(markets), "v5_evaluated": len(v5_rows), "v4_events": len(v4_events), "errors": errors}
    (OUT / "latest_extended_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    main()
