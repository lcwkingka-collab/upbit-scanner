#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Compact archiver for the isolated local Upbit Live API.

Reads localhost only; never touches realtime_radar_v51.py or Telegram.
Writes one gzip JSONL file per KST hour under runtime/archive/YYYYMMDD/.
One row per market per completed minute, deduplicated by (market, minute).
"""
from __future__ import annotations

import gzip
import json
import os
import signal
import time
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, Tuple

API = os.getenv("LIVE_API_URL", "http://127.0.0.1:8787")
ARCHIVE_ROOT = Path(os.getenv("LIVE_ARCHIVE_ROOT", "/home/ubuntu/upbit-scanner/runtime/archive"))
POLL_SEC = int(os.getenv("LIVE_ARCHIVE_POLL_SEC", "20"))
KST = timezone(timedelta(hours=9))
STOP = False
LAST: Dict[str, int] = {}


def shutdown(signum=None, frame=None):
    global STOP
    STOP = True


def get_json(path: str):
    req = urllib.request.Request(API + path, headers={"User-Agent": "upbit-live-archiver/1.0"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.load(r)


def archive_path(minute_epoch: int) -> Path:
    dt = datetime.fromtimestamp(minute_epoch, KST)
    folder = ARCHIVE_ROOT / dt.strftime("%Y%m%d")
    folder.mkdir(parents=True, exist_ok=True)
    return folder / f"features_{dt.strftime('%Y%m%d_%H')}.jsonl.gz"


def append_row(row: dict) -> None:
    path = archive_path(int(row["minute"]))
    raw = (json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
    with gzip.open(path, "ab", compresslevel=6) as f:
        f.write(raw)


def collect_once() -> Tuple[int, int]:
    live = get_json("/live?limit=200")
    rows = live.get("rows", [])
    markets = [r.get("market") for r in rows if r.get("market")]
    written = 0
    checked = 0
    for market in markets:
        ticker = market.split("-", 1)[-1]
        try:
            detail = get_json(f"/market/{ticker}")
        except Exception:
            continue
        checked += 1
        history = detail.get("history") or []
        # API keeps completed minutes in history. Only append unseen minutes.
        for feat in history:
            minute = int(feat.get("minute") or 0)
            if minute <= 0 or minute <= LAST.get(market, 0):
                continue
            row = {
                "market": market,
                "ticker": detail.get("ticker"),
                "korean_name": detail.get("korean_name"),
                "english_name": detail.get("english_name"),
                **feat,
            }
            append_row(row)
            LAST[market] = minute
            written += 1
    return checked, written


def bootstrap_last() -> None:
    # Restart-safe enough for compact runtime: inspect only today's/latest files.
    if not ARCHIVE_ROOT.exists():
        return
    files = sorted(ARCHIVE_ROOT.glob("*/*.jsonl.gz"))[-3:]
    for path in files:
        try:
            with gzip.open(path, "rt", encoding="utf-8") as f:
                for line in f:
                    try:
                        row = json.loads(line)
                        market = row.get("market")
                        minute = int(row.get("minute") or 0)
                        if market and minute > LAST.get(market, 0):
                            LAST[market] = minute
                    except Exception:
                        pass
        except Exception:
            pass


def main():
    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)
    ARCHIVE_ROOT.mkdir(parents=True, exist_ok=True)
    bootstrap_last()
    print(f"[archiver] API={API} root={ARCHIVE_ROOT} poll={POLL_SEC}s", flush=True)
    while not STOP:
        started = time.time()
        try:
            checked, written = collect_once()
            print(f"[archiver] checked={checked} written={written}", flush=True)
        except Exception as exc:
            print(f"[archiver error] {exc}", flush=True)
        delay = max(1.0, POLL_SEC - (time.time() - started))
        end = time.time() + delay
        while not STOP and time.time() < end:
            time.sleep(min(1.0, end - time.time()))


if __name__ == "__main__":
    main()
