#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Upbit Radar V5.2: silent internal Stage 0 -> Telegram Stages 1..3.

Agreed state machine:
0) INTERNAL ONLY: original first acceleration gate (baseline >=2.0x, prev >=2.5x).
   No Telegram. If it dies, reset silently and immediately allow a fresh Stage 0.
1) First Telegram: later NEW data improves/broadens the Stage-0 acceleration.
   If it dies, reset silently. No stop Telegram.
2) Second Telegram: later NEW data strengthens/re-accelerates again.
   From here, a real deterioration sends a stop/cancel Telegram.
3) Final Telegram: later NEW data remains strong and price is >= +1.0% from
   the original Stage-0 price.

No fixed cooldown/re-arm timer. Every new cycle must always begin again at
internal Stage 0. -8% remains downside noise tolerance only.

This file wraps realtime_radar_v51 calculations and is isolated from the Live
API, archiver and GitHub publisher.
"""
from __future__ import annotations

import signal
import threading

import realtime_radar_v51 as r


def user_price_from_stage0(st: r.CoinState, m: dict):
    return r.price_from_stage1(st, m)  # V5.1 field stores the Stage-0 anchor price.


def alert_text(m: dict, stage: int, st: r.CoinState) -> str:
    b = m["best"]
    pr = user_price_from_stage0(st, m)
    heads = {
        1: "⚠️ 1차 유의",
        2: "🔥 2차 발사준비",
        3: "🚀 3차 최종 매수확인",
    }
    why = {
        1: "0차 내부탐색 이후 새 데이터에서 추가 가속/품질 개선",
        2: "1차 이후 새 데이터에서 재가속/강화",
        3: "2차 이후 가속 지속 + 실제 가격반응 확인",
    }
    return (
        f"{r.title(m['market'])}\n"
        f"{heads[stage]}\n"
        f"{why[stage]}\n"
        f"감지창: {b['w']}초 | 활성창: {m['count']}개\n"
        f"평시 대비: {b['base_x']:.2f}x\n"
        f"직전 동일창 대비: {b['prev_x']:.2f}x\n"
        f"BID 10초: {m['share'] * 100:.1f}% | 순매수: {r.fmt_money(m['net'])}\n"
        f"0차 대비 가격: {(f'{pr:+.2f}%' if pr is not None else 'N/A')}"
    )


def stop_text(m: dict, st: r.CoinState, reason: str) -> str:
    pr = user_price_from_stage0(st, m)
    return (
        f"{r.title(m['market'])}\n"
        f"⛔ 발사 중단\n"
        f"{reason}\n"
        f"0차 대비 가격: {(f'{pr:+.2f}%' if pr is not None else 'N/A')}\n"
        f"새 급가속 발생 시 0차부터 다시 실시간 탐색"
    )


def silent_reset(st: r.CoinState) -> None:
    r.reset(st)


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

        # Any stage can die. Stage 0/1 are silent; only Stage 2+ warrants a stop alert.
        if st.stage >= 0 and st.last_snap.sec > 0 and (
            m["dead"] or r.stage_deteriorated(m, st.last_snap)
        ):
            reason = (
                "거래대금 가속 소멸/다중창 감속"
                if m["dead"]
                else "직전 단계 대비 -8% 허용범위를 넘어선 실질 감속"
            )
            if st.stage >= 2:
                r.telegram(stop_text(m, st, reason))
                print("[stop-alert]", market, f"stage={st.stage}", reason, flush=True)
            else:
                print("[stop-silent]", market, f"stage={st.stage}", reason, flush=True)
            silent_reset(st)
            continue

        # Internal Stage 0. Original V5.1 Stage-1 gate; NEVER Telegram.
        # CoinState stage=0 is also idle, so last_snap.sec distinguishes idle from active Stage 0.
        if st.stage == 0 and st.last_snap.sec == 0:
            if m["count"] >= 1 and m["price"] is not None:
                st.cycle_id += 1
                st.stage = 0
                st.stage1_price = m["price"]  # anchor = Stage-0 price
                st.last_snap = r.snap(m, sec)
                print("[stage0]", market, "internal candidate", flush=True)
            continue

        # Every promotion must consume a later snapshot.
        if sec <= st.last_snap.sec:
            continue

        # Internal Stage 0 -> Telegram Stage 1.
        if st.stage == 0:
            if (
                m["count"] >= 2
                and m["improving"]
                and r.genuinely_better(m, st.last_snap, 1, sec)
            ):
                st.stage = 1
                st.last_snap = r.snap(m, sec)
                r.telegram(alert_text(m, 1, st))
                print("[alert]", market, "stage=1", flush=True)

        # Telegram Stage 1 -> Telegram Stage 2.
        elif st.stage == 1:
            if (
                m["count"] >= 3
                and m["confirmed"]
                and r.genuinely_better(m, st.last_snap, 2, sec)
            ):
                st.stage = 2
                st.last_snap = r.snap(m, sec)
                r.telegram(alert_text(m, 2, st))
                print("[alert]", market, "stage=2", flush=True)

        # Telegram Stage 2 -> final Stage 3.
        elif st.stage == 2:
            pr = user_price_from_stage0(st, m)
            if m["confirmed"] and r.genuinely_better(m, st.last_snap, 3, sec):
                if pr is not None and pr >= r.FINAL_PRICE_RETURN:
                    st.stage = 3
                    st.last_snap = r.snap(m, sec)
                    r.telegram(alert_text(m, 3, st))
                    print("[alert]", market, "stage=3 final", flush=True)
                else:
                    # Stage 2 was already actionable, so failure of final price confirmation is useful.
                    r.telegram(
                        stop_text(
                            m,
                            st,
                            f"최종 확인 실패: 0차 대비 +{r.FINAL_PRICE_RETURN:.1f}% 가격반응 미달",
                        )
                    )
                    print("[stop-alert]", market, "stage=2 final-price-fail", flush=True)
                    silent_reset(st)

        # Final Stage 3 remains active until it deteriorates; deterioration is handled above.


def main() -> None:
    signal.signal(signal.SIGINT, r.shutdown)
    signal.signal(signal.SIGTERM, r.shutdown)

    r.fetch_markets()
    r.evaluate_once = evaluate_once

    print(
        f"[V5.2] silent-stage0 baseline>={r.STAGE1_BASELINE_X:.1f}x "
        f"prev>={r.STAGE1_PREV_X:.1f}x windows=1~10s "
        f"downside_tolerance=-{r.DOWNSIDE_TOLERANCE*100:.0f}% "
        f"final=stage0+{r.FINAL_PRICE_RETURN:.1f}%",
        flush=True,
    )

    thread = threading.Thread(target=r.evaluator_loop, name="v52-evaluator", daemon=True)
    thread.start()

    r.telegram(
        "✅ Upbit Radar V5.2 시작\n"
        "0차: 내부 탐색 전용 · Telegram 없음\n"
        "1차: 첫 Telegram · 0차 이후 새 가속 확인\n"
        "2차: 발사준비 · 재가속/강화 확인\n"
        "3차: 최종 · 0차 대비 가격 +1.0% 확인\n"
        "0/1차 실패는 무음 · 2차 이후 실패만 중단 알림\n"
        "시간 재진입 잠금 없음 · 새 사이클은 항상 0차부터 재탐색"
    )

    r.websocket_loop()
    r.STOP.set()
    thread.join(timeout=3)


if __name__ == "__main__":
    main()
