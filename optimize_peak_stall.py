import json,csv,statistics
S='data/learning/v55/evidence/success_t_to_high_seconds.jsonl';O='data/learning/v55/evidence/success17_peak_stall_20260906.csv';F=.1
C=[json.loads(x) for x in open(S,encoding='utf8') if x.strip()][:17]
def run(c,a,d,age):
 p=float(c['t_price']); peak=p; pt=0
 for x in c['candles']:
  t=x['timestamp']/1000; h=float(x['high_price']); z=float(x['trade_price'])
  if h>peak: peak=h;pt=t
  if (peak/p-1)*100>=a and t-pt>=age and z<=peak*(1-d/100): return (z/p-1)*100-F,(peak/p-1)*100
 z=float(c['candles'][-1]['trade_price']);return (z/p-1)*100-F,(peak/p-1)*100
R=[]
for a in (1,2,3,5,7,10):
 for d in (.5,.75,1,1.5,2,3,4,5):
  for g in (15,30,45,60,90,120,180,300,600):
   v=[run(c,a,d,g) for c in C];cap=[100*x/y for x,y in v if y>0]
   R.append({'activate':a,'drop':d,'no_high_sec':g,'avg_net':statistics.mean(x for x,y in v),'median_net':statistics.median(x for x,y in v),'avg_capture':statistics.mean(cap),'median_capture':statistics.median(cap),'wins':sum(x>0 for x,y in v),'worst':min(x for x,y in v)})
R.sort(key=lambda x:(x['avg_capture'],x['avg_net']),reverse=True)
with open(O,'w',encoding='utf-8-sig',newline='') as f:w=csv.DictWriter(f,fieldnames=R[0]);w.writeheader();w.writerows(R)
print('CASES',len(C),'RULES',len(R))
for i,x in enumerate(R[:15],1):print(i,x)
print(O)
