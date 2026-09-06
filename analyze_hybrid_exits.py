import json,csv,statistics,os
SRC='data/learning/v55/evidence/success_t_to_high_seconds.jsonl'
OUT='data/learning/v55/evidence/success17_hybrid_exit_20260906.csv'
REPORT='data/learning/v55/success17_hybrid_exit_20260906.md'
FEE=.10
cases=[json.loads(x) for x in open(SRC,encoding='utf-8') if x.strip()][:17]

def run(c,retain,activate,cutmin):
 p=float(c['t_price']); peak=p; armed=False; cs=c['candles']; cutoff=cutmin*60
 for x in cs:
  sec=(x['timestamp']/1000)-__import__('datetime').datetime.fromisoformat(c['t']).timestamp()
  peak=max(peak,float(x['high_price'])); gain=(peak/p-1)*100
  if gain>=activate: armed=True
  stop=p+(peak-p)*retain
  if armed and float(x['low_price'])<=stop: return (stop/p-1)*100-FEE,'trail',sec,gain
  if not armed and sec>=cutoff: return (float(x['trade_price'])/p-1)*100-FEE,'time',sec,gain
 last=float(cs[-1]['trade_price']) if cs else p
 return (last/p-1)*100-FEE,'end',cutoff,(peak/p-1)*100

rows=[]
for retain in (.5,.6,.7,.75,.8):
 for activate in (3,4,5):
  for cut in (5,10,15,20,30,60):
   vals=[]
   for c in cases:
    ret,why,sec,pk=run(c,retain,activate,cut); vals.append((ret,why))
   rows.append({'retain_pct':int(retain*100),'activate_pct':activate,'fallback_min':cut,
    'avg_net_pct':statistics.mean(x[0] for x in vals),'median_net_pct':statistics.median(x[0] for x in vals),
    'wins':sum(x[0]>0 for x in vals),'losses':sum(x[0]<=0 for x in vals),'worst_net_pct':min(x[0] for x in vals),
    'trail_exits':sum(x[1]=='trail' for x in vals),'time_exits':sum(x[1]=='time' for x in vals)})
rows.sort(key=lambda r:(r['avg_net_pct'],r['median_net_pct'],r['worst_net_pct']),reverse=True)
with open(OUT,'w',encoding='utf-8-sig',newline='') as f:
 w=csv.DictWriter(f,fieldnames=rows[0]);w.writeheader();w.writerows(rows)
with open(REPORT,'w',encoding='utf-8') as f:
 f.write('# 성공군 17개 혼합 매도 역산 (2026-09-06)\n\n')
 f.write('- 초봉, T 이후 최대 4시간. 왕복 수수료 0.10% 반영, 과거 호가 슬리피지는 미반영.\n- 활성수익 도달 시 진행고가 보존선, 미도달 시 지정시간 시장가 청산. 후보 비교용이며 운영코드 미적용.\n\n')
 f.write('|순위|보존선|활성수익|시간청산|평균 순수익|중앙값|승/패|최악|추적/시간|\n|---:|---:|---:|---:|---:|---:|---:|---:|---:|\n')
 for i,r in enumerate(rows,1):
  f.write(f"|{i}|{r['retain_pct']}%|+{r['activate_pct']}%|{r['fallback_min']}분|{r['avg_net_pct']:.3f}%|{r['median_net_pct']:.3f}%|{r['wins']}/{r['losses']}|{r['worst_net_pct']:.3f}%|{r['trail_exits']}/{r['time_exits']}|\n")
print('CASES',len(cases),'RULES',len(rows),'FEE',FEE)
for i,r in enumerate(rows[:15],1): print(i,r)
print(OUT);print(REPORT)
