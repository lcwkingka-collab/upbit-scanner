#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from __future__ import annotations
import csv, json, time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List
import second_scan_api as ss

KST=timezone(timedelta(hours=9)); UTC=timezone.utc
OUT=Path('sep1_3_25_output'); TARGET_DAYS={'2026-09-01','2026-09-02','2026-09-03'}
THRESHOLD=25.0

def get(path, params=None): return ss.http_json(path, params)

def kst_day(row):
    raw=row.get('candle_date_time_kst')
    if raw: return datetime.fromisoformat(raw).replace(tzinfo=KST).date().isoformat()
    return datetime.fromisoformat(row['candle_date_time_utc']).replace(tzinfo=UTC).astimezone(KST).date().isoformat()

def markets():
    rows=get('/market/all', {'is_details':'false'})
    return sorted(r['market'] for r in rows if str(r.get('market','')).startswith('KRW-') and r['market'] not in {'KRW-USDT','KRW-USDC','KRW-DAI','KRW-USDE'})

def daily_hits(market):
    rows=get('/candles/days', {'market':market,'count':8})
    by={kst_day(r):r for r in rows}
    out=[]
    for day in sorted(TARGET_DAYS):
        r=by.get(day)
        prev=(datetime.fromisoformat(day)-timedelta(days=1)).date().isoformat()
        p=by.get(prev)
        if not r or not p: continue
        prev_close=float(p['trade_price']); high=float(r['high_price']); close=float(r['trade_price']); open_=float(r['opening_price'])
        if prev_close<=0: continue
        high_ret=(high/prev_close-1)*100; close_ret=(close/prev_close-1)*100; open_ret=(open_/prev_close-1)*100
        if high_ret>=THRESHOLD:
            out.append({'market':market,'day':day,'prev_close':prev_close,'open':open_,'high':high,'close':close,'open_ret_pct':open_ret,'high_ret_pct':high_ret,'close_ret_pct':close_ret,'day_value_krw':float(r.get('candle_acc_trade_price') or 0)})
    return out

def minutes_for_day(market, day_s):
    start=datetime.fromisoformat(day_s).replace(tzinfo=KST); end=start+timedelta(days=1)
    out={}; cursor=end; guard=0
    while cursor>start and guard<20:
        guard+=1
        rows=get('/candles/minutes/1', {'market':market,'to':ss.iso_z(cursor),'count':200})
        if not rows: break
        oldest=None
        for r in rows:
            dt=datetime.fromisoformat(r['candle_date_time_utc']).replace(tzinfo=UTC)
            oldest=dt if oldest is None or dt<oldest else oldest
            if start.astimezone(UTC)<=dt<end.astimezone(UTC): out[int(dt.timestamp())]=r
        if oldest is None or oldest<=start.astimezone(UTC): break
        cursor=oldest; time.sleep(ss.RATE_SLEEP)
    return [out[k] for k in sorted(out)]

def find_launch_minute(mins, prev_close):
    vals=[]; best=None
    for i,r in enumerate(mins):
        dt=datetime.fromisoformat(r['candle_date_time_utc']).replace(tzinfo=UTC)
        value=float(r.get('candle_acc_trade_price') or 0); close=float(r['trade_price']); high=float(r['high_price'])
        base=sum(vals[-10:])/len(vals[-10:]) if vals else 0
        x=value/base if base>0 else None
        ret=(close/prev_close-1)*100 if prev_close else None
        if x is not None and x>=2.5 and ret is not None and ret>0:
            return dt.astimezone(KST), {'minute_value_x':x,'minute_close_ret_pct':ret,'minute_value_krw':value}
        score=((x or 0), (high/prev_close-1)*100 if prev_close else 0)
        if best is None or score>best[0]: best=(score,dt.astimezone(KST),{'minute_value_x':x,'minute_close_ret_pct':ret,'minute_value_krw':value})
        vals.append(value)
    return (best[1],best[2]) if best else (None,{})

def exact_t0_summary(scan):
    rows=scan.get('rows') or []; tick=scan.get('tick_size')
    t0i=next((i for i,r in enumerate(rows) if (r.get('value_vs_prev10_active_sec_x') or 0)>=2.5),None)
    if t0i is None: return None
    base=rows[t0i]; t0e=base['epoch_sec']; p0=base['close']; by={r['epoch_sec']:r for r in rows}
    offs={}
    for off in (0,1,2,3,5,10):
        r=by.get(t0e+off)
        offs[str(off)]=None if r is None else {'timestamp_kst':r['timestamp_kst'],'close':r['close'],'ticks_from_t0':((r['close']-p0)/tick if tick else None),'pct_from_t0':((r['close']/p0-1)*100 if p0 else None),'value_1s_krw':r['value_1s_krw'],'value_x':r['value_vs_prev10_active_sec_x'],'bid_ratio':r['bid_ratio'],'net_buy_krw':r['net_buy_krw'],'trade_count':r['trade_count']}
    return {'timestamp_kst':base['timestamp_kst'],'price':p0,'tick_size':tick,'tick_pct':(tick/p0*100 if tick and p0 else None),'value_x':base['value_vs_prev10_active_sec_x'],'offsets':offs}

def main():
    OUT.mkdir(exist_ok=True)
    hits=[]
    ms=markets()
    print('markets',len(ms),flush=True)
    for i,m in enumerate(ms,1):
        try: hits.extend(daily_hits(m))
        except Exception as e: print('daily error',m,e,flush=True)
        if i%25==0: print('daily',i,'/',len(ms),'hits',len(hits),flush=True)
        time.sleep(0.05)
    hits.sort(key=lambda x:(x['day'],-x['high_ret_pct']))
    results=[]; second_rows=[]
    for idx,h in enumerate(hits,1):
        m=h['market']; day=h['day']; print(f'[{idx}/{len(hits)}] {day} {m} high={h["high_ret_pct"]:.2f}%',flush=True)
        try:
            mins=minutes_for_day(m,day)
            launch,meta=find_launch_minute(mins,h['prev_close'])
            if launch is None: raise RuntimeError('no minute launch window')
            start=launch-timedelta(minutes=2); end=launch+timedelta(minutes=3)
            scan=ss.analyze_market(m,start,end,enrich_trades=True)
            scan['t0_exact']=exact_t0_summary(scan)
            results.append({'hit':h,'launch_minute_kst':launch.isoformat(),'launch_meta':meta,'scan':scan})
            for r in scan.get('rows',[]):
                rr=dict(r); rr.update({'surge_day':day,'day_high_ret_pct':h['high_ret_pct'],'launch_minute_kst':launch.isoformat()}); second_rows.append(rr)
        except Exception as e:
            results.append({'hit':h,'error':str(e)})
            print(' error',e,flush=True)
        time.sleep(0.2)
    (OUT/'sep1_3_25_results.json').write_text(json.dumps({'generated_at_kst':datetime.now(KST).isoformat(),'threshold_pct':THRESHOLD,'hits':len(hits),'results':results},ensure_ascii=False,indent=2),encoding='utf-8')
    if hits:
        with (OUT/'sep1_3_25_hits.csv').open('w',newline='',encoding='utf-8-sig') as f:
            w=csv.DictWriter(f,fieldnames=list(hits[0].keys())); w.writeheader(); w.writerows(hits)
    if second_rows:
        fields=[]; seen=set()
        for r in second_rows:
            for k in r:
                if k not in seen: seen.add(k); fields.append(k)
        with (OUT/'sep1_3_25_seconds.csv').open('w',newline='',encoding='utf-8-sig') as f:
            w=csv.DictWriter(f,fieldnames=fields,extrasaction='ignore'); w.writeheader(); w.writerows(second_rows)
    summary=[]
    for x in results:
        h=x['hit']; s=x.get('scan') or {}; t=s.get('t0_exact') or {}
        summary.append({'day':h['day'],'market':h['market'],'high_ret_pct':h['high_ret_pct'],'close_ret_pct':h['close_ret_pct'],'prev_close':h['prev_close'],'launch_minute_kst':x.get('launch_minute_kst'),'tick_size':s.get('tick_size'),'t0_timestamp_kst':t.get('timestamp_kst'),'t0_price':t.get('price'),'t0_tick_pct':t.get('tick_pct'),'t0_value_x':t.get('value_x'),'t0_offsets_json':json.dumps(t.get('offsets',{}),ensure_ascii=False),'error':x.get('error')})
    if summary:
        with (OUT/'sep1_3_25_summary.csv').open('w',newline='',encoding='utf-8-sig') as f:
            w=csv.DictWriter(f,fieldnames=list(summary[0].keys())); w.writeheader(); w.writerows(summary)
    print(json.dumps({'hits':len(hits),'markets':sorted({h['market'] for h in hits}),'out':str(OUT)},ensure_ascii=False),flush=True)

if __name__=='__main__': main()
