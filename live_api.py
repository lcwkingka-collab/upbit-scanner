#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Isolated Upbit live collector + local read-only API.

IMPORTANT: This process is independent from realtime_radar_v51.py.
It does not send Telegram alerts and does not share radar memory.

Endpoints (localhost only by default):
  GET /health
  GET /live?limit=30
  GET /market/ANKR

Data:
  - separate Upbit WebSocket subscription
  - per-market current 1-minute bucket
  - recent completed 1-minute feature history (default 60 minutes)
  - atomic local snapshot: runtime/live_state.json
"""
from __future__ import annotations

import json
import os
import signal
import threading
import time
import urllib.request
import uuid
from collections import defaultdict, deque
from dataclasses import asdict, dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Deque, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

try:
    import websocket
except ImportError as exc:
    raise SystemExit("websocket-client가 필요합니다: pip install websocket-client") from exc

UPBIT_REST = "https://api.upbit.com/v1"
UPBIT_WS = "wss://api.upbit.com/websocket/v1"
HOST = os.getenv("LIVE_API_HOST", "127.0.0.1")
PORT = int(os.getenv("LIVE_API_PORT", "8787"))
HISTORY_MINUTES = int(os.getenv("LIVE_HISTORY_MINUTES", "60"))
SNAPSHOT_INTERVAL = float(os.getenv("LIVE_SNAPSHOT_INTERVAL", "5"))
RECONNECT_DELAY = 3.0
SNAPSHOT_PATH = Path(os.getenv("LIVE_SNAPSHOT_PATH", "/home/ubuntu/upbit-scanner/runtime/live_state.json"))
STABLE_MARKETS = {"KRW-USDT", "KRW-USDC", "KRW-DAI", "KRW-USDE"}

STOP = threading.Event()
LOCK = threading.RLock()
STARTED_AT = time.time()
LAST_TRADE_AT = 0.0
LAST_WS_OPEN_AT = 0.0
NAMES: Dict[str, Tuple[str, str]] = {}
MARKETS: List[str] = []


@dataclass
class MinuteBucket:
    minute: int
    open: Optional[float] = None
    high: Optional[float] = None
    low: Optional[float] = None
    close: Optional[float] = None
    value: float = 0.0
    bid_value: float = 0.0
    ask_value: float = 0.0
    trades: int = 0

    def add(self, price: float, volume: float, side: str) -> None:
        value = price * volume
        if self.open is None:
            self.open = price
            self.high = price
            self.low = price
        self.high = max(self.high if self.high is not None else price, price)
        self.low = min(self.low if self.low is not None else price, price)
        self.close = price
        self.value += value
        if side == "BID":
            self.bid_value += value
        elif side == "ASK":
            self.ask_value += value
        self.trades += 1

    def feature(self) -> dict:
        total = self.bid_value + self.ask_value
        bid_ratio = self.bid_value / total if total > 0 else 0.5
        ret = 0.0
        if self.open not in (None, 0) and self.close is not None:
            ret = (self.close / self.open - 1.0) * 100.0
        return {
            "minute": self.minute,
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "return_pct": ret,
            "value_krw": self.value,
            "bid_value_krw": self.bid_value,
            "ask_value_krw": self.ask_value,
            "net_buy_krw": self.bid_value - self.ask_value,
            "bid_ratio": bid_ratio,
            "trades": self.trades,
        }


CURRENT: Dict[str, MinuteBucket] = {}
HISTORY: Dict[str, Deque[dict]] = defaultdict(lambda: deque(maxlen=HISTORY_MINUTES))


def http_json(url: str):
    req = urllib.request.Request(url, headers={"User-Agent": "upbit-live-api/1.0"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.load(resp)


def fetch_markets() -> None:
    global MARKETS
    rows = http_json(f"{UPBIT_REST}/market/all?is_details=false")
    markets = []
    for row in rows:
        market = str(row.get("market", ""))
        if not market.startswith("KRW-") or market in STABLE_MARKETS:
            continue
        ticker = market.split("-", 1)[-1]
        NAMES[market] = (
            str(row.get("english_name") or ticker),
            str(row.get("korean_name") or ticker),
        )
        markets.append(market)
    MARKETS = sorted(markets)
    print(f"[live-api] loaded {len(MARKETS)} KRW markets", flush=True)


def finalize_old_bucket(market: str, new_minute: int) -> None:
    old = CURRENT.get(market)
    if old is not None and old.minute < new_minute and old.trades > 0:
        HISTORY[market].append(old.feature())


def add_trade(row: dict) -> None:
    global LAST_TRADE_AT
    market = row.get("code")
    if market not in NAMES:
        return
    try:
        price = float(row["trade_price"])
        volume = float(row["trade_volume"])
        ts_ms = int(row.get("timestamp") or time.time() * 1000)
    except (KeyError, TypeError, ValueError):
        return
    minute = (ts_ms // 1000) // 60 * 60
    side = str(row.get("ask_bid") or "")
    with LOCK:
        bucket = CURRENT.get(market)
        if bucket is None or bucket.minute != minute:
            finalize_old_bucket(market, minute)
            bucket = MinuteBucket(minute=minute)
            CURRENT[market] = bucket
        bucket.add(price, volume, side)
        LAST_TRADE_AT = time.time()


def latest_feature(market: str) -> Optional[dict]:
    bucket = CURRENT.get(market)
    if bucket is None or bucket.trades <= 0:
        return None
    return bucket.feature()


def market_payload(market: str) -> Optional[dict]:
    with LOCK:
        if market not in NAMES:
            return None
        current = latest_feature(market)
        history = list(HISTORY.get(market, ()))
        english, korean = NAMES[market]
    if current is None and not history:
        return {
            "market": market,
            "ticker": market.split("-", 1)[-1],
            "english_name": english,
            "korean_name": korean,
            "current": None,
            "history": [],
        }
    recent_values = [x["value_krw"] for x in history[-10:] if x["value_krw"] > 0]
    baseline10 = sum(recent_values) / len(recent_values) if recent_values else 0.0
    current_x = current["value_krw"] / baseline10 if current and baseline10 > 0 else None
    return {
        "market": market,
        "ticker": market.split("-", 1)[-1],
        "english_name": english,
        "korean_name": korean,
        "current": current,
        "baseline_10m_value_krw": baseline10,
        "current_vs_10m_x": current_x,
        "history": history,
    }


def live_payload(limit: int = 30) -> dict:
    rows = []
    with LOCK:
        markets = list(MARKETS)
    for market in markets:
        item = market_payload(market)
        if not item or not item.get("current"):
            continue
        cur = item["current"]
        rows.append({
            "market": market,
            "ticker": item["ticker"],
            "korean_name": item["korean_name"],
            "english_name": item["english_name"],
            "price": cur["close"],
            "return_1m_pct": cur["return_pct"],
            "value_1m_krw": cur["value_krw"],
            "bid_ratio_1m": cur["bid_ratio"],
            "net_buy_1m_krw": cur["net_buy_krw"],
            "trades_1m": cur["trades"],
            "value_vs_10m_x": item["current_vs_10m_x"],
        })
    rows.sort(key=lambda x: (x["value_vs_10m_x"] or 0.0, x["value_1m_krw"]), reverse=True)
    return {
        "generated_at": time.time(),
        "markets": len(MARKETS),
        "active_markets": len(rows),
        "rows": rows[:max(1, min(limit, 200))],
    }


def health_payload() -> dict:
    now = time.time()
    return {
        "ok": bool(MARKETS) and LAST_TRADE_AT > 0 and now - LAST_TRADE_AT < 30,
        "service": "upbit-live-api",
        "uptime_sec": int(now - STARTED_AT),
        "markets": len(MARKETS),
        "last_trade_age_sec": None if LAST_TRADE_AT <= 0 else round(now - LAST_TRADE_AT, 3),
        "last_ws_open_age_sec": None if LAST_WS_OPEN_AT <= 0 else round(now - LAST_WS_OPEN_AT, 3),
        "bind": f"{HOST}:{PORT}",
    }


def write_snapshot() -> None:
    payload = live_payload(limit=200)
    payload["health"] = health_payload()
    SNAPSHOT_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = SNAPSHOT_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    os.replace(tmp, SNAPSHOT_PATH)


def snapshot_loop() -> None:
    while not STOP.wait(SNAPSHOT_INTERVAL):
        try:
            write_snapshot()
        except Exception as exc:
            print(f"[snapshot error] {exc}", flush=True)


class Handler(BaseHTTPRequestHandler):
    server_version = "UpbitLiveAPI/1.0"

    def log_message(self, fmt, *args):
        print("[http] " + (fmt % args), flush=True)

    def send_json(self, status: int, payload: dict) -> None:
        raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/health":
            self.send_json(200, health_payload())
            return
        if parsed.path == "/live":
            qs = parse_qs(parsed.query)
            try:
                limit = int(qs.get("limit", ["30"])[0])
            except ValueError:
                limit = 30
            self.send_json(200, live_payload(limit))
            return
        if parsed.path.startswith("/market/"):
            ticker = parsed.path.split("/market/", 1)[1].strip().upper()
            market = ticker if ticker.startswith("KRW-") else f"KRW-{ticker}"
            payload = market_payload(market)
            if payload is None:
                self.send_json(404, {"error": "market_not_found", "market": market})
            else:
                self.send_json(200, payload)
            return
        self.send_json(404, {"error": "not_found", "endpoints": ["/health", "/live?limit=30", "/market/ANKR"]})


def on_open(ws) -> None:
    global LAST_WS_OPEN_AT
    LAST_WS_OPEN_AT = time.time()
    payload = [
        {"ticket": str(uuid.uuid4())},
        {"type": "trade", "codes": MARKETS, "is_only_realtime": True},
        {"format": "DEFAULT"},
    ]
    ws.send(json.dumps(payload))
    print(f"[websocket] live-api subscribed {len(MARKETS)} markets", flush=True)


def on_message(ws, message) -> None:
    try:
        if isinstance(message, bytes):
            message = message.decode("utf-8")
        add_trade(json.loads(message))
    except Exception as exc:
        print(f"[message error] {exc}", flush=True)


def on_error(ws, error) -> None:
    print(f"[websocket error] {error}", flush=True)


def on_close(ws, status_code, message) -> None:
    print(f"[websocket closed] code={status_code} msg={message}", flush=True)


def websocket_loop() -> None:
    while not STOP.is_set():
        ws = websocket.WebSocketApp(
            UPBIT_WS,
            on_open=on_open,
            on_message=on_message,
            on_error=on_error,
            on_close=on_close,
        )
        try:
            ws.run_forever(ping_interval=30, ping_timeout=10)
        except Exception as exc:
            print(f"[websocket run error] {exc}", flush=True)
        if not STOP.is_set():
            STOP.wait(RECONNECT_DELAY)


def shutdown(signum=None, frame=None) -> None:
    STOP.set()


def main() -> None:
    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)
    fetch_markets()

    snapshot_thread = threading.Thread(target=snapshot_loop, name="live-snapshot", daemon=True)
    snapshot_thread.start()

    ws_thread = threading.Thread(target=websocket_loop, name="live-websocket", daemon=True)
    ws_thread.start()

    server = ThreadingHTTPServer((HOST, PORT), Handler)
    server.timeout = 1.0
    print(f"[live-api] listening on http://{HOST}:{PORT}", flush=True)
    print(f"[live-api] snapshot -> {SNAPSHOT_PATH}", flush=True)
    try:
        while not STOP.is_set():
            server.handle_request()
    finally:
        STOP.set()
        server.server_close()
        ws_thread.join(timeout=3)
        snapshot_thread.join(timeout=3)
        try:
            write_snapshot()
        except Exception:
            pass


if __name__ == "__main__":
    main()
