# V5.5 Current Learning State

Updated: 2026-09-06T23:42:00+09:00

## Latest verified boundary

- Radar delta: `2026-09-06T22:50:04+09:00` exclusive through `2026-09-06T23:20:05+09:00`.
- Eligible universe: 278 Upbit KRW markets after excluding KRW-BTC and eight stablecoins.
- Delta rows: 3,155.
- Funnel: Stage4/5/6/7/8 = 2/2/1/1/1; paper-buy-started = 1.
- Stage4 rejects 28; reset/drop/reserve/T-handover = 204/71/0/0.
- F03/N01/T02 blocks = 0/0/0.
- Live-market source: BONK trades through 23:33:53.933 KST; ZAMA candles through 23:39 KST.
- `data/live/latest.json` is 22:48:54 KST and `latest_complete_cycle.json` is logical 18:10 KST, so neither is used for current Stage inference.
- Google Drive: no separate V5.5 source found.

## Newly verified outcome

### ZAMA#9 — delayed Stage6 success

- Stage6 reference: 77.8 at 18:45:15 KST.
- First +10% minute: 23:01 KST; exact crossing second N/A.
- Delay lower bound: 4h 15m 45s.
- Post-entry high/low: 87.6 (+12.5964%) / 77.6 (-0.2571%). Latest 80.5 (+3.4704%).
- Stage8 and actual paper entry: N/A because this cycle belongs to the pre-Stage8 runtime generation.
- Correlation handling: ZAMA#14 shares the same later wave and is not counted as an independent success.
- Legacy success reference group remains 16; ZAMA#9 is retained as one new delayed Stage6-success wave, not evidence of current Stage8 execution quality.

## New live Stage8

### BONK#8 — ongoing

- T 0.00496 at 23:04:27, 5.2018x.
- Stage5 0.00513: 35 trades, BID 27.01M / ASK 5.31M KRW, net +21.70M, BID share 83.57%.
- Stage6 0.00516 at 23:11:32; Stage7 0.00519; Stage8 signal 0.00522 at 23:11:44. T02 duplicate=false.
- Stage6-reference outcome through 23:33:53.933: high/low/latest = +5.2326% / -0.1938% / +2.9070%; +10% not reached.
- Stage8-signal-reference outcome: +4.0230% / -1.3410% / +1.7241%; +10% not reached.
- `paper_buy_started` confirms the async paper route accepted the signal. Actual average fill, quantity, spend, and status remain N/A because the callback only sends Telegram and the portfolio/result is not published to radar events or GitHub.

### Other open state

- ZAMA#4 remains open: Stage6 max/min +7.0905%/-2.0782%; Stage8-signal max/min +5.9250%/-3.1439%; +10% not reached from either reference; actual fill N/A.
- ZAMA#3 was at Stage5 83.8 at the 23:20:05 source cutoff. Stage6 onward is N/A until the next event upload.
- No newly verified MISSED market in the 278-market session scan.

## Learning and modification posture

- Confusion-matrix delta: Stage6 delayed success +1 (ZAMA#9); Stage8 closed samples +0 because BONK#8 and ZAMA#4 remain open.
- Strong short-window flow can pass all entry gates while remaining below +10%; keep the long outcome window open.
- The existing paper execution logging proposal is now directly evidenced by `paper_buy_started` without a persisted result. Persist `paper_buy_completed` with average price, quantity, spend, fee, status, and position ID before treating Stage8 as an executable-result dataset.
- No threshold change, code modification, or deployment was performed.
