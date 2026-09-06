#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Upbit Radar V5.5: strengthened launch, stage 6, and 240-minute T TTL."""
from __future__ import annotations

import collections
import json
import os
import signal
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

import realtime_radar_v54 as v54
from paper_trader import PaperPosition, PaperTrader

r = v54.r
VERSION = "V5.5"
ENTRY_VALUE_X = 2.8
T_TTL_SEC = 240 * 60
RESERVE_START_SEC = 120 * 60
DROP_HOLD_SEC = 20
LAUNCH_SEC = 3
LAUNCH_HIGH_TICKS = 4
LAUNCH_MIN_RISE_PCT = 0.40
LAUNCH_MIN_TRADES_10S = 7
LAUNCH_MIN_T_TICKS = 8
STAGE6_UP_TICKS = 3
STAGE6_DOWN_TICKS = 4
STAGE6_MAX_IDLE_SEC = 5
STAGE5_MAX_WAIT_SEC = 12 * 60 * 60
STAGE7_MIN_RISE_PCT = 0.50
STAGE7_MAX_WAIT_SEC = 30
STAGE8_MIN_RISE_PCT = 1.00
STAGE8_MAX_WAIT_SEC = 5 * 60
T02_DUPLICATE_SEC = 30 * 60
LATE_ENTRY_MIN_RETURN_PCT = 20.0
LATE_ENTRY_MIN_T_AGE_SEC = 30 * 60
N01_MAX_VALUE_10S_KRW = 4_000_000
N01_MAX_BID_SHARE = 0.68
F01_MIN_VALUE_10S_KRW = 25_000_000
F01_MAX_ACTIVE_SECONDS_10S = 7
F02_MIN_NET_PER_TRADE_KRW = 250_000
F02_MAX_MOVE_PCT_PER_VALUE_M = 0.0125
WARMUP_SEC = 11 * 60
EVENT_LOG_DIR = Path(os.getenv("RADAR_EVENT_DIR", "/home/ubuntu/upbit-scanner/data/live/radar_events"))
EXCLUDED_MARKETS = {"KRW-BTC"}
STABLE_TICKERS = {"USDT", "USDC", "USDG", "EURC"}


@dataclass
class V55State:
    stage: int = 0
    t_sec: int = 0
    t_price: Optional[float] = None
    t_value_x: float = 0.0
    drop_since: int = 0
    launch_armed: bool = True
    cycle_id: int = 0
    last_bad_count: int = 0
    stage5_sec: int = 0
    stage5_price: Optional[float] = None
    stage5_last_trade_sec: int = 0
    stage6_sec: int = 0
    stage6_price: Optional[float] = None
    stage7_sec: int = 0
    stage7_price: Optional[float] = None


ST: dict[str, V55State] = {}
RESERVE: dict[str, V55State] = {}
ENTRY_ARMED: dict[str, bool] = {}
LAST_STAGE8_SEC: dict[str, int] = {}
PREV_CLOSE: dict[str, float] = {}
PREV_CLOSE_KST_DAY = ""
PAPER: Optional[PaperTrader] = None


def paper_result_message(position: Optional[PaperPosition]) -> None:
    if position is None:
        return
    slip = ((position.average_price / position.signal_price - 1) * 100
            if position.signal_price else 0.0)
    r.telegram(
        f"🧪 V5.5 모의매수 {position.status}\n"
        f"종목: {position.market} | 주문시도: {position.attempts}회/2초\n"
        f"목표 투자금: {position.target_budget_krw:,.0f}원\n"
        f"체결금액: {position.gross_spent_krw:,.0f}원 | 수수료: {position.fee_krw:,.0f}원\n"
        f"평균가: {position.average_price:g} | 신호대비 슬리피지: {slip:+.3f}%\n"
        f"미사용 투자금: {position.unspent_budget_krw:,.0f}원\n"
        "실제 주문: 없음(PAPER ONLY)")


def refresh_prev_closes(sec: Optional[int] = None, force: bool = False) -> None:
    """Cache the current KST session open, which equals the prior session close."""
    global PREV_CLOSE_KST_DAY
    now_sec = int(sec if sec is not None else time.time())
    kst_day = datetime.fromtimestamp(now_sec, v54.KST).strftime("%Y%m%d")
    if not force and PREV_CLOSE_KST_DAY == kst_day and PREV_CLOSE:
        return
    loaded: dict[str, float] = {}
    try:
        for start in range(0, len(r.MARKETS), 100):
            markets = r.MARKETS[start:start + 100]
            if not markets:
                continue
            query = urllib.parse.urlencode({"markets": ",".join(markets)})
            for row in r.http_json(f"{r.UPBIT_REST}/ticker?{query}"):
                market = str(row.get("market") or "")
                opening = float(row.get("opening_price") or 0.0)
                if market and opening > 0:
                    loaded[market] = opening
        if loaded:
            PREV_CLOSE.clear()
            PREV_CLOSE.update(loaded)
            PREV_CLOSE_KST_DAY = kst_day
            print(f"[prior-close] {len(loaded)} markets day={kst_day}", flush=True)
    except Exception as exc:
        print(f"[prior-close error] {exc}", flush=True)


def late_entry_should_recycle(st: V55State, final_price: float, sec: int,
                              prior_close: Optional[float]) -> tuple[bool, Optional[float], int]:
    """Reject only stale signals already >=20% above the prior session close."""
    t_age_sec = sec - st.t_sec
    if not prior_close or prior_close <= 0:
        return False, None, t_age_sec
    return_pct = (final_price / prior_close - 1.0) * 100.0
    return (return_pct >= LATE_ENTRY_MIN_RETURN_PCT - 1e-12
            and t_age_sec >= LATE_ENTRY_MIN_T_AGE_SEC), return_pct, t_age_sec


def ticker(market: str) -> str:
    return market.split("-", 1)[-1]


def excluded(market: str) -> bool:
    return market in EXCLUDED_MARKETS or ticker(market) in STABLE_TICKERS


def log_event(market: str, event: str, sec: Optional[int] = None,
              st: Optional[V55State] = None, **fields) -> None:
    """Persist every transition; the server publisher pushes this directory."""
    try:
        event_sec = int(sec if sec is not None else time.time())
        state = st or ST.get(market)
        row = {
            "version": VERSION,
            "timestamp_kst": datetime.fromtimestamp(event_sec, v54.KST).isoformat(),
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
        day_dir = EVENT_LOG_DIR / datetime.fromtimestamp(event_sec, v54.KST).strftime("%Y%m%d")
        day_dir.mkdir(parents=True, exist_ok=True)
        line = json.dumps(row, ensure_ascii=False, separators=(",", ":"))
        with (day_dir / "v55_events.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
        tmp = EVENT_LOG_DIR / "latest.tmp"
        tmp.write_text(line, encoding="utf-8")
        os.replace(tmp, EVENT_LOG_DIR / "latest.json")
    except Exception as exc:
        print(f"[event-log error] {market} {event}: {exc}", flush=True)


def reset_state(market: str, why: str, sec: int) -> None:
    old = ST.get(market, V55State())
    if old.stage:
        log_event(market, "reset", sec, old, reason=why)
        print(f"[reset] {market} cycle={old.cycle_id} stage={old.stage} {why}", flush=True)
    ST[market] = V55State(cycle_id=old.cycle_id)
    RESERVE.pop(market, None)


def promote_reserve(market: str, sec: int, why: str) -> bool:
    reserve = RESERVE.pop(market, None)
    if reserve is None:
        return False
    old = ST.get(market)
    log_event(market, "t_handover", sec, reserve, reason=why,
              old_t_sec=old.t_sec if old else None,
              old_stage=old.stage if old else None)
    ST[market] = reserve
    print(f"[T 인계] {market} newT={reserve.t_sec} stage={reserve.stage} {why}", flush=True)
    return True


def retire_or_promote(market: str, sec: int, why: str) -> None:
    if not promote_reserve(market, sec, why):
        reset_state(market, why, sec)


def events_between(market: str, start_ms: int, end_ms: int) -> list[tuple]:
    return [e for e in v54.TRADE_EVENTS.get(market, ()) if start_ms <= e[0] <= end_ms]


def current_second_events(market: str, sec: int) -> list[tuple]:
    return events_between(market, sec * 1000, (sec + 1) * 1000 - 1)


def price_ticks_from_t(market: str, st: V55State, price: Optional[float]) -> Optional[float]:
    tick = v54.TICK_SIZE.get(market) or 0.0
    if tick <= 0 or not st.t_price or price is None:
        return None
    return (price - st.t_price) / tick


def price_pct_from_t(st: V55State, price: Optional[float]) -> Optional[float]:
    if not st.t_price or price is None:
        return None
    return (price / st.t_price - 1.0) * 100.0


def fmt_pct(value: Optional[float]) -> str:
    return "N/A" if value is None else f"{value:+.2f}%"


def n01_should_recycle(flow: dict) -> bool:
    """Recycle low-notional, weak-BID Stage 5 attempts without dropping T."""
    cur = flow["cur"]
    total10 = cur["bid"] + cur["ask"]
    return total10 <= N01_MAX_VALUE_10S_KRW and cur["share"] < N01_MAX_BID_SHARE


def f03_absorption_metrics(market: str, sec: int, flow: dict,
                           launch: dict) -> dict:
    cur = flow["cur"]
    total10 = cur["bid"] + cur["ask"]
    trades10 = cur["bid_count"] + cur["ask_count"]
    points = events_between(market, (sec - 9) * 1000, (sec + 1) * 1000 - 1)
    active_seconds10 = len({e[0] // 1000 for e in points})
    net_per_trade = cur["net"] / trades10 if trades10 else 0.0
    value_m = total10 / 1_000_000
    move_pct_per_value_m = launch["rise_pct"] / value_m if value_m > 0 else float("inf")
    f01 = total10 >= F01_MIN_VALUE_10S_KRW and active_seconds10 <= F01_MAX_ACTIVE_SECONDS_10S
    f02 = (net_per_trade >= F02_MIN_NET_PER_TRADE_KRW
           and move_pct_per_value_m <= F02_MAX_MOVE_PCT_PER_VALUE_M)
    return {"f01": f01, "f02": f02, "f03": f01 or f02,
            "total10": total10, "active_seconds10": active_seconds10,
            "net_per_trade": net_per_trade,
            "move_pct_per_value_m": move_pct_per_value_m}


def launch_metrics(market: str, sec: int, st: V55State, flow: dict) -> Optional[dict]:
    """Match the replay: 3s high +4 ticks, last-price %, 10s trades, T ticks."""
    tick = v54.TICK_SIZE.get(market) or 0.0
    if tick <= 0 or not st.t_price:
        return None
    pts = events_between(market, (sec - LAUNCH_SEC + 1) * 1000, (sec + 1) * 1000 - 1)
    if not pts:
        return None
    pts.sort(key=lambda x: x[0])
    start, high, last = pts[0][1], max(e[1] for e in pts), pts[-1][1]
    high_ticks = (high - start) / tick
    if high_ticks < LAUNCH_HIGH_TICKS - 1e-9:
        return None
    rise_pct = (last / start - 1.0) * 100.0 if start else 0.0
    t_ticks = (last - st.t_price) / tick
    cur = flow["cur"]
    trades10 = cur["bid_count"] + cur["ask_count"]
    return {
        "start": start, "high": high, "last": last,
        "high_ticks": high_ticks, "last_ticks": (last - start) / tick,
        "rise_pct": rise_pct, "t_ticks": t_ticks, "trades10": trades10,
        "ok": rise_pct >= LAUNCH_MIN_RISE_PCT - 1e-12
              and trades10 >= LAUNCH_MIN_TRADES_10S
              and t_ticks >= LAUNCH_MIN_T_TICKS - 1e-9,
    }


def drop_axes(market: str, st: V55State, flow: dict,
              price: Optional[float]) -> tuple[int, str]:
    pt = price_ticks_from_t(market, st, price)
    cur, prev = flow["cur"], flow["prev"]
    labels = []
    if pt is not None and pt <= -2.0:
        labels.append("price<=T-2tick")
    if cur["ask"] > cur["bid"] and cur["net"] < 0:
        labels.append("ASK>BID/net-")
    if cur["bid"] < prev["bid"] and cur["bid_count"] < prev["bid_count"]:
        labels.append("BID-value/count-down")
    return len(labels), ",".join(labels)


def alert4(market: str, st: V55State, vx: float, flow: dict, launch: dict) -> str:
    cur = flow["cur"]
    return (f"{r.title(market)}\n🚨 V5.5 4차 강화 발사 감지\n"
            f"현재가: {launch['last']} | T 대비: {launch['t_ticks']:+.1f}틱\n"
            f"3초 고가: +{launch['high_ticks']:.1f}틱 | 마지막가 상승: {launch['rise_pct']:+.2f}%\n"
            f"10초 체결: {launch['trades10']}건 | BID: {cur['share']*100:.1f}%\n"
            f"T 배수: {st.t_value_x:.2f}x | 현재 배수: {vx:.2f}x")


def alert5(market: str, st: V55State, vx: float, flow: dict, launch: dict) -> str:
    cur = flow["cur"]
    return (f"{r.title(market)}\n✅ V5.5 5차 매수방향 확인\n"
            f"기준가: {launch['last']} | T 대비: {fmt_pct(price_pct_from_t(st, launch['last']))}\n"
            f"BID 10초: {cur['share']*100:.1f}% | 순매수: {r.fmt_money(cur['net'])}\n"
            f"현재 거래대금: {vx:.2f}x\n상태: +3틱 출발 확인 대기")


def alert8(market: str, st: V55State, final_price: float, sec: int) -> str:
    elapsed7 = st.stage7_sec - st.stage6_sec
    elapsed8 = sec - st.stage6_sec
    return (f"{r.title(market)}\n🔥 V5.5 8차 최종 매수확정\n"
            f"확정가: {final_price} | 6차 기준가: {st.stage6_price}\n"
            f"7차 +{STAGE7_MIN_RISE_PCT:.1f}%: {elapsed7}초 | "
            f"8차 +{STAGE8_MIN_RISE_PCT:.1f}%: {elapsed8}초\n"
            f"T 대비: {fmt_pct(price_pct_from_t(st, final_price))}\n결과: 최종 매수 신호")


def return_to_stage3(market: str, st: V55State, sec: int, reason: str, **fields) -> None:
    log_event(market, "stage5_to_stage3", sec, st, reason=reason, **fields)
    if sec - st.t_sec >= T_TTL_SEC:
        if not promote_reserve(market, sec, f"5차 실패 후 만료 T 인계: {reason}"):
            reset_state(market, f"5차 실패 후 만료 T 소멸: {reason}", sec)
        return
    st.stage = 3
    st.stage5_sec = 0
    st.stage5_price = None
    st.stage5_last_trade_sec = 0
    st.launch_armed = False
    print(f"[5차->3차] {market} {reason}", flush=True)


def evaluate_stage5(market: str, sec: int, st: V55State) -> None:
    if sec - st.stage5_sec >= STAGE5_MAX_WAIT_SEC:
        retire_or_promote(market, sec, "5차 틱 판정 12시간 미발생")
        return
    tick = v54.TICK_SIZE.get(market) or 0.0
    evs = current_second_events(market, sec)
    if not evs:
        if sec - st.stage5_last_trade_sec > STAGE6_MAX_IDLE_SEC:
            return_to_stage3(market, st, sec, "5초 체결정체")
        return
    st.stage5_last_trade_sec = sec
    high, low = max(e[1] for e in evs), min(e[1] for e in evs)
    up = high >= st.stage5_price + STAGE6_UP_TICKS * tick - 1e-12
    down = low <= st.stage5_price - STAGE6_DOWN_TICKS * tick + 1e-12
    if up and down:
        return_to_stage3(market, st, sec, "동일초 +3/-4 동시도달", high=high, low=low)
    elif up:
        final_price = st.stage5_price + STAGE6_UP_TICKS * tick
        elapsed = sec - st.stage5_sec
        st.stage = 6
        st.stage6_sec, st.stage6_price = sec, final_price
        RESERVE.pop(market, None)
        log_event(market, "stage6", sec, st, final_price=final_price,
                  stage5_price=st.stage5_price, elapsed_sec=elapsed)
        print(f"[6차] {market} internal price={final_price} elapsed={elapsed}s; 7차 대기", flush=True)
    elif down:
        return_to_stage3(market, st, sec, "5차 기준 -4틱", high=high, low=low)


def advance_reserve(market: str, sec: int, price: float, vx: float, flow: dict) -> None:
    """Advance a silent reserve T through stage 5 without replacing the primary."""
    st = RESERVE.get(market)
    if st is None:
        return
    if st.stage < 5 and sec - st.t_sec >= T_TTL_SEC:
        log_event(market, "reserve_expire", sec, st, reason="reserve T 240분 소멸")
        RESERVE.pop(market, None)
        return
    if st.stage == 1:
        pt = price_ticks_from_t(market, st, price)
        cur, prev = flow["cur"], flow["prev"]
        if pt is not None and pt <= -2.0 or vx < ENTRY_VALUE_X:
            RESERVE.pop(market, None)
        elif cur["bid"] > cur["ask"] and cur["net"] > 0 and cur["bid"] > prev["bid"]:
            st.stage = 3
            log_event(market, "reserve_stage3", sec, st, price=price, current_value_x=vx)
        return
    if st.stage != 3:
        return
    bad_count, _reason = drop_axes(market, st, flow, price)
    if bad_count >= 2:
        if not st.drop_since:
            st.drop_since = sec
        if sec - st.drop_since + 1 >= DROP_HOLD_SEC:
            RESERVE.pop(market, None)
            return
    else:
        st.drop_since = 0
    launch = launch_metrics(market, sec, st, flow)
    if launch is None:
        st.launch_armed = True
        return
    if not st.launch_armed:
        return
    st.launch_armed = False
    cur = flow["cur"]
    if launch["ok"] and cur["bid"] > cur["ask"] and cur["net"] > 0:
        st.stage, st.stage5_sec = 5, sec
        st.stage5_price, st.stage5_last_trade_sec = launch["last"], sec
        log_event(market, "reserve_stage5", sec, st, price=launch["last"], **launch)
        f03 = f03_absorption_metrics(market, sec, flow, launch)
        if f03["f03"]:
            log_event(market, "reserve_stage5_to_stage3", sec, st,
                      reason="F03 매수흡수", **f03)
            st.stage = 3
            st.stage5_sec = st.stage5_last_trade_sec = 0
            st.stage5_price = None
            return
        if n01_should_recycle(flow):
            total10 = cur["bid"] + cur["ask"]
            log_event(market, "reserve_stage5_to_stage3", sec, st,
                      reason="N01 10초 400만원 이하+BID 68% 미만",
                      total10=total10, bid_share=cur["share"])
            st.stage = 3
            st.stage5_sec = 0
            st.stage5_price = None
            st.stage5_last_trade_sec = 0


def return_after_confirmation(market: str, st: V55State, sec: int, reason: str) -> None:
    """A failed 7/8 confirmation is a recycle, not a permanent rejection."""
    log_event(market, "confirmation_to_stage3", sec, st, reason=reason,
              stage6_price=st.stage6_price, stage6_sec=st.stage6_sec,
              stage7_price=st.stage7_price, stage7_sec=st.stage7_sec)
    if sec - st.t_sec >= T_TTL_SEC:
        retire_or_promote(market, sec, f"7/8차 실패 후 T 만료: {reason}")
        return
    st.stage = 3
    st.stage5_sec = st.stage5_last_trade_sec = st.stage6_sec = st.stage7_sec = 0
    st.stage5_price = st.stage6_price = st.stage7_price = None
    st.launch_armed = False
    print(f"[7/8차->3차] {market} {reason}", flush=True)


def evaluate_confirmation(market: str, sec: int, st: V55State,
                          price: Optional[float]) -> None:
    if not st.stage6_price:
        return_after_confirmation(market, st, sec, "6차 기준가 없음")
        return
    evs = current_second_events(market, sec)
    high = max((e[1] for e in evs), default=price)
    if st.stage == 6:
        if high is not None and high >= st.stage6_price * (1 + STAGE7_MIN_RISE_PCT / 100) - 1e-12:
            st.stage = 7
            st.stage7_sec, st.stage7_price = sec, high
            log_event(market, "stage7", sec, st, price=high,
                      stage6_price=st.stage6_price,
                      elapsed_sec=sec - st.stage6_sec,
                      rise_pct=(high / st.stage6_price - 1) * 100)
            print(f"[7차] {market} +0.5% confirmed", flush=True)
        elif sec - st.stage6_sec >= STAGE7_MAX_WAIT_SEC:
            return_after_confirmation(market, st, sec, "6차 후 30초 내 +0.5% 미달")
        return
    if st.stage == 7:
        if high is not None and high >= st.stage6_price * (1 + STAGE8_MIN_RISE_PCT / 100) - 1e-12:
            final_price = high
            late, prior_return_pct, t_age_sec = late_entry_should_recycle(
                st, final_price, sec, PREV_CLOSE.get(market))
            if late:
                log_event(market, "stage8_late_entry_recycle", sec, st,
                          final_price=final_price,
                          prior_close=PREV_CLOSE.get(market),
                          prior_close_return_pct=prior_return_pct,
                          t_age_sec=t_age_sec,
                          threshold_return_pct=LATE_ENTRY_MIN_RETURN_PCT,
                          threshold_t_age_sec=LATE_ENTRY_MIN_T_AGE_SEC)
                return_after_confirmation(
                    market, st, sec,
                    f"후행 8차: 전일종가 대비 {prior_return_pct:+.2f}%/"
                    f"T {t_age_sec//60}분 경과")
                return
            st.stage = 8
            previous = LAST_STAGE8_SEC.get(market)
            duplicate = previous is not None and sec - previous < T02_DUPLICATE_SEC
            log_event(market, "stage8", sec, st, final_price=final_price,
                      target_price=st.stage6_price * (1 + STAGE8_MIN_RISE_PCT / 100),
                      observed_price=high, stage6_price=st.stage6_price,
                      elapsed_sec=sec - st.stage6_sec, t02_duplicate=duplicate,
                      previous_stage8_sec=previous)
            if duplicate:
                log_event(market, "t02_duplicate_block", sec, st,
                          previous_stage8_sec=previous,
                          elapsed_sec=sec - previous)
                print(f"[8차/T02] {market} duplicate blocked", flush=True)
            else:
                LAST_STAGE8_SEC[market] = sec
                r.telegram(alert8(market, st, final_price, sec))
                if PAPER is not None:
                    started = PAPER.buy_async(market, final_price, paper_result_message)
                    log_event(market, "paper_buy_started" if started else "paper_buy_skipped",
                              sec, st, reason=None if started else "position_or_order_already_active")
                print(f"[8차] {market} FINAL price={final_price}", flush=True)
        elif sec - st.stage6_sec >= STAGE8_MAX_WAIT_SEC:
            return_after_confirmation(market, st, sec, "6차 후 5분 내 +1.0% 미달")


def evaluate_market(market: str, sec: int) -> None:
    st = ST.setdefault(market, V55State())
    if excluded(market):
        return
    price = v54.current_price(market, sec)
    try:
        vx, _cur_value, _base = v54.minute_value_x(market, sec)
        flow = v54.flow10(market, sec)
    except Exception as exc:
        print("[v55 metric error]", market, exc, flush=True)
        return

    entry_armed = ENTRY_ARMED.setdefault(market, True)
    if vx < ENTRY_VALUE_X:
        ENTRY_ARMED[market] = True
    elif st.stage == 0:
        ENTRY_ARMED[market] = False
    elif (st.stage in {1, 3, 4, 5} and price is not None
          and sec - st.t_sec >= RESERVE_START_SEC
          and market not in RESERVE and entry_armed):
        reserve = V55State(stage=1, t_sec=sec, t_price=price,
                           t_value_x=vx, cycle_id=st.cycle_id + 1)
        RESERVE[market] = reserve
        ENTRY_ARMED[market] = False
        log_event(market, "reserve_stage1", sec, reserve, price=price, current_value_x=vx)

    if price is not None and market in RESERVE:
        advance_reserve(market, sec, price, vx, flow)

    # Stage 5 is TTL-exempt. After 240m it hands over only to an equally
    # validated reserve stage 5; otherwise its current barrier attempt continues.
    st = ST[market]
    reserve = RESERVE.get(market)
    if (st.stage == 5 and sec - st.t_sec >= T_TTL_SEC
            and reserve is not None and reserve.stage == 5):
        promote_reserve(market, sec, "240분 후 동급 5차 인계")
        return
    if 0 < st.stage < 5 and sec - st.t_sec >= T_TTL_SEC:
        if not promote_reserve(market, sec, "기존 T 240분 소멸 후 예비 T 인계"):
            reset_state(market, "T 240분 소멸", sec)
        return
    if st.stage == 5:
        evaluate_stage5(market, sec, st)
        return
    if price is None:
        return
    if st.stage in {6, 7}:
        evaluate_confirmation(market, sec, st, price)
        return
    if st.stage == 8:
        if vx < 1.0:
            reset_state(market, "8차 완료 후 거래대금 정상화", sec)
        return
    if st.stage == 0:
        if vx >= ENTRY_VALUE_X:
            st.cycle_id += 1
            st.stage, st.t_sec, st.t_price, st.t_value_x = 1, sec, price, vx
            log_event(market, "stage1", sec, st, price=price, current_value_x=vx)
            print(f"[1차] {market} cycle={st.cycle_id} T={price} value={vx:.2f}x", flush=True)
        return
    if st.stage == 1:
        pt = price_ticks_from_t(market, st, price)
        if pt is not None and pt <= -2.0:
            retire_or_promote(market, sec, "2차 전 T-2틱 이탈")
            return
        cur, prev = flow["cur"], flow["prev"]
        if cur["bid"] > cur["ask"] and cur["net"] > 0 and cur["bid"] > prev["bid"]:
            st.stage = 3
            log_event(market, "stage2", sec, st, price=price, current_value_x=vx,
                      bid=cur["bid"], ask=cur["ask"], net=cur["net"],
                      bid_count=cur["bid_count"], prev_bid=prev["bid"])
            log_event(market, "stage3", sec, st, reason="stage2_pass")
            print(f"[2차->3차] {market} bid={cur['share']*100:.1f}% net={cur['net']:.0f}", flush=True)
        elif vx < ENTRY_VALUE_X:
            retire_or_promote(market, sec, "2차 미충족 + 2.8x 해제")
        return

    bad_count, bad_reason = drop_axes(market, st, flow, price)
    if bad_count != st.last_bad_count:
        log_event(market, "drop_axes_change", sec, st, bad_count=bad_count,
                  bad_reason=bad_reason, price=price)
        st.last_bad_count = bad_count
    if bad_count >= 2:
        if not st.drop_since:
            st.drop_since = sec
        held = sec - st.drop_since + 1
        if held >= DROP_HOLD_SEC:
            log_event(market, "drop", sec, st, held_sec=held, bad_reason=bad_reason, price=price)
            retire_or_promote(market, sec, f"DROP {held}s {bad_reason}")
            return
    else:
        st.drop_since = 0

    launch = launch_metrics(market, sec, st, flow)
    if launch is None:
        st.launch_armed = True
        return
    if not st.launch_armed:
        return
    st.launch_armed = False
    if not launch["ok"]:
        log_event(market, "stage4_reject", sec, st, **launch)
        return
    st.stage = 4
    log_event(market, "stage4", sec, st, current_value_x=vx, **launch)
    print(f"[4차] {market} high={launch['high_ticks']:.1f}tick rise={launch['rise_pct']:.2f}%", flush=True)
    cur = flow["cur"]
    if cur["bid"] > cur["ask"] and cur["net"] > 0:
        st.stage, st.stage5_sec = 5, sec
        st.stage5_price, st.stage5_last_trade_sec = launch["last"], sec
        log_event(market, "stage5", sec, st, price=launch["last"], current_value_x=vx,
                  bid=cur["bid"], ask=cur["ask"], net=cur["net"],
                  bid_share=cur["share"], **launch)
        f03 = f03_absorption_metrics(market, sec, flow, launch)
        if f03["f03"]:
            return_to_stage3(market, st, sec, "F03 매수흡수", **f03)
            return
        if n01_should_recycle(flow):
            total10 = cur["bid"] + cur["ask"]
            return_to_stage3(market, st, sec,
                             "N01 10초 400만원 이하+BID 68% 미만",
                             total10=total10, bid_share=cur["share"])
            return
        print(f"[5차] {market} +3틱 대기", flush=True)
    else:
        st.stage = 3
        log_event(market, "stage4_to_stage3", sec, st, reason="stage5_flow_fail")
        print(f"[4차->3차] {market} 5차 매수방향 미달", flush=True)


def evaluate_once() -> None:
    sec = int(time.time()) - 1
    refresh_prev_closes(sec)
    if time.time() - r.STARTED_AT < WARMUP_SEC:
        return
    for market in r.MARKETS:
        evaluate_market(market, sec)


def main() -> None:
    global PAPER
    signal.signal(signal.SIGINT, r.shutdown)
    signal.signal(signal.SIGTERM, r.shutdown)
    r.fetch_markets()
    refresh_prev_closes(force=True)
    for market in r.MARKETS:
        ST[market] = V55State()
        ENTRY_ARMED[market] = True
        v54.TRADE_EVENTS[market] = collections.deque()
    v54.fetch_tick_sizes()
    PAPER = PaperTrader(r.http_json, r.UPBIT_REST)
    r.add_trade = v54.add_trade_v54
    r.evaluate_once = evaluate_once
    print(f"[{VERSION}] all KRW | T={ENTRY_VALUE_X:.1f}x/{T_TTL_SEC//60}m "
          f"reserve-from={RESERVE_START_SEC//60}m | "
          f"4차=3s high+4tick,rise>=0.40%,trades10>=7,T+8tick | "
          f"6차=+3tick | 7차=+0.5%/30s | 8차=+1.0%/5m | "
          f"T02={T02_DUPLICATE_SEC//60}m | F03 absorption | "
          f"N01<=400만원&BID<68% | 후행8차=전일종가+20%&T30m->3차", flush=True)
    r.telegram("✅ Upbit Radar V5.5 시작\nT 유효기간 240분 · 120분부터 예비 T · BTC/스테이블 제외\n"
               "1~7차는 내부 로그 전용 · 8차(+1.0%)만 Telegram 최종확정\n"
               "6차 후 +0.5% 최대30초→7차 · +1.0% 최대5분→8차\n"
               "같은 종목 30분 내 반복 8차는 T02로 알림 차단\n"
               "F03 매수흡수 감지 시 3차 복귀\n"
               "5차는 TTL 예외(최대 12시간) · -4틱/5초 무체결 시 3차 복귀\n"
               "N01: 10초 총체결 400만원 이하+BID 68% 미만이면 3차 복귀\n"
               "후행 8차: 전일종가 +20% 이상且 T 30분 경과 시 3차 복귀")
    th = threading.Thread(target=r.evaluator_loop, name="v55-evaluator", daemon=True)
    th.start()
    r.websocket_loop()
    r.STOP.set()
    th.join(timeout=3)


if __name__ == "__main__":
    main()
