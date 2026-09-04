#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Build compact GitHub-friendly live summaries from localhost Live API.
Does NOT touch radar/Telegram. Writes local files only; git commit/push is handled by systemd timer shell command.
"""
from __future__ import annotations
import json, os, time, urllib.request
from pathlib import Path

API=os.getenv('LIVE_API_URL','http://127.0.0.1:8787')
OUT=Path(os.getenv('LIVE_PUBLISH_DIR','/home/ubuntu/upbit-scanner/data/live'))
TOP_N=int(os.getenv('LIVE_PUBLISH_TOP_N','40'))

def get(path):
    req=urllib.request.Request(API+path,headers={'User-Agent':'upbit-live-publisher/1.0'})
    with urllib.request.urlopen(req,timeout=10) as r:return json.load(r)

def main():
    OUT.mkdir(parents=True,exist_ok=True)
    health=get('/health'); live=get(f'/live?limit={TOP_N}')
    payload={'generated_at':time.time(),'health':health,'markets':live.get('markets'), 'active_markets':live.get('active_markets'),'rows':live.get('rows',[])}
    tmp=OUT/'latest.tmp'; dst=OUT/'latest.json'
    tmp.write_text(json.dumps(payload,ensure_ascii=False,separators=(',',':')),encoding='utf-8'); os.replace(tmp,dst)
    # tiny candidate file for fast ChatGPT reads
    candidates=[]
    for r in payload['rows']:
        x=r.get('value_vs_10m_x') or 0; bid=r.get('bid_ratio_1m') or 0; ret=r.get('return_1m_pct') or 0
        if x>=1.5 or bid>=0.60 or abs(ret)>=0.8:
            candidates.append(r)
    (OUT/'candidates.json').write_text(json.dumps({'generated_at':payload['generated_at'],'rows':candidates[:25]},ensure_ascii=False,separators=(',',':')),encoding='utf-8')
    print(f"[publisher] latest={len(payload['rows'])} candidates={len(candidates[:25])}",flush=True)
if __name__=='__main__':main()
