#!/usr/bin/env python3
from __future__ import annotations

import csv
import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import second_scan_api as ss

KST = timezone(timedelta(hours=9))
OUT = Path("success16_trailing_output")

CASES = [
    ("ANKR", "KRW-ANKR", "2026-09-03T09:00:03+09:00", 5.60),
    ("BONK", "KRW-BONK", "2026-09-01T00:22:49+09:00", 0.00540),
    ("CHIP", "KRW-CHIP", "2026-09-03T16:41:07+09:00", 68.7),
    ("CRV", "KRW-CRV", "2026-09-01T20:56:12+09:00", 516.0),
    ("DRV", "KRW-DRV", "2026-09-05T09:00:06+09:00", 233.0),
    ("EGLD", "KRW-EGLD", "2026-09-02T13:57:46+09:00", 5900.0),
    ("HIVE", "KRW-HIVE", "2026-09-03T09:06:23+09:00", 62.8),
    ("ICX", "KRW-ICX", "2026-09-01T09:30:32+09:00", 21.4),
    ("INJ", "KRW-INJ", "2026-09-02T16:13:45+09:00", 6270.0),
    ("IQ", "KRW-IQ", "2026-09-01T09:04:31+09:00", 0.930),
    ("MOC", "KRW-MOC", "2026-09-02T12:41:13+09:00", 27.2),
    ("SC", "KRW-SC", "2026-09-01T09:00:08+09:00", 0.834),
    ("SNT", "KRW-SNT", "2026-09-03T09:06:50+09:00", 9.17),
    ("SOPH", "KRW-SOPH", "2026-09-02T09:00:06+09:00", 5.73),
    ("T", "KRW-T", "2026-09-02T09:05:11+09:00", 5.24),
    ("SOPH_NEW", "KRW-SOPH", "2026-09-05T21:05:27+09:00", 6.22),
]


def minute_rows(market: str, day: str) -> list[dict]:
    start = datetime.fromisoformat(day).replace(tzinfo=KST)
    end = start + timedelta(days=1)
    cursor = end
    out: dict[int, dict] = {}
    for _ in range(20):
        rows = ss.http_json("/candles/minutes/1", {"market": market, "to": ss.iso_z(cursor), "count": 200})
        if not rows:
            break
        oldest = None
        for row in rows:
            dt = datetime.fromisoformat(row["candle_date_time_kst"]).replace(tzinfo=KST)
            oldest = dt if oldest is None or dt < oldest else oldest
            if start <= dt < end:
                out[int(dt.timestamp())] = row
        if oldest is None or oldest <= start:
            break
        cursor = oldest
        time.sleep(ss.RATE_SLEEP)
    return [out[k] for k in sorted(out)]


def main() -> None:
    OUT.mkdir(exist_ok=True)
    all_rows: list[dict] = []
    manifest: list[dict] = []
    for index, (label, market, stage6_iso, stage6_price) in enumerate(CASES, 1):
        stage6 = datetime.fromisoformat(stage6_iso)
        minutes = minute_rows(market, stage6.date().isoformat())
        after = [r for r in minutes if datetime.fromisoformat(r["candle_date_time_kst"]).replace(tzinfo=KST) >= stage6]
        if not after:
            manifest.append({"label": label, "market": market, "error": "no minute rows"})
            continue
        day_high = max(float(r["high_price"]) for r in after)
        high_minute_row = next(r for r in after if float(r["high_price"]) == day_high)
        high_minute = datetime.fromisoformat(high_minute_row["candle_date_time_kst"]).replace(tzinfo=KST)
        # Continue well beyond the first daily high so trailing exits are observable.
        end = min(high_minute + timedelta(hours=2), stage6.replace(hour=23, minute=59, second=59))
        seconds = ss.fetch_second_candles(market, stage6, end)
        for row in seconds:
            epoch_sec = int(row["timestamp"]) // 1000
            all_rows.append({
                "sample": label,
                "market": market,
                "stage6_kst": stage6.isoformat(),
                "stage6_price": stage6_price,
                "timestamp_kst": datetime.fromtimestamp(epoch_sec, KST).isoformat(),
                "epoch_sec": epoch_sec,
                "open": row["opening_price"],
                "high": row["high_price"],
                "low": row["low_price"],
                "close": row["trade_price"],
                "value_1s_krw": row.get("candle_acc_trade_price"),
                "volume_1s": row.get("candle_acc_trade_volume"),
            })
        manifest.append({
            "label": label, "market": market, "stage6_kst": stage6.isoformat(),
            "stage6_price": stage6_price, "day_high": day_high,
            "day_high_minute_kst": high_minute.isoformat(), "scan_end_kst": end.isoformat(),
            "seconds_with_trades": len(seconds),
        })
        print(f"[{index}/{len(CASES)}] {label} {stage6.isoformat()} -> {end.isoformat()} rows={len(seconds)}", flush=True)

    if all_rows:
        with (OUT / "success16_stage6_to_high_seconds.csv").open("w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(all_rows[0]))
            writer.writeheader(); writer.writerows(all_rows)
    (OUT / "success16_stage6_to_high_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps({"cases": len(manifest), "rows": len(all_rows)}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
