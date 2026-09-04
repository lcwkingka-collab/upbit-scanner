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
def price_anchor(a,pc):
 # Find actual price acceleration, not first volume spike. Score every rolling 1/2/3/5/10-minute upside move,
 # favoring fast expansion and meaningful KRW value. The anchor is the start minute of the best move.
 pts=[]
 for r in a:
  dt=datetime.fromisoformat(r['candle_date_time_utc']).replace(tzinfo=UTC).astimezone(KST); pts.append((dt,float(r['opening_price']),float(r['high_price']),float(r['trade_price']),float(r.get('candle_acc_trade_price') or 0)))
 best=None
 for i,(dt,op,hi,cl,val) in enumerate(pts):
  if op<=0: continue
  for w in (1,2,3,5,10):
   seg=pts[i:min(len(pts),i+w)]
   if not seg: continue
   mh=max(x[2] for x in seg); ret=(mh/op-1)*100; value=sum(x[4] for x in seg); speed=ret/max(w,1)
   # Require real upside; score speed first, then magnitude/value. This catches CHIP-like later explosions.
   if ret<=0: continue
   score=speed*3.0+ret*0.7+min(value/1e9,20)*0.03
   cand=(score,ret,-w,value,dt,w,mh)
   if best is None or cand[:4]>best[:4]: best=cand
 if best is None: raise RuntimeError('no price acceleration')
 _,ret,nw,value,dt,w,mh=best
 return dt,{'window_min':w,'window_return_pct':ret,'window_value_krw':value,'window_high':mh,'anchor_price':next(x[1] for x in pts if x[0]==dt)}
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
   a=mins(h['market'],h['day']); T,meta=price_anchor(a,h['prev_close']); st=T-timedelta(minutes=10); en=T+timedelta(minutes=10)
   s=ss.analyze_market(h['market'],st,en,enrich_trades=True); te=int(T.timestamp())
   for r in s.get('rows',[]):
    q=dict(r); q.update({'surge_day':h['day'],'day_high_ret_pct':h['high_ret_pct'],'price_launch_T_kst':T.isoformat(),'t_from_price_launch_sec':int(r['epoch_sec']-te),'price_launch_window_min':meta['window_min'],'price_launch_window_return_pct':meta['window_return_pct']}); sec.append(q)
   res.append({'hit':h,'price_launch_T_kst':T.isoformat(),'price_launch_meta':meta,'scan_from_kst':st.isoformat(),'scan_to_kst':en.isoformat(),'tick_size':s.get('tick_size'),'seconds_with_trades':len(s.get('rows',[]))})
   print(f'[{j}/{len(hs)}] {h["market"]} {h["day"]} T={T.isoformat()} {meta["window_min"]}m={meta["window_return_pct"]:.2f}% secs={len(s.get("rows",[]))}',flush=True)
  except Exception as e: res.append({'hit':h,'error':str(e)}); print('ERR',h['market'],e,flush=True)
  time.sleep(.2)
 (OUT/'price_accel_Tminus10_Tplus10.json').write_text(json.dumps({'generated_at_kst':datetime.now(KST).isoformat(),'anchor':'actual price acceleration','hits':len(hs),'results':res},ensure_ascii=False,indent=2),encoding='utf-8')
 if hs:
  with (OUT/'price_accel_hits.csv').open('w',newline='',encoding='utf-8-sig') as f: w=csv.DictWriter(f,fieldnames=list(hs[0]));w.writeheader();w.writerows(hs)
 if sec:
  fs=[]; seen=set()
  for r in sec:
   for k in r:
    if k not in seen: seen.add(k);fs.append(k)
  with (OUT/'price_accel_Tminus10_Tplus10_seconds.csv').open('w',newline='',encoding='utf-8-sig') as f: w=csv.DictWriter(f,fieldnames=fs,extrasaction='ignore');w.writeheader();w.writerows(sec)
 summ=[]
 for x in res:
  h=x['hit']; m=x.get('price_launch_meta') or {}; summ.append({'day':h['day'],'market':h['market'],'prev_close':h['prev_close'],'day_high':h['high'],'day_high_ret_pct':h['high_ret_pct'],'price_launch_T_kst':x.get('price_launch_T_kst'),'launch_window_min':m.get('window_min'),'launch_window_return_pct':m.get('window_return_pct'),'launch_anchor_price':m.get('anchor_price'),'tick_size':x.get('tick_size'),'seconds_with_trades':x.get('seconds_with_trades'),'error':x.get('error')})
 if summ:
  with (OUT/'price_accel_summary.csv').open('w',newline='',encoding='utf-8-sig') as f: w=csv.DictWriter(f,fieldnames=list(summ[0]));w.writeheader();w.writerows(summ)
 print(json.dumps({'hits':len(hs),'anchor':'actual price acceleration','out':str(OUT)},ensure_ascii=False),flush=True)
if __name__=='__main__': main()
