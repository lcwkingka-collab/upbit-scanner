#!/usr/bin/env python3
"""V5.5 realtime radar.

V5.4의 동일 구간 재사용(4차/5차 동시 발송), 오래된 후보 유지, 저유동성
단발 체결과 순간 윗꼬리 문제를 보완한다. 전체 KRW 마켓을 감시하며 상위 40개로
잘라내지 않는다.
"""

import asyncio
import time
from dataclasses import dataclass

import realtime_radar_v54 as v54

r = v54.r

VERSION = "V5.5"
ENTRY_VALUE_X = 2.8
ENTRY_MIN_VALUE_KRW = 3_000_000
CONFIRM_SEC = 10
CONFIRM_TIMEOUT_SEC = 25
FLOW_SEC = 10
FLOW_MIN_VALUE_KRW = 1_000_000
FLOW_MIN_TRADES = 5
FLOW_MIN_BID_TRADES = 3
WAIT_TTL_SEC = 90
LAUNCH_SEC = 3
LAUNCH_TICKS = 4
LAUNCH_MIN_TRADES = 4
LAUNCH_MIN_BID_TRADES = 3
POST_CONFIRM_SEC = 10
POST_BID_RETAIN = 0.70
FINAL_COOLDOWN_SEC = 300
STABLE_TICKERS = {"USDT", "USDC", "USDG", "EURC"}


@dataclass
class V55State:
    stage: int = 0
    t_sec: int = 0
    t_price: float = 0.0
    t_value_x: float = 0.0
    confirm_sec: int = 0
    launch_sec: int = 0
    launch_price: float = 0.0
    launch_ticks: int = 0
    cooldown_until: int = 0


ST = {}


def reset(st):
    st.stage = 0
    st.t_sec = 0
    st.t_price = 0.0
    st.t_value_x = 0.0
    st.confirm_sec = 0
    st.launch_sec = 0
    st.launch_price = 0.0
    st.launch_ticks = 0


def ticker(market):
    return market.split("-", 1)[-1]


def events_between(market, start_ms, end_ms):
    return [e for e in v54.TRADE_EVENTS.get(market, ()) if start_ms <= e[0] <= end_ms]


def flow_between(market, start_sec, end_sec):
    evs = events_between(market, start_sec * 1000, end_sec * 1000 + 999)
    bid_value = sum(p * q for _, p, q, side in evs if side == "BID")
    ask_value = sum(p * q for _, p, q, side in evs if side == "ASK")
    bid_count = sum(1 for *_, side in evs if side == "BID")
    ask_count = sum(1 for *_, side in evs if side == "ASK")
    return {
        "bid_value": bid_value,
        "ask_value": ask_value,
        "net": bid_value - ask_value,
        "bid_count": bid_count,
        "ask_count": ask_count,
        "count": bid_count + ask_count,
        "total_value": bid_value + ask_value,
    }


def positive_flow(f, *, require_liquidity=True):
    if not (f["bid_value"] > f["ask_value"] and f["net"] > 0):
        return False
    if not require_liquidity:
        return True
    return (
        f["total_value"] >= FLOW_MIN_VALUE_KRW
        and f["count"] >= FLOW_MIN_TRADES
        and f["bid_count"] >= FLOW_MIN_BID_TRADES
    )


def persistent_flow(market, start_sec, end_sec, *, compare_bid=None):
    """전체 10초와 앞/뒤 절반 모두 매수 우위인지 확인한다."""
    mid = start_sec + (end_sec - start_sec) // 2
    full = flow_between(market, start_sec, end_sec)
    first = flow_between(market, start_sec, mid)
    second = flow_between(market, mid + 1, end_sec)
    ok = (
        positive_flow(full)
        and positive_flow(first, require_liquidity=False)
        and positive_flow(second, require_liquidity=False)
    )
    if compare_bid is not None:
        ok = ok and full["bid_value"] >= compare_bid
    return ok, full


def launch_current(market, sec):
    """3초 최고가가 아니라 마지막 체결가가 실제로 +4틱을 유지해야 한다."""
    evs = events_between(market, (sec - LAUNCH_SEC) * 1000, sec * 1000 + 999)
    if len(evs) < LAUNCH_MIN_TRADES:
        return False, 0, 0.0, 0
    bid_count = sum(1 for *_, side in evs if side == "BID")
    if bid_count < LAUNCH_MIN_BID_TRADES:
        return False, 0, evs[-1][1], bid_count
    start_price = evs[0][1]
    last_price = evs[-1][1]
    tick = v54.TICK_SIZE.get(market) or v54.krw_tick_size(last_price)
    ticks = int(round((last_price - start_price) / tick)) if tick else 0
    high = max(e[1] for e in evs)
    retrace_ticks = int(round((high - last_price) / tick)) if tick else 99
    return ticks >= LAUNCH_TICKS and retrace_ticks <= 1, ticks, last_price, bid_count


async def send_stage4(market, st, sec, price, ticks, flow):
    symbol = ticker(market)
    msg = (
        f"🚨 [{VERSION} 4차] {symbol} 발사 포착\n"
        f"현재가 {price:,.8g} | T 거래대금 {st.t_value_x:.2f}x\n"
        f"직전 10초 순매수 {flow['net'] / 1_000_000:.2f}백만원 | 체결 {flow['count']}건\n"
        f"3초 +{ticks}틱 유지 | 실제 추가 {POST_CONFIRM_SEC}초 확인 시작"
    )
    await r.send_telegram(msg)


async def send_stage5(market, st, price, flow):
    symbol = ticker(market)
    msg = (
        f"✅ [{VERSION} 5차] {symbol} 최종 확정\n"
        f"현재가 {price:,.8g} | 발사가 {st.launch_price:,.8g}\n"
        f"발사 후 {POST_CONFIRM_SEC}초 순매수 {flow['net'] / 1_000_000:.2f}백만원 "
        f"| 체결 {flow['count']}건\n"
        f"가격 유지 + 지속 매수 확인"
    )
    await r.send_telegram(msg)


async def evaluate_once(market, sec):
    st = ST.setdefault(market, V55State())
    if ticker(market) in STABLE_TICKERS:
        return

    vx, cur_value, _base = v54.minute_value_x(market, sec)
    recent = events_between(market, (sec - 2) * 1000, sec * 1000 + 999)
    current_price = recent[-1][1] if recent else 0.0

    if st.stage == 5:
        if sec >= st.cooldown_until:
            reset(st)
        return

    if st.stage == 0:
        if vx >= ENTRY_VALUE_X and cur_value >= ENTRY_MIN_VALUE_KRW and current_price:
            st.stage = 1
            st.t_sec = sec
            st.t_price = current_price
            st.t_value_x = vx
        return

    if st.stage == 1:
        age = sec - st.t_sec
        if age > CONFIRM_TIMEOUT_SEC:
            reset(st)
            return
        if age < CONFIRM_SEC:
            return
        before = flow_between(market, st.t_sec - FLOW_SEC, st.t_sec - 1)
        ok, flow = persistent_flow(market, st.t_sec + 1, st.t_sec + CONFIRM_SEC,
                                   compare_bid=before["bid_value"])
        if not ok:
            reset(st)
            return
        st.stage = 3
        st.confirm_sec = sec
        return

    if st.stage == 3:
        if sec - st.confirm_sec > WAIT_TTL_SEC:
            reset(st)
            return
        launched, ticks, price, _bid_count = launch_current(market, sec)
        if not launched:
            return
        flow_ok, flow = persistent_flow(market, sec - FLOW_SEC + 1, sec)
        if not flow_ok:
            return
        st.stage = 4
        st.launch_sec = sec
        st.launch_price = price
        st.launch_ticks = ticks
        await send_stage4(market, st, sec, price, ticks, flow)
        return

    if st.stage == 4:
        age = sec - st.launch_sec
        if age < POST_CONFIRM_SEC:
            return
        pre = flow_between(market, st.launch_sec - FLOW_SEC + 1, st.launch_sec)
        ok, flow = persistent_flow(
            market,
            st.launch_sec + 1,
            st.launch_sec + POST_CONFIRM_SEC,
            compare_bid=pre["bid_value"] * POST_BID_RETAIN,
        )
        tick = v54.TICK_SIZE.get(market) or v54.krw_tick_size(st.launch_price)
        retained = bool(current_price) and current_price >= st.launch_price - tick
        if ok and retained:
            st.stage = 5
            st.cooldown_until = sec + FINAL_COOLDOWN_SEC
            await send_stage5(market, st, current_price, flow)
        else:
            reset(st)


async def main():
    markets = await r.fetch_krw_markets()
    r.MARKETS = markets
    for market in markets:
        ST[market] = V55State()
        v54.TRADE_EVENTS.setdefault(market, __import__("collections").deque())
    await v54.fetch_tick_sizes()
    r.add_trade = v54.add_trade_v54
    r.evaluate_once = evaluate_once
    print(f"[{VERSION}] monitoring {len(markets)} KRW markets (no top-40 truncation)")
    await r.main()


if __name__ == "__main__":
    asyncio.run(main())
