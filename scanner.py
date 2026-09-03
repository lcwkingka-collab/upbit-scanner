import csv, json, math, os, time, urllib.parse, urllib.request
from datetime import datetime, timedelta, timezone

BASE = "https://api.upbit.com/v1"

def get(path, params=None, tries=5):
    url = BASE + path + ("?" + urllib.parse.urlencode(params) if params else "")
    err = None
    for n in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent":"upbit-scanner/1.0"})
            with urllib.request.urlopen(req, timeout=30) as r: return json.load(r)
        except Exception as e:
            err=e; time.sleep(0.7*(n+1))
    raise err

def ema(xs,n):
    a=2/(n+1); out=[xs[0]]
    for x in xs[1:]: out.append(a*x+(1-a)*out[-1])
    return out

def rsi(xs,n=14):
    if len(xs)<=n:return None
    ds=[xs[i]-xs[i-1] for i in range(1,len(xs))]; g=[max(x,0) for x in ds]; l=[max(-x,0) for x in ds]
    ag=sum(g[:n])/n; al=sum(l[:n])/n
    for i in range(n,len(ds)): ag=(ag*(n-1)+g[i])/n; al=(al*(n-1)+l[i])/n
    return 100 if al==0 else 100-100/(1+ag/al)

def adx(h,l,c,n=14):
    if len(c)<2*n+1:return None
    tr=[]; pd=[]; md=[]
    for i in range(1,len(c)):
        tr.append(max(h[i]-l[i],abs(h[i]-c[i-1]),abs(l[i]-c[i-1])))
        u=h[i]-h[i-1]; d=l[i-1]-l[i]; pd.append(u if u>d and u>0 else 0); md.append(d if d>u and d>0 else 0)
    atr=sum(tr[:n]); ps=sum(pd[:n]); ms=sum(md[:n]); dx=[]
    for i in range(n-1,len(tr)):
        if i>=n: atr=atr-atr/n+tr[i]; ps=ps-ps/n+pd[i]; ms=ms-ms/n+md[i]
        p=100*ps/atr if atr else 0; m=100*ms/atr if atr else 0; dx.append(100*abs(p-m)/(p+m) if p+m else 0)
    a=sum(dx[:n])/n
    for x in dx[n:]:a=(a*(n-1)+x)/n
    return a

def pivots(v,low=True):
    return [v[i] for i in range(2,len(v)-2) if v[i]==(min(v[i-2:i+3]) if low else max(v[i-2:i+3]))]

def calc(mkt, raw):
    a=list(reversed(raw)); c=[x['trade_price'] for x in a]; h=[x['high_price'] for x in a]; l=[x['low_price'] for x in a]
    v=[x['candle_acc_trade_volume'] for x in a]; tv=[x['candle_acc_trade_price'] for x in a]; o=[x['opening_price'] for x in a]; px=c[-1]
    ma=lambda n: sum(c[-n:])/n if len(c)>=n else None
    m15,m50,m120=ma(15),ma(50),ma(120); m15p=sum(c[-16:-1])/15 if len(c)>=16 else None
    e12,e26=ema(c,12),ema(c,26); mac=[x-y for x,y in zip(e12,e26)]; sig=ema(mac,9); hist=[x-y for x,y in zip(mac,sig)]
    rr=rsi(c); aa=adx(h,l,c); lows=pivots(l[-90:],True); highs=pivots(h[-90:],False)
    sup=max([x for x in lows if x<=px],default=min(l[-30:])); res=min([x for x in highs if x>px],default=max(h[-30:]))
    vr=(sum(v[-3:])/3)/(sum(v[-13:-3])/10) if len(v)>=13 and sum(v[-13:-3]) else None
    tvr=(sum(tv[-3:])/3)/(sum(tv[-13:-3])/10) if len(tv)>=13 and sum(tv[-13:-3]) else None
    d1=px/c[-2]-1 if len(c)>1 else None; d5=px/c[-6]-1 if len(c)>5 else None; d20=px/c[-21]-1 if len(c)>20 else None
    hl=len(lows)>=2 and lows[-1]>lows[-2]; near_sup=px/sup-1; near_res=res/px-1
    mac_up=len(hist)>1 and hist[-1]>hist[-2] and mac[-1]>mac[-2]; cross=len(mac)>1 and mac[-1]>sig[-1] and mac[-2]<=sig[-2]
    excluded=bool((d5 or 0)>.18 or (d20 or 0)>.35 or (rr or 0)>68 or px/o[-1]-1>.09 or (0<near_res<.018 and (d5 or 0)>.06))
    score=(2 if near_sup<=.05 else 1 if near_sup<=.10 else 0)+(2 if rr and 45<=rr<=60 else 1 if rr and 40<=rr<=64 else 0)+(2 if cross else 1 if mac_up and mac[-1]<0 else 0)+(2 if aa and aa>=25 else 1 if aa and aa>=20 else 0)+(2 if vr and tvr and vr>=1.35 and tvr>=1.35 else 1 if (vr or 0)>=1.1 or (tvr or 0)>=1.1 else 0)+(2 if hl else 0)+(2 if m15 and m50 and m15<m50 and 0<=(m50-m15)/m50<=.035 and m15p and m15>m15p else 1 if m15 and m15p and m15>m15p else 0)-(5 if excluded else 0)
    return dict(market=mkt,days=len(c),price=px,ma15=m15,ma50=m50,ma120=m120,rsi=rr,adx=aa,macd=mac[-1],signal=sig[-1],hist=hist[-1],hist_prev=hist[-2],mac_up=mac_up,mac_cross=cross,higher_low=hl,support=sup,resistance=res,near_support=near_sup,near_resistance=near_res,volume_accel=vr,value_accel=tvr,d1=d1,d5=d5,d20=d20,candle=px/o[-1]-1,excluded=excluded,score=score)

def main():
    markets=[x for x in get('/market/all',{'is_details':'true'}) if x['market'].startswith('KRW-')]
    rows=[]; errors=[]
    for i,m in enumerate(markets,1):
        try: rows.append(calc(m['market'],get('/candles/days',{'market':m['market'],'count':200})))
        except Exception as e: errors.append({'market':m['market'],'error':str(e)})
        if i%10==0: print(f'{i}/{len(markets)}',flush=True)
        time.sleep(.12)
    rows.sort(key=lambda x:x['score'],reverse=True)
    kst=datetime.now(timezone(timedelta(hours=9))).isoformat()
    out={'kst':kst,'market_count':len(markets),'complete_indicators':sum(r['days']>=120 for r in rows),'short_history':sum(r['days']<120 for r in rows),'evaluated':len(rows),'errors':errors,'rows':rows}
    os.makedirs('data',exist_ok=True)
    json.dump(out,open('data/latest_scan.json','w'),ensure_ascii=False,indent=2)
    if rows:
        with open('data/latest_scan.csv','w',newline='') as f:
            w=csv.DictWriter(f,fieldnames=rows[0].keys()); w.writeheader(); w.writerows(rows)
    print(json.dumps({k:out[k] for k in ('kst','market_count','complete_indicators','short_history','evaluated')},ensure_ascii=False))

if __name__=='__main__': main()
