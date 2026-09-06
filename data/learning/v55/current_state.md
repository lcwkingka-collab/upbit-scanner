# V5.5 Current Learning State

Updated: 2026-09-06T22:51:08+09:00

## Source boundary

- Rules/code: `realtime_radar_v55.py` on `main`, post-`323b2f867281c9bcc09e23466e07351af2ff18c0`.
- Radar delta: `2026-09-06T21:20:04+09:00` exclusive through `2026-09-06T22:24:17+09:00`.
- Eligible universe: all Upbit KRW markets excluding KRW-BTC and stablecoins.
- Delta rows: 7,759. Stage5 16, Stage5→3 15, Stage6 1, Stage7 0, Stage8 0.
- Latest market evidence: ENS trades through 22:35:44.116 KST; ORCA candles through 22:34 KST; ZAMA trades through 22:33:46.259 KST.
- `data/latest_complete_cycle.json` remains stale at logical 18:10 KST; no current structural inference is made from it.

## Newly verified state

### ENS#1 — Stage6 false launch removed before Stage8

- T 8,255 at 21:48:54, rolling-60 ratio 2.906x.
- Stage5 8,300 at 21:52:49; 3-second rise 0.4843%, T+9 ticks, 40 trades, BID 9.86M KRW / ASK 0.
- Stage6 8,315 one second later.
- Confirmation failed: +0.5% was not retained in the confirmation window; returned to Stage3, then dropped/reset at 8,240.
- Stage6-reference outcome through 22:35:44: high 8,330 (+0.1804%), low 8,240 (-0.9020%), latest 8,290 (-0.3007%), +10% not reached.
- Stage8 entry/outcome: N/A because Stage7/8 was never reached.
- Confusion-matrix delta: one additional independent false Stage6 candidate removed by the Stage7 gate; no successful-case loss observed in this delta.

### ORCA#9 precursor — coverage-gap MISSED

- T 1,869 at 21:21:06, 25.96x; Stage2/3 passed, then drop at 21:23:59.
- Radar events are absent from 21:25:43 to 21:42:25 (1,002 seconds), spanning the first +10% candle at 21:31.
- Session open 1,760; high 2,099 (+19.2614%). Stage4–8 at the breakout are N/A, not inferred.
- Post-gap cycles were late: ORCA#5 appeared near 2,027 and rejected; ORCA#7 reached Stage5 at 1,980 then returned by -4 ticks. No Stage6 exists in the available event log.
- Classification: `coverage_gap_missed`. Preserve `market+cycle_id+t_sec` because the process restart reused cycle IDs.

### NEAR#2 — restart warm-up MISSED

- T 3,191 at 21:50:25, 4.6457x; Stage5 3,201 at 21:51:01 with net +4.737M KRW, BID 83.85%, 10 fills, then returned by -4 ticks after 3 seconds.
- It later crossed the session +10% line at 22:39 and reached 3,300 (+10.2941% from the 2,992 open) without Stage6–8.
- This is operationally contaminated, not a closed threshold false negative: the uploaded radar ends 22:24:17 and the manual V5.5 restart around 22:29 has an 11-minute warm-up covering the 22:39 breakout. Exclude it from the filter confusion matrix and retain it as restart/warm-up evidence.

### ZAMA#4 — ongoing, Stage6 and Stage8 separated

- Stage6 reference 81.8: high 85.2 (+4.1565%), low 80.4 (-1.7115%).
- Stage8 signal reference 82.7: high +3.022%, low -2.7811%.
- +10% not reached from either reference. Actual paper fill remains N/A because no paper order/fill event exists.

## Filters and candidates

- F03: 7 rejected attempts across 4 independent cycles (VIRTUAL, RAY, ZAMA, BONK); retained as short-window evidence, not added to the closed matrix yet.
- N01 blocks: 0. T02 blocks: 0. Reserve/handover: 0.
- No threshold proposal. No code change or deployment.
- Data-quality proposal only: persist a scanner `boot_id` plus explicit process start/stop/heartbeat and paper order/fill events, so coverage-gap MISSED and actual Stage8 execution can be audited without inference.

## Post-restart runtime check

- Fresh radar events resumed at 22:40:48, matching the configured 11-minute warm-up after the approximately 22:29 manual restart. Through 22:50:04: Stage1/2/3 = 216/161/161, Stage4/5 = 2/2, Stage6/7/8 = 0/0/0; final alerts 0.
- **RAY#7**: T 1,879/3.7987x at 22:43:16; Stage5 1,910 at 22:45:56 with total 186.124M, BID 124.345M/ASK 61.779M/net +62.566M, BID 66.81%, 172 fills. F02 detected weak price movement per value (0.00254% per 1M) and F03 recycled immediately.
- **IOST#1**: T 0.971/4.1085x at 22:40:54; Stage5 0.980 at 22:45:57 with BID/net +22.058M, 31 fills. No +3-tick follow-through; 5-second idle recycled it at 22:46:09.
- N01/T02/late-entry recycle were not triggered. The late-entry branch is present in the running build but has no live trigger sample yet.
- Warm-up timing works, but 216 Stage1 events in 9m16s and several implausibly large first-generation ratios show that waiting alone does not fully restore baseline quality. Keep first-generation T values and cycle IDs flagged as cold-start-contaminated until baseline persistence or a stronger readiness test is added.

## Learning posture

- Existing success reference group remains 16.
- Stage6 signal quality and Stage8 executable-entry quality must remain separate.
- Same-market clustered cycles are stored individually but deduplicated for independent confusion-matrix counts.
- Long-delay outcome windows remain open; terminal Stage7 rejection is tracked separately from later market-wide price movement.
