#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Upbit Radar V5.4

New 1->5 state machine learned from Sep 1-3 surge samples.

Internal stages
1) 60s traded value >= 2.8x vs prior ten 60s baseline windows.
2) 10s buy-direction confirmation:
   BID value > ASK value, net buy > 0, BID value > previous 10s BID value.
3) waiting / re-accumulation state. No Telegram.
   DROP candidate: at least 2 of 3 deterioration axes persist for 20s:
   - price <= T - 2 ticks
   - 10s sell dominance (ASK>BID and net buy<0)
   - buy activity weakens (10s BID value and BID trade count both below prev 10s)
4) launch: within recent 3s, price expands >= +4 ticks. Telegram starts here.
5) final: at launch, recent 10s BID>ASK and net buy>0. Telegram final buy confirm.

If Stage 4 fails Stage 5, it returns to Stage 3 and re-arms after the
3-second launch condition clears. Stages 1-3 and DROP/re-arm are log-only.
"""
from __future__ import annotations

import collections
import json
import os
import signal
import statistics
import threading
import time
import urllib.parse
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Deque, Dict, Optional, Tuple

import realtime_radar_v51 as r

VERSION = "V5.4"
ENTRY_VALUE_X = 2.8
BASELINE_WINDOWS = 10
FLOW_SEC = 10
LAUNCH_SEC = 3
LAUNCH_TICKS = 4
DROP_HOLD_SEC = 20
WARMUP_SEC = 11 * 60
EVENT_KEEP_MS = 15 * 60 * 1000
KST = timezone(timedelta(hours=9))
EVENT_LOG_DIR = Path(os.getenv("RADAR_EVENT_DIR", "/home/ubuntu/upbit-scanner/data/live/radar_events"))

ORIG_ADD_TRADE = r.add_trade
TRADE_EVENTS: Dict[str, Deque[Tuple[int, float, str, float]]] = {}
TICK_SIZE: Dict[str, float] = {}


@dataclass
class V54State:
    stage: int = 0
    t_sec: int = 0
    t_price: Optional[float] = None
    t_value_x: float = 0.0
    drop_since: int = 0
    launch_armed: bool = True
    last_launch_sec: int = 0
    cycle_id: int = 0
    last_bad_count: int = 0


ST: Dict[str, V54State] = {}


def log_event(market: str, event: str, sec: Optional[int] = None, st: Optional[V54State] = None, **fields) -> None:
    """Append a durable, GitHub-published JSONL state-machine event."""
    try:
        event_sec = int(sec if sec is not None else time.time())
        state = st or ST.get(market)
        row = {
            "version": VERSION,
            "timestamp_kst": datetime.fromtimestamp(event_sec, KST).isoformat(),
            "epoch_sec": event_sec,
            "market": market,
            "event": event,
            "cycle_id": state.cycle_id if state else None,
            "stage": state.stage if state else None,
            "t_sec": state.t_sec if state else None,
            "t_price": state.t_price if state else None,
            "t_value_x": state.t_value_x if state else None,
            **fields,
        }
        day_dir = EVENT_LOG_DIR / datetime.fromtimestamp(event_sec, KST).strftime("%Y%m%d")
        day_dir.mkdir(parents=True, exist_ok=True)
        line = json.dumps(row, ensure_ascii=False, separators=(",", ":"))
        with (day_dir / "v54_events.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(line + "\\n")
        latest_tmp = EVENT_LOG_DIR / "latest.tmp"
        latest_tmp.write_text(line, encoding="utf-8")
        os.replace(latest_tmp, EVENT_LOG_DIR / "latest.json")
    except Exception as exc:
        print(f"[event-log error] {market} {event}: {exc}", flush=True)


def reset_state(market: str, why: str = "") -> None:
    old = ST.get(market, V54State())
    if why and old.stage:
        print(f"[reset] {market} stage={old.stage} {why}", flush=True)
        log_event(market, "reset", st=old, reason=why)
    ST[market] = V54State(cycle_id=old.cycle_id)


def add_trade_v54(row: dict) -> None:
    ORIG_ADD_TRADE(row)
    market = str(row.get("code") or "")
    if market not in r.NAMES:
        return
    try:
        price = float(row["trade_price"])
        volume = float(row["trade_volume"])
        ms = int(row.get("timestamp") or time.time() * 1000)
        side = str(row.get("ask_bid") or "")
    except (KeyError, TypeError, ValueError):
        return
    value = price * volume
    q = TRADE_EVENTS.setdefault(market, collections.deque())
    q.append((ms, price, side, value))
    cutoff = ms - EVENT_KEEP_MS
    while q and q[0][0] < cutoff:
        q.popleft()


def fetch_tick_sizes() -> None:
    TICK_SIZE.clear()
    for i in range(0, len(r.MARKETS), 50):
        batch = r.MARKETS[i:i+50]
        try:
            url = f"{r.UPBIT_REST}/orderbook/instruments?markets=" + urllib.parse.quote(",".join(batch))
            rows = r.http_json(url)
            for row in rows:
                market = str(row.get("market") or "")
                tick = float(row.get("tick_size") or 0)
                if market and tick > 0:
                    TICK_SIZE[market] = tick
        except Exception as exc:
            print("[tick-size error]", exc, flush=True)
    print(f"[tick-size] loaded={len(TICK_SIZE)}", flush=True)


def minute_value_x(market: str, sec: int) -> Tuple[float, float, float]:
    with r.LOCK:
        bs = dict(r.BUCKETS.get(market, {}))
    cur = r.sum_range(bs, sec - 59, sec)["value"]
    vals = []
    for i in range(1, BASELINE_WINDOWS + 1):
        end = sec - 60 * i
        vals.append(r.sum_range(bs, end - 59, end)["value"])
    mean = statistics.fmean(vals) if vals else 0.0
    nz = [x for x in vals if x > 0]
    robust_floor = statistics.median(nz) * 0.35 if nz else 0.0
    base = max(mean, robust_floor, 1.0)
    return cur / base, cur, base


def flow10(market: str, sec: int) -> dict:
    q = TRADE_EVENTS.get(market, ())
    now_ms = (sec + 1) * 1000 - 1
    cur_start = now_ms - FLOW_SEC * 1000 + 1
    prev_start = cur_start - FLOW_SEC * 1000
    cur = {"bid": 0.0, "ask": 0.0, "bid_count": 0, "ask_count": 0}
    prev = {"bid": 0.0, "ask": 0.0, "bid_count": 0, "ask_count": 0}
    for ms, _price, side, value in q:
        target = cur if ms >= cur_start else prev if ms >= prev_start else None
        if target is None:
            continue
        if side == "BID":
            target["bid"] += value
            target["bid_count"] += 1
        elif side == "ASK":
            target["ask"] += value
            target["ask_count"] += 1
    total = cur["bid"] + cur["ask"]
    cur["share"] = cur["bid"] / total if total else 0.5
    cur["net"] = cur["bid"] - cur["ask"]
    return {"cur": cur, "prev": prev}


def launch3(market: str, sec: int) -> dict:
    tick = TICK_SIZE.get(market) or 0.0
    if tick <= 0:
        return {"ok": False, "ticks": 0.0, "start": None, "high": None}
    q = TRADE_EVENTS.get(market, ())
    end_ms = (sec + 1) * 1000 - 1
    start_ms = end_ms - LAUNCH_SEC * 1000 + 1
    pts = [(ms, p) for ms, p, _side, _value in q if ms >= start_ms]
    if not pts:
        return {"ok": False, "ticks": 0.0, "start": None, "high": None}
    pts.sort()
    start_price = pts[0][1]
    high = max(p for _ms, p in pts)
    ticks = (high - start_price) / tick
    return {"ok": ticks >= LAUNCH_TICKS - 1e-9, "ticks": ticks, "start": start_price, "high": high}


def current_price(market: str, sec: int) -> Optional[float]:
    with r.LOCK:
        bs = dict(r.BUCKETS.get(market, {}))
    return r.last_price(bs, sec, 3)


def price_ticks_from_t(market: str, st: V54State, price: Optional[float]) -> Optional[float]:
    tick = TICK_SIZE.get(market) or 0.0
    if not st.t_price or price is None or tick <= 0:
        return None
    return (price - st.t_price) / tick


def price_pct_from_t(st: V54State, price: Optional[float]) -> Optional[float]:
    if not st.t_price or price is None:
        return None
    return (price / st.t_price - 1.0) * 100.0


def drop_axes(market: str, sec: int, st: V54State, flow: dict, price: Optional[float]) -> Tuple[int, str]:
    pt = price_ticks_from_t(market, st, price)
    price_bad = pt is not None and pt <= -2.0
    cur, prev = flow["cur"], flow["prev"]
    sell_bad = cur["ask"] > cur["bid"] and cur["net"] < 0
    buy_weak = cur["bid"] < prev["bid"] and cur["bid_count"] < prev["bid_count"]
    labels = []
    if price_bad:
        labels.append("price<=T-2tick")
    if sell_bad:
        labels.append("ASK>BID/net-")
    if buy_weak:
        labels.append("BID-value/count-down")
    return len(labels), ",".join(labels)


def fmt_pct(v: Optional[float]) -> str:
    return "N/A" if v is None else f"{v:+.2f}%"


def alert4(market: str, st: V54State, vx: float, flow: dict, launch: dict, price: Optional[float]) -> str:
    cur = flow["cur"]
    return (
        f"{r.title(market)}\n"
        f"🚨 4차 발사 감지\n"
        f"3초 가격가속: +{launch['ticks']:.1f}틱 (기준 +{LAUNCH_TICKS}틱)\n"
        f"현재가: {price if price is not None else 'N/A'}\n"
        f"T 대비 가격: {fmt_pct(price_pct_from_t(st, price))}\n"
        f"1분 거래대금: {vx:.2f}x\n"
        f"BID 10초: {cur['share']*100:.1f}% | 순매수: {r.fmt_money(cur['net'])}\n"
        f"상태: 5차 최종 매수조건 확인"
    )


def alert5(market: str, st: V54State, vx: float, flow: dict, launch: dict, price: Optional[float]) -> str:
    cur = flow["cur"]
    return (
        f"{r.title(market)}\n"
        f"🔥 5차 최종 매수확정\n"
        f"현재가: {price if price is not None else 'N/A'}\n"
        f"T 대비 가격: {fmt_pct(price_pct_from_t(st, price))}\n"
        f"1분 거래대금: {vx:.2f}x\n"
        f"BID 10초: {cur['share']*100:.1f}%\n"
        f"순매수 10초: {r.fmt_money(cur['net'])}\n"
        f"가격가속: 3초 +{launch['ticks']:.1f}틱\n"
        f"결과: 매수확정"
    )


def evaluate_once() -> None:
    sec = int(time.time()) - 1
    if time.time() - r.STARTED_AT < WARMUP_SEC:
        return

    for market in r.MARKETS:
        st = ST.setdefault(market, V54State())
        price = current_price(market, sec)
        if price is None:
            continue
        try:
            vx, _cur_value, _base = minute_value_x(market, sec)
            flow = flow10(market, sec)
            launch = launch3(market, sec)
        except Exception as exc:
            print("[v54 metric error]", market, exc, flush=True)
            continue

        if st.stage == 0:
            if vx >= ENTRY_VALUE_X:
                st.cycle_id += 1
                st.stage = 1
                st.t_sec = sec
                st.t_price = price
                st.t_value_x = vx
                log_event(market, "stage1", sec, st, price=price, current_value_x=vx)
                print(f"[1차] {market} cycle={st.cycle_id} T={price} value={vx:.2f}x", flush=True)
            continue

        if st.stage == 1:
            pt = price_ticks_from_t(market, st, price)
            if pt is not None and pt <= -2.0:
                reset_state(market, "2차 전 T-2틱 이탈")
                continue
            cur, prev = flow["cur"], flow["prev"]
            flow_ok = cur["bid"] > cur["ask"] and cur["net"] > 0 and cur["bid"] > prev["bid"]
            if flow_ok:
                st.stage = 3
                log_event(market, "stage2", sec, st, price=price, current_value_x=vx, bid=cur["bid"], ask=cur["ask"], net=cur["net"], bid_count=cur["bid_count"], ask_count=cur["ask_count"], prev_bid=prev["bid"], prev_bid_count=prev["bid_count"])
                log_event(market, "stage3", sec, st, reason="stage2_pass")
                print(
                    f"[2차->3차] {market} bid={cur['share']*100:.1f}% "
                    f"net={cur['net']:.0f} bidcnt={cur['bid_count']}",
                    flush=True,
                )
            elif vx < ENTRY_VALUE_X:
                reset_state(market, "2차 미충족 + 2.8x 해제")
            continue

        if st.stage == 5:
            if vx < 1.0:
                reset_state(market, "완료 후 거래대금 정상화")
            continue

        bad_count, bad_reason = drop_axes(market, sec, st, flow, price)
        if bad_count != st.last_bad_count:
            log_event(market, "drop_axes_change", sec, st, bad_count=bad_count, bad_reason=bad_reason, price=price, bid=flow["cur"]["bid"], ask=flow["cur"]["ask"], net=flow["cur"]["net"], bid_count=flow["cur"]["bid_count"], ask_count=flow["cur"]["ask_count"])
            st.last_bad_count = bad_count

        if bad_count >= 2:
            if st.drop_since == 0:
                st.drop_since = sec
            held = sec - st.drop_since + 1
            if held >= DROP_HOLD_SEC:
                log_event(market, "drop", sec, st, held_sec=held, bad_count=bad_count, bad_reason=bad_reason, price=price)
                print(f"[DROP] {market} held={held}s axes={bad_reason}", flush=True)
                reset_state(market, f"DROP {held}s {bad_reason}")
                continue
        else:
            st.drop_since = 0

        if not launch["ok"]:
            st.launch_armed = True

        if st.stage == 3 and launch["ok"] and st.launch_armed:
            st.stage = 4
            st.launch_armed = False
            st.last_launch_sec = sec
            log_event(market, "stage4", sec, st, price=price, current_value_x=vx, launch_ticks=launch["ticks"], launch_start=launch["start"], launch_high=launch["high"], bid=flow["cur"]["bid"], ask=flow["cur"]["ask"], net=flow["cur"]["net"], bid_share=flow["cur"]["share"], bid_count=flow["cur"]["bid_count"], ask_count=flow["cur"]["ask_count"])
            r.telegram(alert4(market, st, vx, flow, launch, price))
            print(f"[4차] {market} +{launch['ticks']:.1f}tick", flush=True)

            cur = flow["cur"]
            final_ok = cur["bid"] > cur["ask"] and cur["net"] > 0
            if final_ok:
                st.stage = 5
                log_event(market, "stage5", sec, st, price=price, current_value_x=vx, launch_ticks=launch["ticks"], bid=cur["bid"], ask=cur["ask"], net=cur["net"], bid_share=cur["share"], bid_count=cur["bid_count"], ask_count=cur["ask_count"])
                r.telegram(alert5(market, st, vx, flow, launch, price))
                print(
                    f"[5차] {market} FINAL bid={cur['share']*100:.1f}% net={cur['net']:.0f}",
                    flush=True,
                )
            else:
                st.stage = 3
                log_event(market, "stage4_to_stage3", sec, st, reason="stage5_flow_fail", price=price, current_value_x=vx, bid=cur["bid"], ask=cur["ask"], net=cur["net"], bid_share=cur["share"], bid_count=cur["bid_count"], ask_count=cur["ask_count"])
                print(
                    f"[4차->3차] {market} 5차미달 bid={cur['share']*100:.1f}% net={cur['net']:.0f}",
                    flush=True,
                )


def main() -> None:
    signal.signal(signal.SIGINT, r.shutdown)
    signal.signal(signal.SIGTERM, r.shutdown)

    r.fetch_markets()
    for market in r.MARKETS:
        ST[market] = V54State()
        TRADE_EVENTS[market] = collections.deque()
    fetch_tick_sizes()

    r.add_trade = add_trade_v54
    r.evaluate_once = evaluate_once

    print(
        f"[{VERSION}] 1차=60s value>={ENTRY_VALUE_X:.1f}x | "
        f"2차=10s BID>ASK/net+/BID증가 | "
        f"4차=3s+{LAUNCH_TICKS}tick | 5차=10s BID>ASK/net+ | "
        f"DROP=2axes {DROP_HOLD_SEC}s",
        flush=True,
    )
    r.telegram(
        "✅ Upbit Radar V5.4 시작\n"
        "1~3차: 내부 로그 전용\n"
        "4차: 3초 +4틱 발사감지부터 Telegram\n"
        "5차: 10초 BID>ASK + 순매수(+) 재확인 시 최종 매수확정\n"
        "4차 실패는 3차 복귀 · DROP은 서버 로그만 기록"
    )

    th = threading.Thread(target=r.evaluator_loop, name="v54-evaluator", daemon=True)
    th.start()
    r.websocket_loop()
    r.STOP.set()
    th.join(timeout=3)


if __name__ == "__main__":
    main()
