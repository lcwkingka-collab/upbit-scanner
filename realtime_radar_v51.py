#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Upbit KRW Real-time Radar V5.1

4-stage state machine
1) first acceleration: baseline >= 2.0x and previous same-window >= 2.5x
2) later NEW data must show broader/better acceleration
3) later NEW data must strengthen/re-accelerate again
4) later NEW data must stay strong AND price >= +1.0% from stage1

Important
- No fixed stage timers.
- One snapshot can never promote multiple stages.
- -8% is ONLY downside noise tolerance, NOT a promotion threshold.
- Example: 10 -> 9.2 is still alive; 10 -> 10 -> 50 can re-accelerate.
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
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

try:
    import websocket
except ImportError as exc:
    raise SystemExit("websocket-client가 필요합니다: pip install websocket-client") from exc

UPBIT_REST = "https://api.upbit.com/v1"
UPBIT_WS = "wss://api.upbit.com/websocket/v1"
WINDOWS = tuple(range(1, 11))
WARMUP_SEC = 300
KEEP_SEC = 720
BASELINE_START_SEC = 300
BASELINE_EXCLUDE_RECENT_SEC = 30
STAGE1_BASELINE_X = 2.0
STAGE1_PREV_X = 2.5
DOWNSIDE_TOLERANCE = 0.08
ALIVE_FLOOR = 1.0 - DOWNSIDE_TOLERANCE  # 0.92
FINAL_PRICE_RETURN = 1.0
EVAL_INTERVAL = 1.0
RECONNECT_DELAY = 3.0
STABLE_MARKETS = {"KRW-USDT", "KRW-USDC", "KRW-DAI", "KRW-USDE"}
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
class Snap:
    sec: int = 0
    score: float = 0.0
    base_x: float = 0.0
    prev_x: float = 0.0
    active_count: int = 0
    bid_share: float = 0.5
    net_buy: float = 0.0
    price: Optional[float] = None
    best_window: int = 0


@dataclass
class CoinState:
    stage: int = 0
    cycle_id: int = 0
    stage1_price: Optional[float] = None
    last_snap: Snap = field(default_factory=Snap)


BUCKETS: Dict[str, Dict[int, Bucket]] = {}
STATES: Dict[str, CoinState] = {}
NAMES: Dict[str, Tuple[str, str]] = {}
MARKETS: List[str] = []


def http_json(url, data=None, timeout=15):
    headers = {"User-Agent": "upbit-radar-v51/1.1"}
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
    try:
        http_json(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            {"chat_id": CHAT_ID, "text": text, "disable_web_page_preview": "true"},
            10,
        )
    except Exception as exc:
        print("[telegram error]", exc, flush=True)


def fetch_markets() -> None:
    global MARKETS
    MARKETS = []
    for row in http_json(f"{UPBIT_REST}/market/all?is_details=false"):
        market = str(row.get("market", ""))
        if not market.startswith("KRW-") or market in STABLE_MARKETS:
            continue
        ticker = market.split("-", 1)[-1]
        NAMES[market] = (
            str(row.get("english_name") or ticker),
            str(row.get("korean_name") or ticker),
        )
        BUCKETS[market] = {}
        STATES[market] = CoinState()
        MARKETS.append(market)
    MARKETS.sort()
    print(f"[markets] {len(MARKETS)}", flush=True)


def title(market: str) -> str:
    en, ko = NAMES.get(market, (market, market))
    return f"{market.split('-', 1)[-1]} / {ko} · {en} ({market})"


def add_trade(row: dict) -> None:
    market = row.get("code")
    if market not in NAMES:
        return
    try:
        price = float(row["trade_price"])
        volume = float(row["trade_volume"])
        sec = int(row.get("timestamp") or time.time() * 1000) // 1000
    except (KeyError, TypeError, ValueError):
        return

    with LOCK:
        b = BUCKETS[market].setdefault(sec, Bucket())
        value = price * volume
        b.value += value
        b.count += 1
        b.last_price = price
        if row.get("ask_bid") == "BID":
            b.bid += value
        elif row.get("ask_bid") == "ASK":
            b.ask += value

        cutoff = int(time.time()) - KEEP_SEC
        for key in [x for x in BUCKETS[market] if x < cutoff]:
            BUCKETS[market].pop(key, None)


def sum_range(bs: Dict[int, Bucket], start: int, end: int) -> dict:
    out = {"value": 0.0, "bid": 0.0, "ask": 0.0, "count": 0, "price": None}
    last_sec = -1
    for sec in range(start, end + 1):
        b = bs.get(sec)
        if not b:
            continue
        out["value"] += b.value
        out["bid"] += b.bid
        out["ask"] += b.ask
        out["count"] += b.count
        if b.last_price is not None and sec >= last_sec:
            out["price"] = b.last_price
            last_sec = sec
    return out


def last_price(bs: Dict[int, Bucket], sec: int, look: int = 120) -> Optional[float]:
    for s in range(sec, sec - look - 1, -1):
        b = bs.get(s)
        if b and b.last_price is not None:
            return b.last_price
    return None


def ratio(a: float, b: float) -> float:
    return a / b if b > 0 else 0.0


def baseline(bs: Dict[int, Bucket], now: int, w: int) -> float:
    vals = []
    end = now - BASELINE_EXCLUDE_RECENT_SEC
    oldest = now - BASELINE_START_SEC
    while end >= oldest:
        vals.append(sum_range(bs, end - w + 1, end)["value"])
        end -= max(1, w)
    if not vals:
        return 0.0
    mean = statistics.fmean(vals)
    nz = [x for x in vals if x > 0]
    robust = max(mean, statistics.median(nz) * 0.35 if nz else 0.0)
    return max(robust, 1.0)


def metric(bs: Dict[int, Bucket], now: int, w: int) -> dict:
    cur = sum_range(bs, now - w + 1, now)
    prev = sum_range(bs, now - 2 * w + 1, now - w)
    base = baseline(bs, now, w)
    # Prevent 0/tiny denominator explosions by tying the floor to this coin's own baseline.
    prev_den = max(prev["value"], base * 0.25, 1.0)
    px = ratio(cur["value"], prev_den)
    bx = ratio(cur["value"], base)
    active = (
        bx >= STAGE1_BASELINE_X
        and px >= STAGE1_PREV_X
        and cur["value"] > prev["value"]
    )
    return {"w": w, "cur": cur, "prev": prev, "base": base, "base_x": bx, "prev_x": px, "active": active}


def compute(market: str, now: int) -> Optional[dict]:
    with LOCK:
        bs = dict(BUCKETS.get(market, {}))
    if not bs or time.time() - STARTED_AT < WARMUP_SEC:
        return None

    ms = [metric(bs, now, w) for w in WINDOWS]
    active = [x for x in ms if x["active"]]
    best = max(active or ms, key=lambda x: (x["base_x"] * x["prev_x"], x["cur"]["value"]))

    f5 = sum_range(bs, now - 4, now)
    p5 = sum_range(bs, now - 9, now - 5)
    f10 = sum_range(bs, now - 9, now)
    p10 = sum_range(bs, now - 19, now - 10)

    total = f10["bid"] + f10["ask"]
    share = f10["bid"] / total if total else 0.5
    net = f10["bid"] - f10["ask"]
    ptotal = p10["bid"] + p10["ask"]
    pshare = p10["bid"] / ptotal if ptotal else 0.5
    pnet = p10["bid"] - p10["ask"]

    improving = (net > pnet) or (share > pshare) or (net > 0 and share >= 0.50)
    confirmed = (
        (net > 0 and share >= 0.55 and share >= pshare)
        or (f5["bid"] > f5["ask"] and share >= 0.50)
    )

    slopes = {w: ms[w - 1]["cur"]["value"] - ms[w - 1]["prev"]["value"] for w in (1, 3, 5, 7, 10)}
    long_weak = slopes[5] < 0 and slopes[7] < 0 and slopes[10] < 0
    deep = f5["value"] < p5["value"] * 0.70 and f10["value"] < p10["value"] * 0.80
    normalized = max(x["base_x"] for x in ms) < 1.40
    dead = long_weak and (deep or normalized or (share < 0.40 and net < 0))

    price = last_price(bs, now, 3)
    score = (
        min(best["base_x"], 12.0)
        + min(best["prev_x"], 15.0)
        + max(0.0, (share - 0.5) * 20.0)
        + len(active) * 2.0
    )
    return {
        "market": market,
        "active": active,
        "count": len(active),
        "best": best,
        "share": share,
        "net": net,
        "improving": improving,
        "confirmed": confirmed,
        "dead": dead,
        "price": price,
        "score": score,
    }


def snap(m: dict, sec: int) -> Snap:
    b = m["best"]
    return Snap(sec, m["score"], b["base_x"], b["prev_x"], m["count"], m["share"], m["net"], m["price"], b["w"])


def core_alive(m: dict, old: Snap) -> bool:
    """-8% is downside tolerance only. Up to 8% drop stays alive."""
    b = m["best"]
    base_alive = old.base_x <= 0 or b["base_x"] >= old.base_x * ALIVE_FLOOR
    prev_alive = old.prev_x <= 0 or b["prev_x"] >= old.prev_x * ALIVE_FLOOR
    # Do not kill a cycle because one dimension changed window; require both core measures to break tolerance.
    return base_alive or prev_alive


def stage_deteriorated(m: dict, old: Snap) -> bool:
    """Meaningful decay: core acceleration breaks -8% tolerance AND quality also weakens."""
    if old.sec <= 0:
        return False
    b = m["best"]
    base_bad = old.base_x > 0 and b["base_x"] < old.base_x * ALIVE_FLOOR
    prev_bad = old.prev_x > 0 and b["prev_x"] < old.prev_x * ALIVE_FLOOR
    breadth_bad = m["count"] < old.active_count
    flow_bad = m["share"] < old.bid_share and m["net"] < old.net_buy
    return base_bad and prev_bad and (breadth_bad or flow_bad)


def genuinely_better(m: dict, old: Snap, from_stage: int, sec: int) -> bool:
    """Promotion uses NEW evidence. +8% is NOT required; any real improvement can count."""
    if old.sec <= 0 or sec <= old.sec:
        return False
    if not core_alive(m, old):
        return False

    b = m["best"]
    checks = [
        m["score"] > old.score,
        b["base_x"] > old.base_x,
        b["prev_x"] > old.prev_x,
        m["count"] > old.active_count,
        m["share"] > old.bid_share,
        m["net"] > old.net_buy,
    ]
    # Stage 2: at least two distinct improvements.
    # Stage 3/4: stronger confirmation, at least three distinct improvements.
    need = 2 if from_stage == 1 else 3
    return sum(bool(x) for x in checks) >= need


def fmt_money(v: float) -> str:
    if abs(v) >= 100_000_000:
        return f"{v / 100_000_000:.2f}억"
    if abs(v) >= 10_000:
        return f"{v / 10_000:.0f}만"
    return f"{v:,.0f}"


def price_from_stage1(st: CoinState, m: dict) -> Optional[float]:
    if not st.stage1_price or not m["price"]:
        return None
    return (m["price"] / st.stage1_price - 1.0) * 100.0


def alert(m: dict, stage: int, st: CoinState) -> str:
    b = m["best"]
    pr = price_from_stage1(st, m)
    heads = {
        1: "⚠️ 1차 유의",
        2: "🔥 2차 추천",
        3: "🚀 3차 강매수 후보",
        4: "✅ 4차 최종 매수확인",
    }
    why = {
        1: "최초 거래대금 급가속 감지",
        2: "1차 이후 새 데이터에서 추가 가속/품질 개선",
        3: "2차 이후 새 데이터에서 재가속/강화",
        4: "3차 이후 가속 지속 + 실제 가격반응 확인",
    }
    return (
        f"{title(m['market'])}\n"
        f"{heads[stage]}\n"
        f"{why[stage]}\n"
        f"감지창: {b['w']}초 | 활성창: {m['count']}개\n"
        f"평시 대비: {b['base_x']:.2f}x\n"
        f"직전 동일창 대비: {b['prev_x']:.2f}x\n"
        f"BID 10초: {m['share'] * 100:.1f}% | 순매수: {fmt_money(m['net'])}\n"
        f"1차 대비 가격: {(f'{pr:+.2f}%' if pr is not None else 'N/A')}"
    )


def stop_text(m: dict, st: CoinState, reason: str) -> str:
    pr = price_from_stage1(st, m)
    return (
        f"{title(m['market'])}\n"
        f"⛔ 사이클 중단\n"
        f"{reason}\n"
        f"1차 대비 가격: {(f'{pr:+.2f}%' if pr is not None else 'N/A')}\n"
        f"재가속 시 새 1차부터 재탐지"
    )


def reset(st: CoinState) -> None:
    st.stage = 0
    st.stage1_price = None
    st.last_snap = Snap()


def evaluate_once() -> None:
    sec = int(time.time()) - 1
    for market in MARKETS:
        try:
            m = compute(market, sec)
        except Exception as exc:
            print("[metric error]", market, exc, flush=True)
            continue
        if not m:
            continue

        st = STATES[market]

        if st.stage > 0 and (m["dead"] or stage_deteriorated(m, st.last_snap)):
            reason = "거래대금 가속 소멸/다중창 감속" if m["dead"] else "직전 단계 대비 -8% 허용범위를 넘어선 실질 감속"
            telegram(stop_text(m, st, reason))
            print("[stop]", market, reason, flush=True)
            reset(st)
            continue

        if st.stage == 0:
            if m["count"] >= 1 and m["price"] is not None:
                st.cycle_id += 1
                st.stage = 1
                st.stage1_price = m["price"]
                st.last_snap = snap(m, sec)
                telegram(alert(m, 1, st))
                print("[alert]", market, "stage=1", flush=True)
            continue

        # Never promote on the same second/snapshot.
        if sec <= st.last_snap.sec:
            continue

        if st.stage == 1:
            if m["count"] >= 2 and m["improving"] and genuinely_better(m, st.last_snap, 1, sec):
                st.stage = 2
                st.last_snap = snap(m, sec)
                telegram(alert(m, 2, st))
                print("[alert]", market, "stage=2", flush=True)

        elif st.stage == 2:
            if m["count"] >= 3 and m["confirmed"] and genuinely_better(m, st.last_snap, 2, sec):
                st.stage = 3
                st.last_snap = snap(m, sec)
                telegram(alert(m, 3, st))
                print("[alert]", market, "stage=3", flush=True)

        elif st.stage == 3:
            pr = price_from_stage1(st, m)
            if m["confirmed"] and genuinely_better(m, st.last_snap, 3, sec):
                if pr is not None and pr >= FINAL_PRICE_RETURN:
                    st.stage = 4
                    st.last_snap = snap(m, sec)
                    telegram(alert(m, 4, st))
                    print("[alert]", market, "stage=4", flush=True)
                else:
                    telegram(stop_text(m, st, f"최종 확인 실패: 1차 대비 +{FINAL_PRICE_RETURN:.1f}% 가격반응 미달"))
                    print("[stop]", market, "price reaction fail", flush=True)
                    reset(st)


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
            print("[evaluator error]", exc, flush=True)
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
        add_trade(json.loads(message))
    except Exception as exc:
        print("[message error]", exc, flush=True)


def on_error(ws, error) -> None:
    print("[websocket error]", error, flush=True)


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
            print("[websocket run error]", exc, flush=True)
        if not STOP.is_set():
            STOP.wait(RECONNECT_DELAY)


def shutdown(signum=None, frame=None) -> None:
    if not STOP.is_set():
        print("[shutdown] stopping V5.1 radar", flush=True)
        STOP.set()


def main() -> None:
    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    fetch_markets()
    print(
        f"[V5.1] warmup={WARMUP_SEC}s windows=1~10s "
        f"stage1 baseline>={STAGE1_BASELINE_X:.1f}x prev>={STAGE1_PREV_X:.1f}x "
        f"downside_tolerance=-{DOWNSIDE_TOLERANCE*100:.0f}% final_price=+{FINAL_PRICE_RETURN:.1f}%",
        flush=True,
    )

    thread = threading.Thread(target=evaluator_loop, name="v51-evaluator", daemon=True)
    thread.start()

    telegram(
        "✅ Upbit Radar V5.1 시작\n"
        "1초 재판정 · 1~10초 멀티윈도우\n"
        "1차: 평시≥2.0x + 직전 동일창≥2.5x\n"
        "2/3차: 반드시 직전 단계 이후 새 데이터로 개선 확인\n"
        "감속 오차: -8%까지 허용 (승급조건 아님)\n"
        "4차: 1차 대비 가격 +1.0% 최종 확인"
    )

    websocket_loop()
    STOP.set()
    thread.join(timeout=3)


if __name__ == "__main__":
    main()
