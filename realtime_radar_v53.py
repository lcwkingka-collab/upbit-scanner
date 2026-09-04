#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Upbit Radar V5.3

Five internal stages: 0 -> 1 -> 2 -> 3 -> 4

User-visible behavior:
- Stage 0: internal learning/log only, no Telegram
- Stage 1: internal learning/log only, no Telegram
- Stage 2: internal learning/log only, no Telegram
- Stage 3: first Telegram = "⚠️ 1차 유의"
- Stage 4: second Telegram = "🔥 2차 발사확인"
- If a cycle dies at Stage 3 or Stage 4, send "⛔ 발사 중단"
- If a cycle dies at Stage 0/1/2, reset silently

Every promotion must use a later snapshot. No same-second multi-stage promotion.
Stage 4 keeps the existing final price confirmation: price >= +1.0% from Stage 0.
The underlying V5.1 acceleration metrics and -8% downside tolerance remain unchanged.
"""
from __future__ import annotations

import signal
import threading

import realtime_radar_v51 as r


def price_from_stage0(st: r.CoinState, m: dict):
    return r.price_from_stage1(st, m)


def metrics_log(m: dict, st: r.CoinState, label: str) -> None:
    b = m["best"]
    pr = price_from_stage0(st, m)
    print(
        f"[{label}] {m['market']} stage={st.stage} "
        f"active={m['count']} w={b['w']} "
        f"base={b['base_x']:.2f}x prev={b['prev_x']:.2f}x "
        f"bid={m['share']*100:.1f}% net={m['net']:.0f} "
        f"price0={(f'{pr:+.2f}%' if pr is not None else 'N/A')}",
        flush=True,
    )


def alert_text(m: dict, visible_stage: int, st: r.CoinState) -> str:
    b = m["best"]
    pr = price_from_stage0(st, m)
    heads = {
        1: "⚠️ 1차 유의",
        2: "🔥 2차 발사확인",
    }
    why = {
        1: "내부 0→1→2차 검증 후 3차까지 연속 강화 확인",
        2: "3차 이후 추가 강화 + 0차 대비 실제 가격반응 확인",
    }
    return (
        f"{r.title(m['market'])}\n"
        f"{heads[visible_stage]}\n"
        f"{why[visible_stage]}\n"
        f"감지창: {b['w']}초 | 활성창: {m['count']}개\n"
        f"평시 대비: {b['base_x']:.2f}x\n"
        f"직전 동일창 대비: {b['prev_x']:.2f}x\n"
        f"BID 10초: {m['share'] * 100:.1f}% | 순매수: {r.fmt_money(m['net'])}\n"
        f"0차 대비 가격: {(f'{pr:+.2f}%' if pr is not None else 'N/A')}"
    )


def stop_text(m: dict, st: r.CoinState, reason: str) -> str:
    pr = price_from_stage0(st, m)
    return (
        f"{r.title(m['market'])}\n"
        f"⛔ 발사 중단\n"
        f"{reason}\n"
        f"중단 내부단계: {st.stage}차\n"
        f"0차 대비 가격: {(f'{pr:+.2f}%' if pr is not None else 'N/A')}\n"
        f"새 급가속 발생 시 0차부터 다시 실시간 탐색"
    )


def reset(st: r.CoinState) -> None:
    r.reset(st)


def promote(st: r.CoinState, m: dict, sec: int, new_stage: int) -> None:
    st.stage = new_stage
    st.last_snap = r.snap(m, sec)
    metrics_log(m, st, f"stage{new_stage}")


def evaluate_once() -> None:
    sec = int(r.time.time()) - 1

    for market in r.MARKETS:
        try:
            m = r.compute(market, sec)
        except Exception as exc:
            print("[metric error]", market, exc, flush=True)
            continue
        if not m:
            continue

        st = r.STATES[market]

        # Deterioration: visible stop only after the user has already received Stage 3.
        if st.last_snap.sec > 0 and (m["dead"] or r.stage_deteriorated(m, st.last_snap)):
            reason = (
                "거래대금 가속 소멸/다중창 감속"
                if m["dead"]
                else "직전 단계 대비 -8% 허용범위를 넘어선 실질 감속"
            )
            if st.stage >= 3:
                r.telegram(stop_text(m, st, reason))
                print("[stop-alert]", market, f"stage={st.stage}", reason, flush=True)
            else:
                print("[stop-silent]", market, f"stage={st.stage}", reason, flush=True)
            reset(st)
            continue

        # Idle -> internal Stage 0. Original V5.1 first-acceleration gate.
        # Stage 0 active is distinguished from idle by last_snap.sec > 0.
        if st.stage == 0 and st.last_snap.sec == 0:
            if m["count"] >= 1 and m["price"] is not None:
                st.cycle_id += 1
                st.stage = 0
                st.stage1_price = m["price"]  # Stage-0 anchor price
                st.last_snap = r.snap(m, sec)
                metrics_log(m, st, "stage0")
            continue

        # Never promote multiple stages on the same snapshot.
        if sec <= st.last_snap.sec:
            continue

        # Stage 0 -> Stage 1: first internal confirmation.
        if st.stage == 0:
            if (
                m["count"] >= 2
                and m["improving"]
                and r.genuinely_better(m, st.last_snap, 1, sec)
            ):
                promote(st, m, sec, 1)
            continue

        # Stage 1 -> Stage 2: stronger internal confirmation.
        if st.stage == 1:
            if (
                m["count"] >= 3
                and m["confirmed"]
                and r.genuinely_better(m, st.last_snap, 2, sec)
            ):
                promote(st, m, sec, 2)
            continue

        # Stage 2 -> Stage 3: first user-visible alert.
        if st.stage == 2:
            if (
                m["count"] >= 3
                and m["confirmed"]
                and r.genuinely_better(m, st.last_snap, 3, sec)
            ):
                promote(st, m, sec, 3)
                r.telegram(alert_text(m, 1, st))
                print("[alert]", market, "internal-stage=3 visible=1차", flush=True)
            continue

        # Stage 3 -> Stage 4: second user-visible alert, with existing +1% price confirmation.
        if st.stage == 3:
            pr = price_from_stage0(st, m)
            if m["confirmed"] and r.genuinely_better(m, st.last_snap, 3, sec):
                if pr is not None and pr >= r.FINAL_PRICE_RETURN:
                    promote(st, m, sec, 4)
                    r.telegram(alert_text(m, 2, st))
                    print("[alert]", market, "internal-stage=4 visible=2차", flush=True)
                else:
                    # Do not stop immediately just because price has not reached +1% yet.
                    # Keep Stage 3 alive until either the price confirms or the cycle truly deteriorates.
                    metrics_log(m, st, "stage3-wait-price")
            continue

        # Stage 4 remains active until deterioration; stop handling is above.


def main() -> None:
    signal.signal(signal.SIGINT, r.shutdown)
    signal.signal(signal.SIGTERM, r.shutdown)

    r.fetch_markets()
    r.evaluate_once = evaluate_once

    print(
        f"[V5.3] internal=0→1→2→3→4 "
        f"stage0 baseline>={r.STAGE1_BASELINE_X:.1f}x prev>={r.STAGE1_PREV_X:.1f}x "
        f"downside_tolerance=-{r.DOWNSIDE_TOLERANCE*100:.0f}% "
        f"stage4_price=stage0+{r.FINAL_PRICE_RETURN:.1f}%",
        flush=True,
    )

    thread = threading.Thread(target=r.evaluator_loop, name="v53-evaluator", daemon=True)
    thread.start()

    r.telegram(
        "✅ Upbit Radar V5.3 시작\n"
        "0차: 내부 탐색/학습로그 · 무음\n"
        "1차: 내부 강화/학습로그 · 무음\n"
        "2차: 내부 재확인/학습로그 · 무음\n"
        "3차: Telegram ⚠️ 1차 유의\n"
        "4차: Telegram 🔥 2차 발사확인 · 0차 대비 +1.0% 확인\n"
        "0~2차 실패는 무음 · 3/4차 실패는 ⛔ 발사 중단"
    )

    r.websocket_loop()
    r.STOP.set()
    thread.join(timeout=3)


if __name__ == "__main__":
    main()
