#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Upbit Radar V5.2 safety wrapper over V5.1.

Purpose: stop the V5.1 alert loop where a stopped cycle immediately resets to
stage=0 and re-enters Stage 1 while the SAME Stage-1 condition is still alive.

Changes only the state/re-arm behavior. Core V5.1 thresholds and calculations
remain unchanged:
- Stage1: baseline >= 2.0x and previous same-window >= 2.5x
- downside noise tolerance: -8%
- Stage4: stage1 price +1.0%

V5.2 re-arm rule:
- after any stopped/failed cycle, that market is DISARMED;
- it cannot send a new Stage 1 while the old acceleration regime remains;
- it re-arms only after the Stage-1 gate has genuinely cleared/normalized:
  active windows == 0, best baseline ratio < 1.50x, best previous-window ratio < 1.50x;
- only a later NEW Stage-1 edge can alert again.

Noise control:
- a Stage-1-only failure is reset silently (no Telegram stop message);
- stop/cancel Telegram is sent only after Stage 2 or higher, where the user may
  actually be preparing to enter.

This file is isolated from live_api.py / archiver / GitHub publisher.
"""
from __future__ import annotations

import signal
import threading
from dataclasses import dataclass
from typing import Dict

import realtime_radar_v51 as r

REARM_BASELINE_X = 1.50
REARM_PREV_X = 1.50


@dataclass
class ReArmState:
    armed: bool = True
    last_stop_cycle: int = -1


REARM: Dict[str, ReArmState] = {}


def meta(market: str) -> ReArmState:
    state = REARM.get(market)
    if state is None:
        state = ReArmState()
        REARM[market] = state
    return state


def disarm_and_reset(market: str, st: r.CoinState) -> None:
    m = meta(market)
    m.armed = False
    m.last_stop_cycle = st.cycle_id
    r.reset(st)


def rearm_cleared(metrics: dict) -> bool:
    """State-based normalization, not a fixed cooldown timer."""
    best = metrics["best"]
    return (
        metrics["count"] == 0
        and best["base_x"] < REARM_BASELINE_X
        and best["prev_x"] < REARM_PREV_X
    )


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
        arm = meta(market)

        # A stopped cycle MUST first return to a genuinely normal regime.
        # The same still-active acceleration cannot create a fresh Stage 1.
        if st.stage == 0 and not arm.armed:
            if rearm_cleared(m):
                arm.armed = True
                print(
                    f"[rearm] {market} gate cleared "
                    f"base={m['best']['base_x']:.2f}x prev={m['best']['prev_x']:.2f}x",
                    flush=True,
                )
            continue

        # Existing-cycle deterioration.
        if st.stage > 0 and (m["dead"] or r.stage_deteriorated(m, st.last_snap)):
            reason = (
                "거래대금 가속 소멸/다중창 감속"
                if m["dead"]
                else "직전 단계 대비 -8% 허용범위를 넘어선 실질 감속"
            )

            # Stage 1 is only an observation alert. Avoid 1차→중지 Telegram spam.
            if st.stage >= 2:
                r.telegram(r.stop_text(m, st, reason))
                print("[stop-alert]", market, f"stage={st.stage}", reason, flush=True)
            else:
                print("[stop-silent]", market, "stage=1", reason, flush=True)

            disarm_and_reset(market, st)
            continue

        # Fresh Stage 1 is allowed only on an ARMED market.
        if st.stage == 0:
            if arm.armed and m["count"] >= 1 and m["price"] is not None:
                st.cycle_id += 1
                st.stage = 1
                st.stage1_price = m["price"]
                st.last_snap = r.snap(m, sec)
                r.telegram(r.alert(m, 1, st))
                print("[alert]", market, "stage=1", flush=True)
            continue

        # Same-second snapshot can never promote again.
        if sec <= st.last_snap.sec:
            continue

        if st.stage == 1:
            if (
                m["count"] >= 2
                and m["improving"]
                and r.genuinely_better(m, st.last_snap, 1, sec)
            ):
                st.stage = 2
                st.last_snap = r.snap(m, sec)
                r.telegram(r.alert(m, 2, st))
                print("[alert]", market, "stage=2", flush=True)

        elif st.stage == 2:
            if (
                m["count"] >= 3
                and m["confirmed"]
                and r.genuinely_better(m, st.last_snap, 2, sec)
            ):
                st.stage = 3
                st.last_snap = r.snap(m, sec)
                r.telegram(r.alert(m, 3, st))
                print("[alert]", market, "stage=3", flush=True)

        elif st.stage == 3:
            pr = r.price_from_stage1(st, m)
            if m["confirmed"] and r.genuinely_better(m, st.last_snap, 3, sec):
                if pr is not None and pr >= r.FINAL_PRICE_RETURN:
                    st.stage = 4
                    st.last_snap = r.snap(m, sec)
                    r.telegram(r.alert(m, 4, st))
                    print("[alert]", market, "stage=4", flush=True)
                else:
                    r.telegram(
                        r.stop_text(
                            m,
                            st,
                            f"최종 확인 실패: 1차 대비 +{r.FINAL_PRICE_RETURN:.1f}% 가격반응 미달",
                        )
                    )
                    print("[stop-alert]", market, "stage=3 price reaction fail", flush=True)
                    disarm_and_reset(market, st)


def main() -> None:
    signal.signal(signal.SIGINT, r.shutdown)
    signal.signal(signal.SIGTERM, r.shutdown)

    r.fetch_markets()
    for market in r.MARKETS:
        REARM[market] = ReArmState()

    # Make V5.1 evaluator loop call the V5.2 evaluator above.
    r.evaluate_once = evaluate_once

    print(
        f"[V5.2] warmup={r.WARMUP_SEC}s windows=1~10s "
        f"stage1 baseline>={r.STAGE1_BASELINE_X:.1f}x prev>={r.STAGE1_PREV_X:.1f}x "
        f"rearm<{REARM_BASELINE_X:.2f}x/{REARM_PREV_X:.2f}x "
        f"final_price=+{r.FINAL_PRICE_RETURN:.1f}%",
        flush=True,
    )

    thread = threading.Thread(target=r.evaluator_loop, name="v52-evaluator", daemon=True)
    thread.start()

    r.telegram(
        "✅ Upbit Radar V5.2 시작\n"
        "재진입 잠금 적용: 중단 후 동일 가속상태 재알림 금지\n"
        "재무장: 활성창 0 + 평시/직전창 모두 1.5x 미만으로 정상화 후\n"
        "새 급가속이 다시 발생할 때만 1차 재탐지\n"
        "1차 단독 실패의 중지 Telegram은 발송하지 않음\n"
        "기존 2.0x/2.5x · -8% 오차 · 4차 +1.0% 기준 유지"
    )

    r.websocket_loop()
    r.STOP.set()
    thread.join(timeout=3)


if __name__ == "__main__":
    main()
