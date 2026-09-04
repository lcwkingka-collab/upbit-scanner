#!/usr/bin/env python3
from __future__ import annotations
import csv,json,time
from datetime import datetime,timedelta,timezone
from pathlib import Path
import second_scan_api as ss
KST=timezone(timedelta(hours=9)); UTC=timezone.utc; OUT=Path('sep1_3_25_output')
ANCHORS=[
('2026-09-01','KRW-CRV','20:54'),('2026-09-01','KRW-SC','08:58'),('2026-09-01','KRW-BONK','00:16'),('2026-09-01','KRW-ICX','09:22'),('2026-09-01','KRW-IQ','09:03'),('2026-09-01','KRW-AHT','17:20'),('2026-09-01','KRW-ONG','15:24'),
('2026-09-02','KRW-T','09:01'),('2026-09-02','KRW-SOPH','08:59'),('2026-09-02','KRW-MOC','09:07'),('2026-09-02','KRW-EGLD','13:19'),('2026-09-02','KRW-INJ','16:13'),
('2026-09-03','KRW-CHIP','06:40'),('2026-09-03','KRW-SNT','09:03'),('2026-09-03','KRW-ANKR','08:58'),('2026-09-03','KRW-HIVE','09:00')]
def main():
 OUT.mkdir(exist_ok=True); allrows=[]; summary=[]
 for i,(day,m,hm) in enumerate(ANCHORS,1):
  T=datetime.fromisoformat(f'{day}T{hm}:00').replace(tzinfo=KST); st=T-timedelta(minutes=10); en=T+timedelta(minutes=10)
  try:
   s=ss.analyze_market(m,st,en,enrich_trades=True); rows=s.get('rows') or []; te=int(T.timestamp()); tick=s.get('tick_size')
   for r in rows:
    q=dict(r); q.update({'day':day,'market':m,'T_kst':T.isoformat(),'t_from_T_sec':int(r['epoch_sec']-te),'tick_size':tick}); allrows.append(q)
   summary.append({'day':day,'market':m,'T_kst':T.isoformat(),'from_kst':st.isoformat(),'to_kst':en.isoformat(),'tick_size':tick,'seconds_with_trades':len(rows)})
   print(f'[{i}/16] {m} T={T.isoformat()} secs={len(rows)} tick={tick}',flush=True)
  except Exception as e:
   summary.append({'day':day,'market':m,'T_kst':T.isoformat(),'error':str(e)}); print('ERR',m,e,flush=True)
  time.sleep(.15)
 if allrows:
  fields=[];seen=set()
  for r in allrows:
   for k in r:
    if k not in seen:seen.add(k);fields.append(k)
  with (OUT/'ANCHOR_2P8X_Tminus10_Tplus10_SECONDS.csv').open('w',newline='',encoding='utf-8-sig') as f:w=csv.DictWriter(f,fieldnames=fields,extrasaction='ignore');w.writeheader();w.writerows(allrows)
 if summary:
  fields=[];seen=set()
  for r in summary:
   for k in r:
    if k not in seen:seen.add(k);fields.append(k)
  with (OUT/'ANCHOR_2P8X_summary.csv').open('w',newline='',encoding='utf-8-sig') as f:w=csv.DictWriter(f,fieldnames=fields,extrasaction='ignore');w.writeheader();w.writerows(summary)
 (OUT/'ANCHOR_2P8X_manifest.json').write_text(json.dumps({'definition':'T = selected first valid 1m value ratio >= 2.8x near first launch','range':'T-10m..T+10m','anchors':[{'day':d,'market':m,'time':h} for d,m,h in ANCHORS]},ensure_ascii=False,indent=2),encoding='utf-8')
 print(json.dumps({'anchors':16,'rows':len(allrows),'range':'T-10m..T+10m'},ensure_ascii=False),flush=True)
if __name__=='__main__':main()
