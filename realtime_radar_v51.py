#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Upbit KRW Real-time Radar V5.1

4-stage state machine:
1) first acceleration: baseline >=2.0x and previous same-window >=2.5x
2) NEW data after stage1 must improve acceleration
3) NEW data after stage2 must improve/re-accelerate again
4) NEW data after stage3 must remain strong AND price >= +1.0% from stage1

No fixed stage timers. Same snapshot can never promote multiple stages.
"""
from __future__ import annotations
import json, math, os, signal, statistics, threading, time, urllib.parse, urllib.request, uuid
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple
try:
    import websocket
except ImportError as exc:
    raise SystemExit("websocket-client가 필요합니다: pip install websocket-client") from exc

UPBIT_REST="https://api.upbit.com/v1"
UPBIT_WS="wss://api.upbit.com/websocket/v1"
WINDOWS=tuple(range(1,11))
WARMUP_SEC=300
KEEP_SEC=720
BASELINE_START_SEC=300
BASELINE_EXCLUDE_RECENT_SEC=30
STAGE1_BASELINE_X=2.0
STAGE1_PREV_X=2.5
FINAL_PRICE_RETURN=1.0
EVAL_INTERVAL=1.0
RECONNECT_DELAY=3.0
STABLE_MARKETS={"KRW-USDT","KRW-USDC","KRW-DAI","KRW-USDE"}
BOT_TOKEN=os.getenv("TELEGRAM_BOT_TOKEN","").strip()
CHAT_ID=os.getenv("TELEGRAM_CHAT_ID","").strip()
STOP=threading.Event(); LOCK=threading.RLock(); STARTED_AT=time.time()

@dataclass
class Bucket:
    value:float=0.0; bid:float=0.0; ask:float=0.0; count:int=0; last_price:Optional[float]=None

@dataclass
class Snap:
    sec:int=0; score:float=0.0; base_x:float=0.0; prev_x:float=0.0; active_count:int=0
    bid_share:float=0.5; net_buy:float=0.0; price:Optional[float]=None; best_window:int=0

@dataclass
class CoinState:
    stage:int=0; cycle_id:int=0; stage1_price:Optional[float]=None; last_snap:Snap=field(default_factory=Snap)
    cancelled:bool=False; last_dead_sec:int=0

BUCKETS:Dict[str,Dict[int,Bucket]]={}; STATES:Dict[str,CoinState]={}; NAMES:Dict[str,Tuple[str,str]]={}; MARKETS:List[str]=[]

def http_json(url,data=None,timeout=15):
    headers={"User-Agent":"upbit-radar-v51/1.0"}; body=None
    if data is not None:
        body=urllib.parse.urlencode(data).encode(); headers["Content-Type"]="application/x-www-form-urlencoded"
    with urllib.request.urlopen(urllib.request.Request(url,data=body,headers=headers),timeout=timeout) as r:return json.load(r)

def telegram(text):
    if not BOT_TOKEN or not CHAT_ID: print("[telegram disabled]",text.replace("\n"," | "),flush=True); return
    try:http_json(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",{"chat_id":CHAT_ID,"text":text,"disable_web_page_preview":"true"},10)
    except Exception as e:print("[telegram error]",e,flush=True)

def fetch_markets():
    global MARKETS
    for row in http_json(f"{UPBIT_REST}/market/all?is_details=false"):
        m=str(row.get("market",""))
        if not m.startswith("KRW-") or m in STABLE_MARKETS:continue
        NAMES[m]=(str(row.get("english_name") or m.split("-")[1]),str(row.get("korean_name") or m.split("-")[1]))
        BUCKETS[m]={}; STATES[m]=CoinState(); MARKETS.append(m)
    MARKETS.sort(); print(f"[markets] {len(MARKETS)}",flush=True)

def title(m):
    en,ko=NAMES.get(m,(m,m)); return f"{m.split('-',1)[-1]} / {ko} · {en} ({m})"

def add_trade(row):
    m=row.get("code")
    if m not in NAMES:return
    try:p=float(row["trade_price"]); v=float(row["trade_volume"]); sec=int(row.get("timestamp") or time.time()*1000)//1000
    except:return
    with LOCK:
        b=BUCKETS[m].setdefault(sec,Bucket()); val=p*v; b.value+=val; b.count+=1; b.last_price=p
        if row.get("ask_bid")=="BID":b.bid+=val
        elif row.get("ask_bid")=="ASK":b.ask+=val
        cutoff=int(time.time())-KEEP_SEC
        for k in [x for x in BUCKETS[m] if x<cutoff]:BUCKETS[m].pop(k,None)

def sum_range(bs,a,z):
    out={"value":0.0,"bid":0.0,"ask":0.0,"count":0,"price":None}; lp=-1
    for s in range(a,z+1):
        b=bs.get(s)
        if not b:continue
        out["value"]+=b.value; out["bid"]+=b.bid; out["ask"]+=b.ask; out["count"]+=b.count
        if b.last_price is not None and s>=lp:out["price"]=b.last_price; lp=s
    return out

def last_price(bs,sec,look=120):
    for s in range(sec,sec-look-1,-1):
        if s in bs and bs[s].last_price is not None:return bs[s].last_price
    return None

def ratio(a,b):return a/b if b>0 else 0.0

def baseline(bs,now,w):
    vals=[]; end=now-BASELINE_EXCLUDE_RECENT_SEC; oldest=now-BASELINE_START_SEC
    while end>=oldest:
        vals.append(sum_range(bs,end-w+1,end)["value"]); end-=max(1,w)
    if not vals:return 0.0
    mean=statistics.fmean(vals); nz=[x for x in vals if x>0]
    return max(mean,(statistics.median(nz)*0.35 if nz else 0),1.0)

def metric(bs,now,w):
    cur=sum_range(bs,now-w+1,now); prev=sum_range(bs,now-2*w+1,now-w); base=baseline(bs,now,w)
    # Critical zero-denominator protection: previous floor follows own normal baseline.
    px=ratio(cur["value"],max(prev["value"],base*0.25,1.0)); bx=ratio(cur["value"],base)
    active=bx>=STAGE1_BASELINE_X and px>=STAGE1_PREV_X and cur["value"]>prev["value"]
    return {"w":w,"cur":cur,"prev":prev,"base":base,"base_x":bx,"prev_x":px,"active":active}

def compute(m,now):
    with LOCK:bs=dict(BUCKETS.get(m,{}))
    if not bs or time.time()-STARTED_AT<WARMUP_SEC:return None
    ms=[metric(bs,now,w) for w in WINDOWS]; active=[x for x in ms if x["active"]]
    best=max(active or ms,key=lambda x:(x["base_x"]*x["prev_x"],x["cur"]["value"]))
    f5=sum_range(bs,now-4,now); p5=sum_range(bs,now-9,now-5); f10=sum_range(bs,now-9,now); p10=sum_range(bs,now-19,now-10)
    total=f10["bid"]+f10["ask"]; share=f10["bid"]/total if total else .5; net=f10["bid"]-f10["ask"]
    ptotal=p10["bid"]+p10["ask"]; pshare=p10["bid"]/ptotal if ptotal else .5; pnet=p10["bid"]-p10["ask"]
    improving=(net>pnet) or (share>=pshare+.05) or (net>0 and share>=.50)
    confirmed=(net>0 and share>=.55 and share>=pshare) or (f5["bid"]>f5["ask"] and share>=.50)
    slopes={w:ms[w-1]["cur"]["value"]-ms[w-1]["prev"]["value"] for w in (1,3,5,7,10)}
    long_weak=slopes[5]<0 and slopes[7]<0 and slopes[10]<0
    deep=f5["value"]<p5["value"]*.70 and f10["value"]<p10["value"]*.80
    normalized=max(x["base_x"] for x in ms)<1.40
    dead=long_weak and (deep or normalized or (share<.40 and net<0))
    price=last_price(bs,now,3)
    score=min(best["base_x"],12)+min(best["prev_x"],15)+max(0,(share-.5)*20)+len(active)*2
    return {"market":m,"active":active,"count":len(active),"best":best,"share":share,"net":net,"improving":improving,"confirmed":confirmed,"dead":dead,"price":price,"score":score}

def snap(m,sec):
    b=m["best"]; return Snap(sec,m["score"],b["base_x"],b["prev_x"],m["count"],m["share"],m["net"],m["price"],b["w"])

def genuinely_better(m,old:Snap,stage):
    """A promotion MUST consume a later second and materially new evidence."""
    if old.sec<=0:return False
    now=int(time.time())-1
    if now<=old.sec:return False
    b=m["best"]
    # At least two independent improvement dimensions; no fixed waiting time.
    checks=[
        m["score"]>=old.score+2.0,
        b["base_x"]>=old.base_x*1.10,
        b["prev_x"]>=old.prev_x*1.10,
        m["count"]>=old.active_count+1,
        m["share"]>=old.bid_share+.03,
        m["net"]>old.net_buy and m["net"]>0,
    ]
    need=2 if stage==1 else 3
    return sum(bool(x) for x in checks)>=need

def fmt_money(v):
    if abs(v)>=100_000_000:return f"{v/100_000_000:.2f}억"
    if abs(v)>=10_000:return f"{v/10_000:.0f}만"
    return f"{v:,.0f}"

def price_from_stage1(st,m):
    if not st.stage1_price or not m["price"]:return None
    return (m["price"]/st.stage1_price-1)*100

def alert(m,stage,st):
    b=m["best"]; pr=price_from_stage1(st,m)
    heads={1:"⚠️ 1차 유의",2:"🔥 2차 추천",3:"🚀 3차 강매수 후보",4:"✅ 4차 최종 매수확인"}
    why={1:"최초 거래대금 급가속 감지",2:"1차 이후 새 데이터에서 추가 가속",3:"2차 이후 새 데이터에서 재가속/강화",4:"3차 이후 가속 지속 + 실제 가격반응 확인"}
    return (f"{title(m['market'])}\n{heads[stage]}\n{why[stage]}\n"
            f"감지창: {b['w']}초 | 활성창: {m['count']}개\n평시 대비: {b['base_x']:.2f}x\n직전 동일창 대비: {b['prev_x']:.2f}x\n"
            f"BID 10초: {m['share']*100:.1f}% | 순매수: {fmt_money(m['net'])}\n"
            f"1차 대비 가격: {(f'{pr:+.2f}%' if pr is not None else 'N/A')}")

def stop_text(m,st,reason):
    pr=price_from_stage1(st,m)
    return (f"{title(m['market'])}\n⛔ 사이클 중단\n{reason}\n"
            f"1차 대비 가격: {(f'{pr:+.2f}%' if pr is not None else 'N/A')}\n재가속 시 새 1차부터 재탐지")

def reset(st):
    st.stage=0; st.stage1_price=None; st.last_snap=Snap(); st.cancelled=False

def evaluate_once():
    sec=int(time.time())-1
    for market in MARKETS:
        try:m=compute(market,sec)
        except Exception as e:print("[metric error]",market,e,flush=True); continue
        if not m:continue
        st=STATES[market]
        if st.stage>0 and m["dead"]:
            telegram(stop_text(m,st,"거래대금 가속 소멸/다중창 감속")); print("[stop]",market,"dead",flush=True); reset(st); continue
        if st.stage==0:
            if m["count"]>=1 and m["price"] is not None:
                st.cycle_id+=1; st.stage=1; st.stage1_price=m["price"]; st.last_snap=snap(m,sec)
                telegram(alert(m,1,st)); print("[alert]",market,"stage=1",flush=True)
            continue
        # Never promote on the same second/snapshot.
        if sec<=st.last_snap.sec:continue
        if st.stage==1:
            if m["count"]>=2 and m["improving"] and genuinely_better(m,st.last_snap,1):
                st.stage=2; st.last_snap=snap(m,sec); telegram(alert(m,2,st)); print("[alert]",market,"stage=2",flush=True)
        elif st.stage==2:
            if m["count"]>=3 and m["confirmed"] and genuinely_better(m,st.last_snap,2):
                st.stage=3; st.last_snap=snap(m,sec); telegram(alert(m,3,st)); print("[alert]",market,"stage=3",flush=True)
        elif st.stage==3:
            # Stage4 requires NEW post-stage3 evidence plus actual +1% price response from stage1.
            pr=price_from_stage1(st,m)
            if m["confirmed"] and genuinely_better(m,st.last_snap,3):
                if pr is not None and pr>=FINAL_PRICE_RETURN:
                    st.stage=4; st.last_snap=snap(m,sec); telegram(alert(m,4,st)); print("[alert]",market,"stage=4",flush=True)
                else:
                    telegram(stop_text(m,st,f"최종 확인 실패: 1차 대비 +{FINAL_PRICE_RETURN:.1f}% 가격반응 미달")); print("[stop]",market,"price reaction fail",flush=True); reset(st)
        elif st.stage==4 and m["dead"]:
            telegram(stop_text(m,st,"최종 확인 후 가속 소멸")); reset(st)

def evaluator_loop():
    nxt=math.floor(time.time())+1
    while not STOP.is_set():
        d=nxt-time.time()