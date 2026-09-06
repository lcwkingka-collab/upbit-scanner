# V5.5 Current Learning State

Updated: 2026-09-06T21:38:54.390+09:00

## Latest verified window
- Radar source: `data/live/radar_events/20260906/v55_events.jsonl`, 133,806 rows through 2026-09-06 21:20:04 KST.
- Learning delta: 20:20:06–21:20:04 KST, BTC and stablecoins excluded, 9,985 events across 278 KRW markets.
- Funnel: Stage 4/5/6/7/8 = 9/8/1/1/1. Stage 5 returned to Stage 3 seven times: F03 2, 5-second idle 4, -4 ticks 1. One Stage 4 failed flow. N01 blocks 0, T02 duplicate blocks 0.
- Reset/drop/reserve/T handover = 152/97/0/0. Stage 4 rejects 54.

## New Stage 8 and separated outcomes
- **ZAMA#4** (t_sec 1788694525): Stage 6 at 21:15:48, 81.8 KRW; Stage 7 at 82.3; Stage 8 at 21:16:02, signal entry 82.7 KRW; T02 duplicate=false.
- Stage 6 reference: high 84.8 (+3.67%), low 80.4 (-1.71%), latest 81.2 (-0.73%), +10% not reached.
- Stage 8 reference: high 84.8 (+2.54%), low 80.4 (-2.78%), latest 81.2 (-1.81%), +10% not reached. Classification remains **in progress**.
- No `paper_buy_started`, `paper_buy_skipped`, or paper fill event exists after Stage 8. Therefore actual paper execution price is **N/A**; 82.7 is only the Stage 8 signal reference until execution logging is verified.

## New MISSED
- **JTO restart-generation #1** (cycle_id 1, t_sec 1788692331): T 611 KRW at 19:58:51, 3.148x; Stage 2 BID 278,497 KRW/ASK 0, one BID fill.
- It reached the session's first +10% minute at 20:31 and high 629 KRW (+10.16% from 09:00 open 571) without Stage 4–8, then dropped at 20:34:43. This is a slow-staircase MISSED.
- Cycle IDs restarted and are reused; preserve `market+cycle_id+t_sec` for this generation.

## Filter learning
- F03 rejected two CHIP#2 Stage 5 attempts, but both are dependent attempts in one cycle. Do not count them as two independent confusion-matrix samples.
- N01 and T02 received no new blocked samples.
- Prior matrix reference stays unchanged: base F03/Stage7/Stage8 fake removal 16/19; +N01 18/19; +T02 19/19; expanded N01 success retention 24/24. No closed independent delta sample was added.
- ZAMA#4 is not yet a success; it remains outside the success cohort. JTO matches the accumulated slow-staircase MISSED pattern.

## Active conclusion
- Preserve: `data/learning/v55/evidence/v55_delta_20260906_202006_212004_stage8_jto.csv`, the listed radar slices, JTO/ZAMA 1-minute candles, and ZAMA paper-execution gap.
- No immediate threshold change. Continue F03/N01/T02 validation and require paper buy/fill events before treating Stage 8 signal price as an executed paper entry.
- Code and thresholds were not modified or deployed in this learning run.
