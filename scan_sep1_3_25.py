#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from __future__ import annotations
import csv, json, time
from datetime import datetime, timedelta, timezone
from pathlib import Path
import second_scan_api as ss

KST=timezone(timedelta(hours=9)); UTC=timezone.utc
OUT=Path('sep1_3_25_output'); TARGET_DAYS={'2026-09-01','2026-09-02','2026-09-03'}; THRESHOLD=25.0

def get(path,params=None): return ss.http_json(path,params)
def kst_day(r):
    raw=r.get('candle_date_time_kst')
    return (datetime.fromisoformat(raw).replace(tzinfo=KST) if raw else datetime.fromisoformat(r['candle_date_time_utc']).replace(tzinfo=UTC).astimezone(KST)).date().isoformat()
def markets():
    rows=get('/market/all',{'is_details':'false'})
    return sorted(r['market'] for r in rows if str(r.get('market','')).startswith('KRW-') and r['market'] not in {'KRW-USDT','KRW-USDC','KRW-DAI','KRW-USDE'})
def daily_hits(m):
    rows=get('/candles/days',{'market':m,'count':8}); by={kst_day(r):r for r in rows}; out=[]
    for day in sorted(TARGET_DAYS):
        r=by.get(day); prev=(datetime.fromisoformat(day)-timedelta(days=1)).date().isoformat(); p=by.get(prev)
        if not r or not p: continue
        pc=float(p['trade_price']); hi=float(r['high_price']); cl=float(r['trade_price']); op=float(r['opening_price'])
        if pc<=0: continue
        hr=(hi/pc-1)*100
        if hr>=THRESHOLD: out.append({'market':m,'day':day,'prev_close':pc,'open':op,'high':hi,'close':cl,'open_ret_pct':(op/pc-1)*100,'high_ret_pct':hr,'close_ret_pct':(cl/pc-1)*100,'day_value_krw':float(r.get('candle_acc_trade_price') or 0)})
    return out
def minutes_for_day(m,day_s):
    start=datetime.fromisoformat(day_s).replace(tzinfo=KST); end=start+timedelta(days=1); out={}; cursor=end
    for _ in range(20):
        rows=get('/candles/minutes/1',{'market':m,'to':ss.iso_z(cursor),'count':200})
        if not rows: break
        oldest=None
        for r in rows:
            dt=datetime.fromisoformat(r['candle_date_time_utc']).replace(tzinfo=UTC); oldest=dt if oldest is None or dt<oldest else oldest
            if start.astimezone(UTC)<=dt<end.astimezone(UTC): out[int(dt.timestamp())]=r
        if oldest is None or oldest<=start.astimezone(UTC): break
        cursor=oldest; time.sleep(ss.RATE_SLEEP)
    return [out[k] for k in sorted(out)]
def find_launch_minute(mins,pc):
    vals=[]; best=None
    for r in mins:
        dt=datetime.fromisoformat(r['candle_date_time_utc']).replace(tzinfo=UTC); value=float(r.get('candle_acc_trade_price') or 0); close=float(r['trade_price']); high=float(r['high_price'])
        base=sum(vals[-10:])/len(vals[-10:]) if vals else 0; x=value/base if base>0 else None; ret=(close/pc-1)*100
        if x is not None and x>=2.5 and ret>0: return dt.astimezone(KST),{'minute_value_x':x,'minute_close_ret_pct':ret,'minute_value_krw':value}
        score=((x or 0),(high/pc-1)*100)
        if best is None or score>best[0]: best=(score,dt.astimezone(KST),{'minute_value_x':x,'minute_close_ret_pct':ret,'minute_value_krw':value})
        vals.append(value)
    return (best[1],best[2]) if best else (None,{})
def main():
    OUT.mkdir(exist_ok=True); hits=[]; ms=markets(); print('markets',len(ms),flush=True)
    for i,m in enumerate(ms,1):
        try: hits.extend(daily_hits(m))
        except Exception as e: print('daily error',m,e,flush=True)
        if i%25==0: print('daily',i,'/',len(ms),'hits',len(hits),flush=True)
        time.sleep(.05)
    hits.sort(key=lambda x:(x['day'],-x['high_ret_pct'])); results=[]; rowsout=[]
    for idx,h in enumerate(hits,1):
        try:
            mins=minutes_for_day(h['market'],h['day']); launch,meta=find_launch_minute(mins,h['prev_close'])
            if launch is None: raise RuntimeError('no launch minute')
            start=launch-timedelta(minutes=10); end=launch+timedelta(minutes=10)
            scan=ss.analyze_market(h['market'],start,end,enrich_trades=True)
            le=int(launch.timestamp()); tick=scan.get('tick_size'); raw=scan.get('rows') or []
            # Preserve real elapsed seconds. Missing-trade seconds remain absent rather than being shifted.
            for r in raw:
                rr=dict(r); rr.update({'surge_day':h['day'],'day_high_ret_pct':h['high_ret_pct'],'launch_T_kst':launch.isoformat(),'t_from_launch_sec':int(r['epoch_sec']-le)}); rowsout.append(rr)
            results.append({'hit':h,'launch_T_kst':launch.isoformat(),'launch_meta':meta,'scan_from_kst':start.isoformat(),'scan_to_kst':end.isoformat(),'tick_size':tick,'seconds_with_trades':len(raw)})
            print(f'[{idx}/{len(hits)}] {h["day"]} {h["market"]} T={launch.isoformat()} seconds={len(raw)}',flush=True)
        except Exception as e: results.append({'hit':h,'error':str(e)}); print('error',h['market'],e,flush=True)
        time.sleep(.2)
    payload={'generated_at_kst':datetime.now(KST).isoformat(),'range':'T-10m..T+10m','threshold_pct':THRESHOLD,'hits':len(hits),'results':results}
    (OUT/'sep1_3_25_Tminus10_Tplus10.json').write_text(json.dumps(payload,ensure_ascii=False,indent=2),encoding='utf-8')
    if hits:
        with (OUT/'sep1_3_25_hits.csv').open('w',newline='',encoding='utf-8-sig') as f: w=csv.DictWriter(f,fieldnames=list(hits[0])); w.writeheader(); w.writerows(hits)
    if rowsout:
        fields=[]; seen=set()
        for r in rowsout:
            for k in r:
                if k not in seen: seen.add(k); fields.append(k)
        with (OUT/'sep1_3_25_Tminus10_Tplus10_seconds.csv').open('w',newline='',encoding='utf-8-sig') as f: w=csv.DictWriter(f,fieldnames=fields,extrasaction='ignore'); w.writeheader(); w.writerows(rowsout)
    summary=[]
    for x in results:
        h=x['hit']; summary.append({'day':h['day'],'market':h['market'],'high_ret_pct':h['high_ret_pct'],'close_ret_pct':h['close_ret_pct'],'prev_close':h['prev_close'],'launch_T_kst':x.get('launch_T_kst'),'scan_from_kst':x.get('scan_from_kst'),'scan_to_kst':x.get('scan_to_kst'),'tick_size':x.get('tick_size'),'seconds_with_trades':x.get('seconds_with_trades'),'error':x.get('error')})
    if summary:
        with (OUT/'sep1_3_25_Tminus10_Tplus10_summary.csv').open('w',newline='',encoding='utf-8-sig') as f: w=csv.DictWriter(f,fieldnames=list(summary[0])); w.writeheader(); w.writerows(summary)
    print(json.dumps({'hits':len(hits),'range':'T-10m..T+10m','out':str(OUT)},ensure_ascii=False),flush=True)
if __name__=='__main__': main()
