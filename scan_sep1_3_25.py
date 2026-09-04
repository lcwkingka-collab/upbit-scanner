#!/usr/bin/env python3
from __future__ import annotations
import csv,json,time
from datetime import datetime,timedelta,timezone
from pathlib import Path
import second_scan_api as ss
KST=timezone(timedelta(hours=9)); OUT=Path('sep1_3_25_output')
CASES=[('2026-09-01','KRW-AHT','17:20'),('2026-09-01','KRW-ONG','15:24'),('2026-09-02','KRW-MOC','09:07'),('2026-09-02','KRW-EGLD','13:19')]
def get(p,q=None): return ss.http_json(p,q)
def minute_rows(m,day):
 st=datetime.fromisoformat(day).replace(tzinfo=KST); en=st+timedelta(days=1); out={};cur=en
 for _ in range(20):
  a=get('/candles/minutes/1',{'market':m,'to':ss.iso_z(cur),'count':200})
  if not a:break
  old=None
  for r in a:
   d=datetime.fromisoformat(r['candle_date_time_kst']).replace(tzinfo=KST);old=d if old is None or d<old else old
   if st<=d<en:out[int(d.timestamp())]=r
  if old is None or old<=st:break
  cur=old;time.sleep(ss.RATE_SLEEP)
 return [out[k] for k in sorted(out)]
def main():
 OUT.mkdir(exist_ok=True); allrows=[]; manifest=[]
 for i,(day,m,hm) in enumerate(CASES,1):
  T=datetime.fromisoformat(f'{day}T{hm}:00').replace(tzinfo=KST); mins=minute_rows(m,day)
  after=[r for r in mins if datetime.fromisoformat(r['candle_date_time_kst']).replace(tzinfo=KST)>=T]
  if not after:continue
  hi=max(float(r['high_price']) for r in after); hr=next(r for r in after if float(r['high_price'])==hi); H=datetime.fromisoformat(hr['candle_date_time_kst']).replace(tzinfo=KST)
  # Scan from T+10 through the minute after the day high, in <=20m chunks so second/trade enrichment stays manageable.
  cur=T+timedelta(minutes=10); end=min(H+timedelta(minutes=2),T+timedelta(hours=12)); chunks=0
  while cur<end:
   ce=min(cur+timedelta(minutes=20),end)
   try:
    s=ss.analyze_market(m,cur,ce,enrich_trades=True);tick=s.get('tick_size')
    for r in s.get('rows') or []:
     q=dict(r);q.update({'day':day,'market':m,'T_kst':T.isoformat(),'t_from_T_sec':int(r['epoch_sec']-T.timestamp()),'tick_size':tick,'day_high':hi,'day_high_minute_kst':H.isoformat()});allrows.append(q)
    chunks+=1
   except Exception as e:print('chunk err',m,cur,e,flush=True)
   cur=ce;time.sleep(.1)
  manifest.append({'day':day,'market':m,'T_kst':T.isoformat(),'day_high':hi,'day_high_minute_kst':H.isoformat(),'scan_end_kst':end.isoformat(),'chunks':chunks})
  print(f'[{i}/4] {m} T={T.isoformat()} high={hi} at {H.isoformat()} chunks={chunks}',flush=True)
 if allrows:
  fs=[];seen=set()
  for r in allrows:
   for k in r:
    if k not in seen:seen.add(k);fs.append(k)
  with (OUT/'WAIT4_AFTER_TPLUS10_TO_HIGH_SECONDS.csv').open('w',newline='',encoding='utf-8-sig') as f:w=csv.DictWriter(f,fieldnames=fs,extrasaction='ignore');w.writeheader();w.writerows(allrows)
 with (OUT/'WAIT4_manifest.csv').open('w',newline='',encoding='utf-8-sig') as f:
  if manifest:w=csv.DictWriter(f,fieldnames=list(manifest[0]));w.writeheader();w.writerows(manifest)
 print(json.dumps({'cases':len(manifest),'rows':len(allrows)},ensure_ascii=False),flush=True)
if __name__=='__main__':main()
