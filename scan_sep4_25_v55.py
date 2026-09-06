#!/usr/bin/env python3
"""Independently find 2026-09-04 Upbit KRW +25% daily winners and replay V5.5.

The Upbit daily candle is UTC based (09:00 KST to next 09:00 KST).  T is
searched from the first effective rolling-60-second 2.8x event before the
first material launch; it is never moved to a later already-pumped segment.
"""
from __future__ import annotations

import collections
import csv
import json
import statistics
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import second_scan_api as ss

KST = timezone(timedelta(hours=9))
UTC = timezone.utc
DAY = "2026-09-04"
OUT = Path("sep4_25_v55_output")
ENTRY_X = 2.8
EXCLUDED = {"KRW-BTC"}
STABLE = {"USDT", "USDC", "USDG", "EURC"}


def write_csv(path: Path, rows: list[dict]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fields, seen = [], set()
    for row in rows:
        for key in row:
            if key not in seen:
                seen.add(key); fields.append(key)
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader(); w.writerows(rows)


def get(path: str, params=None):
    return ss.http_json(path, params)


def fetch_minutes(market: str, start: datetime, end: datetime) -> list[dict]:
    out, cursor = {}, end
    for _ in range(12):
        rows = get("/candles/minutes/1", {"market": market, "to": ss.iso_z(cursor), "count": 200})
        if not rows: break
        oldest = None
        for row in rows:
            dt = datetime.fromisoformat(row["candle_date_time_utc"]).replace(tzinfo=UTC)
            oldest = dt if oldest is None or dt < oldest else oldest
            if start <= dt < end: out[int(dt.timestamp())] = row
        if oldest is None or oldest <= start: break
        cursor = oldest; time.sleep(ss.RATE_SLEEP)
    return [out[k] for k in sorted(out)]


def daily_universe() -> list[dict]:
    markets = [x for x in get("/market/all", {"is_details": "false"}) if str(x.get("market", "")).startswith("KRW-")]
    target_utc = datetime(2026, 9, 4, tzinfo=UTC)
    rows = []
    for i, meta in enumerate(markets, 1):
        market = meta["market"]
        try:
            cs = get("/candles/days", {"market": market, "to": "2026-09-05T00:00:01Z", "count": 3})
            candle = next((c for c in cs if c.get("candle_date_time_utc", "").startswith("2026-09-04")), None)
            if not candle: continue
            op, hi, lo, close = map(float, (candle["opening_price"], candle["high_price"], candle["low_price"], candle["trade_price"]))
            rows.append({
                "day": DAY, "market": market, "korean_name": meta.get("korean_name"),
                "open": op, "high": hi, "low": lo, "close": close,
                "high_gain_pct": (hi / op - 1) * 100 if op else None,
                "close_gain_pct": (close / op - 1) * 100 if op else None,
                "candle_start_kst": "2026-09-04T09:00:00+09:00",
                "candle_end_kst": "2026-09-05T09:00:00+09:00",
            })
        except Exception as e:
            rows.append({"day": DAY, "market": market, "error": repr(e)})
        if i % 25 == 0: print(f"daily {i}/{len(markets)}", flush=True)
        time.sleep(ss.RATE_SLEEP)
    return rows


def minute_value_x(values: list[float], i: int) -> float:
    if i < 10: return 0.0
    prev = values[i-10:i]
    mean = statistics.fmean(prev)
    nz = [x for x in prev if x > 0]
    floor = statistics.median(nz) * .35 if nz else 0.0
    return values[i] / max(mean, floor, 1.0)


def find_prelaunch_t(minutes: list[dict], daily_open: float, daily_high: float) -> dict | None:
    if not minutes: return None
    vals = [float(r.get("candle_acc_trade_price") or 0) for r in minutes]
    # First point from which a material first leg (+5% from daily open) develops.
    launch_i = next((i for i, r in enumerate(minutes) if float(r["high_price"]) >= daily_open * 1.05), len(minutes)-1)
    candidates = []
    for i, row in enumerate(minutes):
        vx = minute_value_x(vals, i)
        if vx < ENTRY_X: continue
        # Keep a T only if meaningful forward expansion follows within 60 minutes.
        close = float(row["trade_price"])
        fhi = max(float(x["high_price"]) for x in minutes[i:min(len(minutes), i+61)])
        if fhi >= close * 1.03:
            candidates.append((i, vx, fhi))
    before = [x for x in candidates if x[0] <= launch_i]
    picked = before[0] if before else (candidates[0] if candidates else None)
    if not picked: return None
    i, vx, fhi = picked; row = minutes[i]
    dt = datetime.fromisoformat(row["candle_date_time_utc"]).replace(tzinfo=UTC)
    return {
        "minute_index": i, "approx_t_utc": dt.isoformat(), "approx_t_kst": dt.astimezone(KST).isoformat(),
        "approx_t_price": float(row["opening_price"]), "minute_value_x": vx,
        "first_5pct_minute_kst": datetime.fromisoformat(minutes[launch_i]["candle_date_time_utc"]).replace(tzinfo=UTC).astimezone(KST).isoformat(),
        "forward_60m_high": fhi,
    }


def fetch_replay_window(market: str, start: datetime, end: datetime) -> tuple[dict[int,dict], dict[int,list[tuple]]]:
    candles, trades = {}, collections.defaultdict(list)
    cur = start
    while cur < end:
        ce = min(cur + timedelta(minutes=20), end)
        result = ss.analyze_market(market, cur, ce, enrich_trades=True)
        for r in result.get("rows") or []:
            sec = int(r["epoch_sec"]); candles[sec] = r
            # aggregate rows cannot reconstruct intrasecond order, but OHLC covers barriers.
            if r.get("bid_value_krw") is not None or r.get("ask_value_krw") is not None:
                trades[sec].append((float(r.get("close") or 0), float(r.get("bid_value_krw") or 0), float(r.get("ask_value_krw") or 0), int(r.get("bid_count") or 0), int(r.get("ask_count") or 0)))
        print(f"  {market} seconds {cur.astimezone(KST).strftime('%H:%M')}..{ce.astimezone(KST).strftime('%H:%M')}", flush=True)
        cur = ce; time.sleep(.15)
    return candles, trades


def replay_v55(market: str, tick: float, candles: dict[int,dict], trades: dict[int,list[tuple]], approx_t: datetime) -> dict:
    secs = range(min(candles), max(candles)+1)
    price = None; stage = 0; t_sec = 0; t_price = None; t_x = 0.0
    stage5_sec = 0; stage5_price = None; last_trade_sec = 0
    launch_armed = True; drop_since = 0; cycles = 0; events = []
    sec_values = {s: float(candles[s].get("value_1s_krw") or 0) for s in candles}
    # Prefix sum makes exact rolling 60s and ten prior 60s inexpensive.
    lo, hi = min(candles)-700, max(candles)+1
    prefix, total = {}, 0.0
    for s in range(lo, hi+1): total += sec_values.get(s, 0.0); prefix[s] = total
    def sumr(a,b): return prefix.get(b,0.0)-prefix.get(a-1,0.0)
    def vx_at(s):
        cur = sumr(s-59,s); vals=[sumr(s-60*i-59,s-60*i) for i in range(1,11)]
        mean=statistics.fmean(vals); nz=[x for x in vals if x>0]; floor=statistics.median(nz)*.35 if nz else 0.0
        return cur/max(mean,floor,1.0),cur
    def flow(s,a,b):
        bid=ask=0.0; bc=ac=0
        for z in range(a,b+1):
            for _p,x,y,n,m in trades.get(z,[]): bid+=x;ask+=y;bc+=n;ac+=m
        return bid,ask,bc,ac
    first_allowed = int((approx_t - timedelta(minutes=2)).timestamp())
    for s in secs:
        c=candles.get(s)
        if c: price=float(c["close"])
        if price is None or s < first_allowed: continue
        vx, curvalue=vx_at(s); bid,ask,bc,ac=flow(s,s-9,s); pbid,pask,pbc,pac=flow(s,s-19,s-10)
        if stage==0:
            if vx>=ENTRY_X:
                cycles+=1; stage=1;t_sec=s;t_price=price;t_x=vx
                events.append({"event":"stage1","sec":s,"price":price,"value_x":vx,"value_60s":curvalue})
            continue
        if stage==1:
            if (price-t_price)/tick<=-2 or vx<ENTRY_X:
                events.append({"event":"reset","sec":s,"reason":"pre-stage2"});stage=0;continue
            if bid>ask and bid-ask>0 and bid>pbid:
                stage=3;events.append({"event":"stage2_3","sec":s,"price":price,"bid":bid,"ask":ask,"bid_count":bc,"prev_bid":pbid})
            continue
        if stage==5:
            if s-t_sec>=12*3600: events.append({"event":"reset","sec":s,"reason":"stage5_12h"});stage=0;continue
            if not c:
                if s-last_trade_sec>5: events.append({"event":"stage5_to_3","sec":s,"reason":"idle"});stage=3;stage5_price=None;launch_armed=False
                continue
            last_trade_sec=s; high=float(c["high"]);low=float(c["low"])
            up=high>=stage5_price+3*tick-1e-12;down=low<=stage5_price-4*tick+1e-12
            if up and down: events.append({"event":"stage5_to_3","sec":s,"reason":"both"});stage=3;stage5_price=None;launch_armed=False
            elif up:
                final=stage5_price+3*tick;events.append({"event":"stage6","sec":s,"price":final,"elapsed":s-stage5_sec});stage=6;break
            elif down: events.append({"event":"stage5_to_3","sec":s,"reason":"-4tick"});stage=3;stage5_price=None;launch_armed=False
            continue
        # Drop engine.
        bad=0
        if (price-t_price)/tick<=-2: bad+=1
        if ask>bid and bid-ask<0: bad+=1
        if bid<pbid and bc<pbc: bad+=1
        if bad>=2:
            drop_since=drop_since or s
            if s-drop_since+1>=20: events.append({"event":"drop","sec":s});stage=0;drop_since=0;continue
        else: drop_since=0
        pts=[candles[z] for z in range(s-2,s+1) if z in candles]
        if not pts: launch_armed=True;continue
        start=float(pts[0]["open"]); high=max(float(x["high"]) for x in pts); last=float(pts[-1]["close"])
        ht=(high-start)/tick
        if ht<4-1e-9: launch_armed=True;continue
        if not launch_armed: continue
        launch_armed=False; rise=(last/start-1)*100; tt=(last-t_price)/tick; trades10=bc+ac
        if rise<.40-1e-12 or trades10<7 or tt<8-1e-9:
            events.append({"event":"stage4_reject","sec":s,"rise_pct":rise,"high_ticks":ht,"t_ticks":tt,"trades10":trades10});continue
        events.append({"event":"stage4","sec":s,"price":last,"rise_pct":rise,"high_ticks":ht,"last_ticks":(last-start)/tick,"t_ticks":tt,"trades10":trades10})
        if bid>ask and bid-ask>0:
            stage=5;stage5_sec=s;stage5_price=last;last_trade_sec=s
            events.append({"event":"stage5","sec":s,"price":last,"bid":bid,"ask":ask,"bid_share":bid/(bid+ask) if bid+ask else None,"notional10":bid+ask})
        else: stage=3;events.append({"event":"stage4_to_3","sec":s})
    s1=next((x for x in events if x["event"]=="stage1"),None); s6=next((x for x in events if x["event"]=="stage6"),None)
    def stamp(x): return datetime.fromtimestamp(x,KST).isoformat() if x else None
    return {
        "market":market,"tick_size":tick,"pass_v55":bool(s6),"final_stage":stage,"cycles":cycles,
        "T_kst":stamp(s1["sec"]) if s1 else None,"T_price":s1.get("price") if s1 else None,"T_value_x":s1.get("value_x") if s1 else None,
        "stage6_kst":stamp(s6["sec"]) if s6 else None,"stage6_price":s6.get("price") if s6 else None,
        "t_to_stage6_sec":s6["sec"]-s1["sec"] if s1 and s6 else None,
        "stage5_return_count":sum(x["event"]=="stage5_to_3" for x in events),
        "event_count":len(events),"events_json":json.dumps([{**e,"kst":stamp(e["sec"])} for e in events],ensure_ascii=False,separators=(",",":")),
    }


def main():
    OUT.mkdir(exist_ok=True)
    universe=daily_universe();write_csv(OUT/"SEP4_ALL_KRW_DAILY.csv",universe)
    winners=[r for r in universe if r.get("high_gain_pct",-999)>=25 and r.get("market") not in EXCLUDED and r.get("market","").split("-")[-1] not in STABLE]
    write_csv(OUT/"SEP4_25_SUCCESS_LIST.csv",winners)
    print("WINNERS",json.dumps([(x["market"],round(x["high_gain_pct"],3)) for x in winners],ensure_ascii=False),flush=True)
    start=datetime(2026,9,4,tzinfo=UTC);end=start+timedelta(days=1)
    minute_all=[]; trows=[]; replay=[]
    for n,w in enumerate(winners,1):
        market=w["market"]; mins=fetch_minutes(market,start-timedelta(minutes=11),end)
        for r in mins:
            dt=datetime.fromisoformat(r["candle_date_time_utc"]).replace(tzinfo=UTC)
            minute_all.append({"day":DAY,"market":market,"timestamp_kst":dt.astimezone(KST).isoformat(),"opening_price":r["opening_price"],"high_price":r["high_price"],"low_price":r["low_price"],"trade_price":r["trade_price"],"value_1m_krw":r["candle_acc_trade_price"],"volume_1m":r["candle_acc_trade_volume"]})
        in_day=[r for r in mins if start<=datetime.fromisoformat(r["candle_date_time_utc"]).replace(tzinfo=UTC)<end]
        tc=find_prelaunch_t(in_day,float(w["open"]),float(w["high"]))
        if not tc:
            replay.append({"market":market,"pass_v55":False,"reason":"no_prelaunch_2.8x_T"});continue
        trows.append({"market":market,**tc})
        approx=datetime.fromisoformat(tc["approx_t_utc"])
        highrow=max(in_day,key=lambda x:float(x["high_price"])); highdt=datetime.fromisoformat(highrow["candle_date_time_utc"]).replace(tzinfo=UTC)
        scan_start=max(start,approx-timedelta(minutes=11));scan_end=min(end,max(approx+timedelta(minutes=30),min(highdt+timedelta(minutes=2),approx+timedelta(hours=4))))
        tick=ss.fetch_tick_size(market)
        candles,trades=fetch_replay_window(market,scan_start,scan_end)
        if not tick or not candles: replay.append({"market":market,"pass_v55":False,"reason":"missing_tick_or_seconds"});continue
        rr=replay_v55(market,tick,candles,trades,approx);rr.update({"daily_high_gain_pct":w["high_gain_pct"],"daily_open":w["open"],"daily_high":w["high"],"approx_t_kst":tc["approx_t_kst"],"scan_start_kst":scan_start.astimezone(KST).isoformat(),"scan_end_kst":scan_end.astimezone(KST).isoformat()});replay.append(rr)
        print(f"[{n}/{len(winners)}] {market} pass={rr['pass_v55']} T={rr.get('T_kst')} s6={rr.get('stage6_kst')}",flush=True)
    write_csv(OUT/"SEP4_25_FULL_DAY_1MIN.csv",minute_all);write_csv(OUT/"SEP4_T_CANDIDATES.csv",trows);write_csv(OUT/"SEP4_V55_REPLAY.csv",replay)
    (OUT/"manifest.json").write_text(json.dumps({"generated_at":datetime.now(KST).isoformat(),"method_day":"Upbit UTC daily candle / 09:00 KST boundary","universe":len(universe),"winners":len(winners),"replay_pass":sum(bool(x.get('pass_v55')) for x in replay),"replay":replay},ensure_ascii=False,indent=2),encoding="utf-8")


if __name__=="__main__": main()
