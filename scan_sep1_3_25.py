#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from __future__ import annotations
import csv,json,time
from datetime import datetime,timedelta,timezone
from pathlib import Path
import second_scan_api as ss
KST=timezone(timedelta(hours=9)); UTC=timezone.utc; OUT=Path('sep1_3_25_output'); DAYS={'2026-09-01','2026-09-02','2026-09-03'}; TH=25.0

def get(p,q=None): return ss.http_json(p,q)
def kd(r):
 raw=r.get('candle_date_time_kst'); return (datetime.fromisoformat(raw).replace(tzinfo=KST) if raw else datetime.fromisoformat(r['candle_date_time_utc']).replace(tzinfo=UTC).astimezone(KST)).date().isoformat()
def markets(): return sorted(r['market'] for r in get('/market/all',{'is_details':'false'}) if str(r.get('market','')).startswith('KRW-') and r['market'] not in {'KRW-USDT','KRW-USDC','KRW-DAI','KRW-USDE'})
def hits(m):
 rows=get('/candles/days',{'market':m,'count':8}); by={kd(r):r for r in rows}; z=[]
 for d in sorted(DAYS):
  r=by.get(d); p=by.get((datetime.fromisoformat(d)-timedelta(days=1)).date().isoformat())
  if not r or not p: continue
  pc=float(p['trade_price']); hi=float(r['high_price']); op=float(r['opening_price']); cl=float(r['trade_price']); hr=(hi/pc-1)*100 if pc else 0
  if hr>=TH: z.append({'market':m,'day':d,'prev_close':pc,'open':op,'high':hi,'close':cl,'high_ret_pct':hr,'close_ret_pct':(cl/pc-1)*100})
 return z
def mins(m,d):
 st=datetime.fromisoformat(d).replace(tzinfo=KST); en=st+timedelta(days=1); out={}; cur=en
 for _ in range(20):
  a=get('/candles/minutes/1',{'market':m,'to':ss.iso_z(cur),'count':200})
  if not a: break
  old=None
  for r in a:
   x=datetime.fromisoformat(r['candle_date_time_utc']).replace(tzinfo=UTC); old=x if old is None or x<old else old
   if st.astimezone(UTC)<=x<en.astimezone(UTC): out[int(x.timestamp())]=r
  if old is None or old<=st.astimezone(UTC): break
  cur=old; time.sleep(ss.RATE_SLEEP)
 return [out[k] for k in sorted(out)]
def trade_metrics(m,st,en):
 try: tr=ss.fetch_raw_trades(m,st,en)
 except Exception: tr=[]
 bv=av=0.0; n=0
 for r in tr:
  p=float(r.get('trade_price') or 0); v=float(r.get('trade_volume') or 0); x=p*v; n+=1
  if r.get('ask_bid')=='BID': bv+=x
  elif r.get('ask_bid')=='ASK': av+=x
 tot=bv+av
 return {'bid_value_krw':bv,'ask_value_krw':av,'bid_ratio':(bv/tot if tot else None),'net_buy_krw':bv-av,'trade_count':n}
def first_price_launch(a,m):
 # T = the minute immediately before the first *meaningful* price launch of the day.
 # Search chronologically. A candidate must show a transition from quiet/local balance into
 # fast upside expansion, with at least one confirming flow signal (value or buy-side trades).
 pts=[]
 for r in a:
  dt=datetime.fromisoformat(r['candle_date_time_utc']).replace(tzinfo=UTC).astimezone(KST)
  pts.append({'dt':dt,'o':float(r['opening_price']),'h':float(r['high_price']),'l':float(r['low_price']),'c':float(r['trade_price']),'v':float(r.get('candle_acc_trade_price') or 0)})
 if len(pts)<20: raise RuntimeError('not enough minute data')
 vals=[]
 for i,p in enumerate(pts):
  vals.append(p['v'])
  if i<10 or p['o']<=0: continue
  pre=pts[max(0,i-5):i]
  if not pre: continue
  pre_low=min(x['l'] for x in pre); pre_high=max(x['h'] for x in pre)
  pre_range=(pre_high/pre_low-1)*100 if pre_low>0 else 999
  base_v=sum(x['v'] for x in pts[max(0,i-10):i])/max(1,len(pts[max(0,i-10):i]))
  value_x=p['v']/base_v if base_v>0 else 0
  # forward local price expansion from this minute's open
  fwd2=pts[i:min(len(pts),i+2)]; fwd5=pts[i:min(len(pts),i+5)]; fwd10=pts[i:min(len(pts),i+10)]
  ret2=(max(x['h'] for x in fwd2)/p['o']-1)*100 if fwd2 else 0
  ret5=(max(x['h'] for x in fwd5)/p['o']-1)*100 if fwd5 else 0
  ret10=(max(x['h'] for x in fwd10)/p['o']-1)*100 if fwd10 else 0
  cur_range=(p['h']/p['l']-1)*100 if p['l']>0 else 0
  # Require the first real local acceleration, not a later strongest leg.
  price_trigger=(ret2>=1.0 or ret5>=2.0 or ret10>=3.0 or cur_range>=1.0)
  flow_trigger=(value_x>=1.8)
  if not price_trigger and not flow_trigger: continue
  # Enrich only shortlisted chronological candidates with actual BID/ASK flow.
  st=p['dt']-timedelta(seconds=30); en=p['dt']+timedelta(minutes=2)
  tm=trade_metrics(m,st,en)
  buy_trigger=((tm.get('bid_ratio') or 0)>=0.55 and (tm.get('net_buy_krw') or 0)>0) or (tm.get('trade_count') or 0)>=10
  if not (flow_trigger or buy_trigger): continue
  # Avoid calling already-expanded continuation legs 'T' when the local 5m structure was already vertical.
  # We still allow volatile coins, but prefer a genuine transition from a relatively quieter base.
  if pre_range>8.0 and ret2<2.0 and ret5<4.0: continue
  T=p['dt']
  meta={'pre5_range_pct':pre_range,'minute_value_x':value_x,'ret2_pct':ret2,'ret5_pct':ret5,'ret10_pct':ret10,'minute_range_pct':cur_range,**tm,'anchor_price':p['o']}
  return T,meta
 raise RuntimeError('no first valid price launch')
def main():
 OUT.mkdir(exist_ok=True); hs=[]; ms=markets()
 for i,m in enumerate(ms,1):
  try: hs.extend(hits(m))
  except Exception as e: print('daily error',m,e)
  if i%25==0: print('daily',i,len(ms),'hits',len(hs),flush=True)
  time.sleep(.05)
 hs.sort(key=lambda x:(x['day'],-x['high_ret_pct'])); res=[]; sec=[]
 for j,h in enumerate(hs,1):
  try:
   a=mins(h['market'],h['day']); T,meta=first_price_launch(a,h['market']); st=T-timedelta(minutes=10); en=T+timedelta(minutes=10)
   s=ss.analyze_market(h['market'],st,en,enrich_trades=True); te=int(T.timestamp())
   for r in s.get('rows',[]):
    q=dict(r); q.update({'surge_day':h['day'],'day_high_ret_pct':h['high_ret_pct'],'first_launch_T_kst':T.isoformat(),'t_from_first_launch_sec':int(r['epoch_sec']-te)}); sec.append(q)
   res.append({'hit':h,'first_launch_T_kst':T.isoformat(),'first_launch_meta':meta,'scan_from_kst':st.isoformat(),'scan_to_kst':en.isoformat(),'tick_size':s.get('tick_size'),'seconds_with_trades':len(s.get('rows',[]))})
   print(f'[{j}/{len(hs)}] {h["market"]} {h["day"]} T={T.isoformat()} ret2={meta["ret2_pct"]:.2f}% ret5={meta["ret5_pct"]:.2f}% ret10={meta["ret10_pct"]:.2f}% vx={meta["minute_value_x"]:.2f} bid={meta.get("bid_ratio")}',flush=True)
  except Exception as e: res.append({'hit':h,'error':str(e)}); print('ERR',h['market'],e,flush=True)
  time.sleep(.2)
 (OUT/'first_launch_Tminus10_Tplus10.json').write_text(json.dumps({'generated_at_kst':datetime.now(KST).isoformat(),'anchor':'first valid price launch before breakout','hits':len(hs),'results':res},ensure_ascii=False,indent=2),encoding='utf-8')
 if hs:
  with (OUT/'first_launch_hits.csv').open('w',newline='',encoding='utf-8-sig') as f: w=csv.DictWriter(f,fieldnames=list(hs[0]));w.writeheader();w.writerows(hs)
 if sec:
  fs=[]; seen=set()
  for r in sec:
   for k in r:
    if k not in seen: seen.add(k);fs.append(k)
  with (OUT/'first_launch_Tminus10_Tplus10_seconds.csv').open('w',newline='',encoding='utf-8-sig') as f: w=csv.DictWriter(f,fieldnames=fs,extrasaction='ignore');w.writeheader();w.writerows(sec)
 summ=[]
 for x in res:
  h=x['hit']; m=x.get('first_launch_meta') or {}; summ.append({'day':h['day'],'market':h['market'],'prev_close':h['prev_close'],'day_high':h['high'],'day_high_ret_pct':h['high_ret_pct'],'first_launch_T_kst':x.get('first_launch_T_kst'),'launch_anchor_price':m.get('anchor_price'),'pre5_range_pct':m.get('pre5_range_pct'),'minute_value_x':m.get('minute_value_x'),'ret2_pct':m.get('ret2_pct'),'ret5_pct':m.get('ret5_pct'),'ret10_pct':m.get('ret10_pct'),'bid_ratio':m.get('bid_ratio'),'net_buy_krw':m.get('net_buy_krw'),'trade_count':m.get('trade_count'),'tick_size':x.get('tick_size'),'seconds_with_trades':x.get('seconds_with_trades'),'error':x.get('error')})
 if summ:
  with (OUT/'first_launch_summary.csv').open('w',newline='',encoding='utf-8-sig') as f: w=csv.DictWriter(f,fieldnames=list(summ[0]));w.writeheader();w.writerows(summ)
 print(json.dumps({'hits':len(hs),'anchor':'first valid price launch before breakout','out':str(OUT)},ensure_ascii=False),flush=True)
if __name__=='__main__': main()
