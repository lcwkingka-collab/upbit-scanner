#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Upbit KRW Real-time Radar V5
- 1-second evaluation
- 1~10 second multi-window trade-value acceleration
- Stage 1 gate: normal baseline >= 2.0x AND previous same window >= 2.5x
- Condition-based stage escalation (no fixed timers)
- Direction confirmation gets stricter at stages 2/3
- State-based acceleration death / re-arm
- Telegram top line: English / Korean (KRW-TICKER)

Requires:
    pip install websocket-client

Environment:
    TELEGRAM_BOT_TOKEN
    TELEGRAM_CHAT_ID
"""

from __future__ import annotations

import json
import math
import os
import signal
import statistics
import threading
import time
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

try:
    import websocket
except ImportError as exc:
    raise SystemExit(
        "websocket-client가 필요합니다. .venv 활성화 후 "
        "`pip install websocket-client` 실행 후 다시 시작하세요."
    ) from exc


UPBIT_REST = "https://api.upbit.com/v1"
UPBIT_WS = "wss://api.upbit.com/websocket/v1"

WINDOWS = tuple(range(1, 11))
WARMUP_SEC = 300
KEEP_SEC = 720
BASELINE_START_SEC = 300
BASELINE_EXCLUDE_RECENT_SEC = 30

STAGE1_BASELINE_X = 2.0
STAGE1_PREV_X = 2.5

# True stablecoins only. CHIP / USD.AI is intentionally NOT excluded.
STABLE_MARKETS = {
    "KRW-USDT",
    "KRW-USDC",
    "KRW-DAI",
    "KRW-USDE",
}

EVAL_INTERVAL = 1.0
RECONNECT_DELAY = 3.0

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

STOP = threading.Event()
LOCK = threading.RLock()
STARTED_AT = time.time()


@dataclass
class Bucket:
    value: float = 0.0
    bid: float = 0.0
    ask: float = 0.0
    count: int = 0
    last_price: Optional[float] = None


@dataclass
class CoinState:
    stage: int = 0
    cycle_id: int = 0
    last_stage_at: float = 0.0
    last_score: float = 0.0
    last_best_window: int = 0
    last_price: Optional[float] = None
    cancelled_cycle_id: int = -1


BUCKETS: Dict[str, Dict[int, Bucket]] = {}
STATES: Dict[str, CoinState] = {}
NAMES: Dict[str, Tuple[str, str]] = {}
MARKETS: List[str] = []


def http_json(url: str, data: Optional[dict] = None, timeout: int = 15):
    headers = {"User-Agent": "upbit-radar-v5/1.0"}
    body = None
    if data is not None:
        body = urllib.parse.urlencode(data).encode("utf-8")
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    req = urllib.request.Request(url, data=body, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)


def telegram(text: str) -> None:
    if not BOT_TOKEN or not CHAT_ID:
        print("[telegram disabled]", text.replace("\n", " | "), flush=True)
        return
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "text": text,
        "disable_web_page_preview": "true",
    }
    try:
        http_json(url, payload, timeout=10)
    except Exception as exc:
        print(f"[telegram error] {exc}", flush=True)


def fetch_markets() -> None:
    global MARKETS
    rows = http_json(f"{UPBIT_REST}/market/all?is_details=false")
    markets: List[str] = []
    for row in rows:
        market = str(row.get("market", ""))
        if not market.startswith("KRW-"):
            continue
        if market in STABLE_MARKETS:
            continue
        english = str(row.get("english_name") or market.split("-", 1)[1])
        korean = str(row.get("korean_name") or english)
        NAMES[market] = (english, korean)
        markets.append(market)
        BUCKETS.setdefault(market, {})
        STATES.setdefault(market, CoinState())
    MARKETS = sorted(markets)
    print(f"[markets] KRW {len(MARKETS)} markets loaded (stablecoins excluded)", flush=True)


def market_title(market: str) -> str:
    english, korean = NAMES.get(market, (market.split("-", 1)[-1], market))
    ticker = market.split("-", 1)[-1]
    return f"{ticker} / {korean} · {english} ({market})"


def add_trade(row: dict) -> None:
    market = row.get("code")
    if market not in NAMES:
        return

    try:
        price = float(row["trade_price"])
        volume = float(row["trade_volume"])
        ts_ms = int(row.get("timestamp") or time.time() * 1000)
    except (KeyError, TypeError, ValueError):
        return

    sec = ts_ms // 1000
    value = price * volume
    side = str(row.get("ask_bid") or "")

    with LOCK:
        market_buckets = BUCKETS[market]
        bucket = market_buckets.get(sec)
        if bucket is None:
            bucket = Bucket()
            market_buckets[sec] = bucket

        bucket.value += value
        if side == "BID":
            bucket.bid += value
        elif side == "ASK":
            bucket.ask += value
        bucket.count += 1
        bucket.last_price = price

        cutoff = int(time.time()) - KEEP_SEC
        stale = [key for key in market_buckets.keys() if key < cutoff]
        for key in stale:
            market_buckets.pop(key, None)


def sum_range(buckets: Dict[int, Bucket], start_sec: int, end_sec: int) -> dict:
    total = bid = ask = 0.0
    count = 0
    last_price = None
    last_sec = -1

    for sec in range(start_sec, end_sec + 1):
        b = buckets.get(sec)
        if b is None:
            continue
        total += b.value
        bid += b.bid
        ask += b.ask
        count += b.count
        if b.last_price is not None and sec >= last_sec:
            last_sec = sec
            last_price = b.last_price

    return {
        "value": total,
        "bid": bid,
        "ask": ask,
        "count": count,
        "last_price": last_price,
    }


def last_price_before(buckets: Dict[int, Bucket], sec: int, lookback: int = 120) -> Optional[float]:
    for t in range(sec, sec - lookback - 1, -1):
        b = buckets.get(t)
        if b and b.last_price is not None:
            return b.last_price
    return None


def safe_ratio(num: float, den: float) -> float:
    if den <= 0:
        return 0.0
    return num / den


def bid_share(flow: dict) -> float:
    total = flow["bid"] + flow["ask"]
    if total <= 0:
        return 0.5
    return flow["bid"] / total


def baseline_for_window(buckets: Dict[int, Bucket], now_sec: int, window: int) -> float:
    """
    Robust baseline for the same window length.
    Uses older history only, excluding the latest 30 sec.
    A mean floor prevents tiny/zero previous windows from creating absurd ratios.
    """
    newest_end = now_sec - BASELINE_EXCLUDE_RECENT_SEC
    oldest_end = now_sec - BASELINE_START_SEC

    samples: List[float] = []
    end = newest_end
    step = max(1, window)
    while end >= oldest_end:
        start = end - window + 1
        samples.append(sum_range(buckets, start, end)["value"])
        end -= step

    if not samples:
        return 0.0

    mean_value = statistics.fmean(samples)
    nonzero = [x for x in samples if x > 0]
    if nonzero:
        median_nonzero = statistics.median(nonzero)
        robust = max(mean_value, median_nonzero * 0.35)
    else:
        robust = mean_value

    return max(robust, 1.0)


def window_metric(buckets: Dict[int, Bucket], now_sec: int, window: int) -> dict:
    current = sum_range(buckets, now_sec - window + 1, now_sec)
    previous = sum_range(buckets, now_sec - 2 * window + 1, now_sec - window)
    baseline = baseline_for_window(buckets, now_sec, window)

    # Denominator floor is tied to that coin's own baseline, not a fixed KRW amount.
    prev_den = max(previous["value"], baseline * 0.25, 1.0)
    base_x = safe_ratio(current["value"], baseline)
    prev_x = safe_ratio(current["value"], prev_den)

    active = (
        base_x >= STAGE1_BASELINE_X
        and prev_x >= STAGE1_PREV_X
        and current["value"] > previous["value"]
    )

    return {
        "window": window,
        "current": current,
        "previous": previous,
        "baseline": baseline,
        "base_x": base_x,
        "prev_x": prev_x,
        "active": active,
    }


def pct_change(current: Optional[float], previous: Optional[float]) -> Optional[float]:
    if current is None or previous in (None, 0):
        return None
    return (current / previous - 1.0) * 100.0


def compute_metrics(market: str, now_sec: int) -> Optional[dict]:
    with LOCK:
        source = BUCKETS.get(market)
        if not source:
            return None
        buckets = dict(source)

    if time.time() - STARTED_AT < WARMUP_SEC:
        return None

    metrics = [window_metric(buckets, now_sec, w) for w in WINDOWS]
    active = [m for m in metrics if m["active"]]
    if active:
        best = max(active, key=lambda m: (m["base_x"] * m["prev_x"], m["current"]["value"]))
    else:
        best = max(metrics, key=lambda m: (m["base_x"] * m["prev_x"], m["current"]["value"]))

    f3 = sum_range(buckets, now_sec - 2, now_sec)
    p3 = sum_range(buckets, now_sec - 5, now_sec - 3)
    f5 = sum_range(buckets, now_sec - 4, now_sec)
    p5 = sum_range(buckets, now_sec - 9, now_sec - 5)
    f10 = sum_range(buckets, now_sec - 9, now_sec)
    p10 = sum_range(buckets, now_sec - 19, now_sec - 10)

    share3, share5, share10 = bid_share(f3), bid_share(f5), bid_share(f10)
    prev_share3, prev_share5, prev_share10 = bid_share(p3), bid_share(p5), bid_share(p10)

    net3 = f3["bid"] - f3["ask"]
    net5 = f5["bid"] - f5["ask"]
    net10 = f10["bid"] - f10["ask"]
    prev_net3 = p3["bid"] - p3["ask"]
    prev_net5 = p5["bid"] - p5["ask"]
    prev_net10 = p10["bid"] - p10["ask"]

    direction_improving = (
        net3 > prev_net3
        or net5 > prev_net5
        or share5 >= prev_share5 + 0.05
        or (net10 > 0 and share10 >= 0.50)
    )

    direction_confirmed = (
        (net10 > 0 and share10 >= 0.55 and share10 >= prev_share10)
        or (
            net5 > 0
            and share5 >= 0.60
            and share10 >= 0.50
            and net5 >= prev_net5
        )
    )

    current_price = last_price_before(buckets, now_sec, 3)
    price_5s_ago = last_price_before(buckets, now_sec - 5, 30)
    price_30s_ago = last_price_before(buckets, now_sec - 30, 60)
    price_60s_ago = last_price_before(buckets, now_sec - 60, 120)

    ret5 = pct_change(current_price, price_5s_ago)
    ret30 = pct_change(current_price, price_30s_ago)
    ret60 = pct_change(current_price, price_60s_ago)

    # State-based deceleration/death: no fixed wait.
    slopes = {}
    for w in (1, 3, 5, 7, 10):
        m = metrics[w - 1]
        slopes[w] = m["current"]["value"] - m["previous"]["value"]

    long_weaken = slopes[5] < 0 and slopes[7] < 0 and slopes[10] < 0
    deep_weaken = (
        f5["value"] < p5["value"] * 0.70
        and f10["value"] < p10["value"] * 0.80
    )
    normalized_dead = max((m["base_x"] for m in metrics), default=0.0) < 1.40
    ask_control = share10 < 0.40 and net10 < 0 and f10["value"] <= p10["value"]

    dead = (long_weaken and (deep_weaken or normalized_dead)) or (ask_control and long_weaken)

    # Ranking only; not a trigger threshold.
    active_count = len(active)
    base_strength = min(best["base_x"], 12.0)
    prev_strength = min(best["prev_x"], 15.0)
    directional_bonus = max(0.0, (share10 - 0.50) * 20.0)
    persistence_bonus = active_count * 2.0
    quality_score = base_strength + prev_strength + directional_bonus + persistence_bonus

    chase = "낮음"
    if (ret30 is not None and ret30 >= 5.0) or (ret60 is not None and ret60 >= 8.0):
        chase = "높음"
    elif (ret30 is not None and ret30 >= 3.0) or (ret60 is not None and ret60 >= 5.0):
        chase = "주의"

    return {
        "market": market,
        "metrics": metrics,
        "active": active,
        "active_count": active_count,
        "best": best,
        "direction_improving": direction_improving,
        "direction_confirmed": direction_confirmed,
        "bid_share_3": share3,
        "bid_share_5": share5,
        "bid_share_10": share10,
        "prev_bid_share_10": prev_share10,
        "net3": net3,
        "net5": net5,
        "net10": net10,
        "ret5": ret5,
        "ret30": ret30,
        "ret60": ret60,
        "price": current_price,
        "dead": dead,
        "quality_score": quality_score,
        "chase": chase,
    }


def target_stage(m: dict) -> int:
    """Every second, promote immediately if conditions are met. No fixed timers."""
    count = m["active_count"]
    if count >= 3 and m["direction_confirmed"]:
        return 3
    if count >= 2 and m["direction_improving"]:
        return 2
    if count >= 1:
        return 1
    return 0


def fmt_money(v: float) -> str:
    if abs(v) >= 100_000_000:
        return f"{v / 100_000_000:.2f}억"
    if abs(v) >= 10_000:
        return f"{v / 10_000:.0f}만"
    return f"{v:,.0f}"


def fmt_pct(v: Optional[float]) -> str:
    return "N/A" if v is None else f"{v:+.2f}%"


def alert_text(m: dict, stage: int, is_top: bool) -> str:
    market = m["market"]
    best = m["best"]
    title = market_title(market)
    active_windows = ",".join(str(x["window"]) for x in m["active"]) or "-"
    top = " · 현재 1순위" if is_top and stage == 3 else ""

    if stage == 1:
        header = "⚠️ 1차 유의"
        status = "거래대금 최초 이상가속"
    elif stage == 2:
        header = "🔥 2차 추천"
        status = "가속 지속/다중창 확산 · 방향 개선"
    else:
        header = f"🚀 3차 매수{top}"
        status = "가속 강화/재가속 · 상방 확인"

    return (
        f"{title}\n"
        f"{header}\n"
        f"{status}\n"
        f"감지창: {best['window']}초 | 활성창: {active_windows}\n"
        f"평시 대비: {best['base_x']:.2f}x\n"
        f"직전 동일창 대비: {best['prev_x']:.2f}x\n"
        f"BID 10초: {m['bid_share_10'] * 100:.1f}% | 순매수: {fmt_money(m['net10'])}\n"
        f"가격반응 5초: {fmt_pct(m['ret5'])} | 30초: {fmt_pct(m['ret30'])}\n"
        f"추격위험: {m['chase']}"
    )


def cancel_text(m: dict) -> str:
    return (
        f"{market_title(m['market'])}\n"
        f"⛔ 매수 취소\n"
        f"거래대금 가속 소멸/다중창 감속\n"
        f"BID 10초: {m['bid_share_10'] * 100:.1f}% | 순매수: {fmt_money(m['net10'])}\n"
        f"신규진입 보류 · 재가속 시 새 사이클로 재탐지"
    )


def evaluate_once() -> None:
    now_sec = int(time.time()) - 1
    computed: Dict[str, dict] = {}

    for market in MARKETS:
        try:
            m = compute_metrics(market, now_sec)
            if m is not None:
                computed[market] = m
        except Exception as exc:
            print(f"[metric error] {market}: {exc}", flush=True)

    if not computed:
        return

    desired = {market: target_stage(m) for market, m in computed.items()}
    stage3_candidates = [
        m for market, m in computed.items()
        if desired.get(market) == 3 and not m["dead"]
    ]
    top_market = None
    if stage3_candidates:
        top_market = max(stage3_candidates, key=lambda x: x["quality_score"])["market"]

    for market, m in computed.items():
        state = STATES[market]
        wanted = desired[market]

        if m["dead"]:
            if state.stage == 3 and state.cancelled_cycle_id != state.cycle_id:
                telegram(cancel_text(m))
                state.cancelled_cycle_id = state.cycle_id

            if state.stage > 0:
                print(f"[reset] {market} stage={state.stage} acceleration dead", flush=True)
                state.stage = 0
                state.last_score = 0.0
                state.last_best_window = 0
            continue

        if wanted == 0:
            continue

        if state.stage == 0:
            state.cycle_id += 1
            state.cancelled_cycle_id = -1

        # If a coin satisfies several stages in one second, send them in order.
        if wanted > state.stage:
            for stage in range(state.stage + 1, wanted + 1):
                telegram(alert_text(m, stage, market == top_market))
                print(
                    f"[alert] {market} stage={stage} "
                    f"w={m['best']['window']} "
                    f"base={m['best']['base_x']:.2f}x "
                    f"prev={m['best']['prev_x']:.2f}x "
                    f"active={m['active_count']}",
                    flush=True,
                )
                state.stage = stage
                state.last_stage_at = time.time()
                state.last_score = m["quality_score"]
                state.last_best_window = m["best"]["window"]
                state.last_price = m["price"]


def evaluator_loop() -> None:
    next_tick = math.floor(time.time()) + 1
    while not STOP.is_set():
        delay = next_tick - time.time()
        if delay > 0:
            STOP.wait(delay)
        if STOP.is_set():
            break
        try:
            evaluate_once()
        except Exception as exc:
            print(f"[evaluator error] {exc}", flush=True)
        next_tick += EVAL_INTERVAL


def on_open(ws) -> None:
    payload = [
        {"ticket": str(uuid.uuid4())},
        {"type": "trade", "codes": MARKETS, "is_only_realtime": True},
        {"format": "DEFAULT"},
    ]
    ws.send(json.dumps(payload))
    print(f"[websocket] subscribed {len(MARKETS)} markets", flush=True)


def on_message(ws, message) -> None:
    try:
        if isinstance(message, bytes):
            message = message.decode("utf-8")
        row = json.loads(message)
        add_trade(row)
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
    if not STOP.is_set():
        print("[shutdown] stopping V5 radar", flush=True)
        STOP.set()


def main() -> None:
    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    if not BOT_TOKEN or not CHAT_ID:
        print(
            "[warning] TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID 미설정. "
            "알림은 콘솔에만 표시됩니다.",
            flush=True,
        )

    fetch_markets()

    print(
        f"[V5] warmup={WARMUP_SEC}s windows=1~10s "
        f"stage1 baseline>={STAGE1_BASELINE_X:.1f}x "
        f"prev>={STAGE1_PREV_X:.1f}x",
        flush=True,
    )

    thread = threading.Thread(target=evaluator_loop, name="v5-evaluator", daemon=True)
    thread.start()

    telegram(
        "✅ Upbit Radar V5 시작\n"
        "1초 재판정 · 1~10초 멀티윈도우\n"
        "1차: 평시≥2.0x + 직전 동일창≥2.5x\n"
        "2/3차: 시간고정 없이 조건 충족 즉시 승급"
    )

    websocket_loop()
    STOP.set()
    thread.join(timeout=3)


if __name__ == "__main__":
    main()
