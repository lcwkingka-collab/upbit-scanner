import importlib
import sys
import types
from collections import deque
from datetime import timedelta, timezone


def load_module(tmp_path):
    sent=[]
    fake_r=types.SimpleNamespace(
        telegram=lambda msg:sent.append(msg), title=lambda m:m,
        fmt_money=lambda x:str(x), STARTED_AT=0, MARKETS=[],
    )
    fake=types.ModuleType('realtime_radar_v54')
    fake.r=fake_r;fake.KST=timezone(timedelta(hours=9));fake.TRADE_EVENTS={};fake.TICK_SIZE={}
    fake.current_price=lambda _m,_s:100.0
    fake.minute_value_x=lambda _m,_s:(3.0,3.0,1.0)
    fake.flow10=lambda _m,_s:{'cur':{'bid':5_000_000.0,'ask':1_000_000.0,'net':4_000_000.0,'bid_count':5,'ask_count':2,'share':5/6},'prev':{'bid':1_000_000.0,'ask':1_000_000.0,'bid_count':2,'ask_count':2}}
    sys.modules['realtime_radar_v54']=fake;sys.modules.pop('realtime_radar_v55',None)
    mod=importlib.import_module('realtime_radar_v55');mod.EVENT_LOG_DIR=tmp_path
    return mod,sent


def test_strengthened_launch_metrics(tmp_path):
    m,_=load_module(tmp_path);market='KRW-X';m.v54.TICK_SIZE[market]=1
    m.v54.TRADE_EVENTS[market]=deque([
        (100_000,100,'BID',10),(101_000,102,'BID',10),(102_000,104,'BID',10),
        (102_100,104,'BID',10),(102_200,104,'BID',10),(102_300,104,'BID',10),(102_400,104,'ASK',10),
    ])
    st=m.V55State(stage=3,t_sec=1,t_price=96)
    out=m.launch_metrics(market,102,st,m.v54.flow10(market,102))
    assert out['ok'];assert out['high_ticks']>=4;assert out['rise_pct']>=.40;assert out['t_ticks']>=8


def test_stage6_plus3(tmp_path):
    m,sent=load_module(tmp_path);market='KRW-X';m.v54.TICK_SIZE[market]=1
    st=m.V55State(stage=5,t_sec=1,t_price=90,stage5_sec=100,stage5_price=100,stage5_last_trade_sec=100)
    m.ST[market]=st;m.v54.TRADE_EVENTS[market]=deque([(101_000,103,'BID',10)])
    m.evaluate_stage5(market,101,st)
    assert st.stage==6;assert st.stage6_price==103;assert sent==[]


def test_stage7_and_stage8_only_final_telegram(tmp_path):
    m,sent=load_module(tmp_path);market='KRW-X';m.v54.TICK_SIZE[market]=1
    st=m.V55State(stage=6,t_sec=1,t_price=90,stage6_sec=100,stage6_price=100)
    m.ST[market]=st;m.v54.TRADE_EVENTS[market]=deque([(105_000,100.5,'BID',10)])
    m.evaluate_confirmation(market,105,st,100.5)
    assert st.stage==7;assert sent==[]
    m.v54.TRADE_EVENTS[market].append((106_000,101.1,'BID',10))
    m.evaluate_confirmation(market,106,st,101.1)
    assert st.stage==8;assert len(sent)==1;assert '8차 최종' in sent[0]


def test_late_extended_stage8_recycles_to_stage3(tmp_path):
    m,sent=load_module(tmp_path);market='KRW-X';m.v54.TICK_SIZE[market]=1
    m.PREV_CLOSE[market]=80.0
    st=m.V55State(stage=7,t_sec=100, t_price=79, stage6_sec=1990,stage6_price=100,
                   stage7_sec=1994,stage7_price=100.5)
    m.ST[market]=st;m.v54.TRADE_EVENTS[market]=deque([(2000_000,101,'BID',10)])
    m.evaluate_confirmation(market,2000,st,101)
    assert st.stage==3;assert sent==[]
    rows=(tmp_path/'19700101'/'v55_events.jsonl').read_text(encoding='utf-8')
    assert 'stage8_late_entry_recycle' in rows


def test_fast_extended_stage8_is_preserved(tmp_path):
    m,sent=load_module(tmp_path);market='KRW-X';m.v54.TICK_SIZE[market]=1
    m.PREV_CLOSE[market]=80.0
    st=m.V55State(stage=7,t_sec=500, t_price=79, stage6_sec=590,stage6_price=100,
                   stage7_sec=594,stage7_price=100.5)
    m.ST[market]=st;m.v54.TRADE_EVENTS[market]=deque([(600_000,101,'BID',10)])
    m.evaluate_confirmation(market,600,st,101)
    assert st.stage==8;assert len(sent)==1


def test_late_but_below_20pct_stage8_is_preserved(tmp_path):
    m,sent=load_module(tmp_path);market='KRW-X';m.v54.TICK_SIZE[market]=1
    m.PREV_CLOSE[market]=85.0
    st=m.V55State(stage=7,t_sec=100, t_price=84, stage6_sec=1990,stage6_price=100,
                   stage7_sec=1994,stage7_price=100.5)
    m.ST[market]=st;m.v54.TRADE_EVENTS[market]=deque([(2000_000,101,'BID',10)])
    m.evaluate_confirmation(market,2000,st,101)
    assert st.stage==8;assert len(sent)==1


def test_stage7_timeout_recycles_to_stage3(tmp_path):
    m,sent=load_module(tmp_path);market='KRW-X';m.v54.TICK_SIZE[market]=1
    st=m.V55State(stage=6,t_sec=1,t_price=90,stage6_sec=100,stage6_price=100)
    m.ST[market]=st;m.v54.TRADE_EVENTS[market]=deque()
    m.evaluate_confirmation(market,130,st,100.4)
    assert st.stage==3;assert sent==[]


def test_stage8_timeout_recycles_to_stage3(tmp_path):
    m,sent=load_module(tmp_path);market='KRW-X';m.v54.TICK_SIZE[market]=1
    st=m.V55State(stage=7,t_sec=1,t_price=90,stage6_sec=100,stage6_price=100,
                   stage7_sec=105,stage7_price=100.5)
    m.ST[market]=st;m.v54.TRADE_EVENTS[market]=deque()
    m.evaluate_confirmation(market,400,st,100.9)
    assert st.stage==3;assert sent==[]


def test_t02_blocks_repeat_stage8_telegram(tmp_path):
    m,sent=load_module(tmp_path);market='KRW-X';m.v54.TICK_SIZE[market]=1
    m.LAST_STAGE8_SEC[market]=100
    st=m.V55State(stage=7,t_sec=1,t_price=90,stage6_sec=200,stage6_price=100,
                   stage7_sec=205,stage7_price=100.5)
    m.ST[market]=st;m.v54.TRADE_EVENTS[market]=deque([(210_000,101,'BID',10)])
    m.evaluate_confirmation(market,210,st,101)
    assert st.stage==8;assert sent==[]


def test_stage4_and_stage5_are_log_only(tmp_path):
    m,sent=load_module(tmp_path);market='KRW-X';m.v54.TICK_SIZE[market]=1
    m.ST[market]=m.V55State(stage=3,t_sec=1,t_price=90)
    m.v54.TRADE_EVENTS[market]=deque([
        (100_000,100,'BID',10),(101_000,102,'BID',10),(102_000,104,'BID',10),
        (102_100,104,'BID',10),(102_200,104,'BID',10),(102_300,104,'BID',10),(102_400,104,'ASK',10),
    ])
    m.evaluate_market(market,102)
    assert m.ST[market].stage==5;assert sent==[]


def test_stage5_is_ttl_exempt_but_has_12h_cap(tmp_path):
    m,_=load_module(tmp_path);market='KRW-X';m.v54.TICK_SIZE[market]=1
    st=m.V55State(stage=5,t_sec=0,t_price=90,stage5_sec=100,stage5_price=100,stage5_last_trade_sec=100)
    m.ST[market]=st;m.v54.TRADE_EVENTS[market]=deque([(14_400_000,100,'BID',10)])
    m.evaluate_market(market,14_400)
    assert m.ST[market].stage==5
    m.v54.TRADE_EVENTS[market].append(((100+12*3600)*1000,100,'BID',10))
    m.evaluate_market(market,100+12*3600)
    assert m.ST[market].stage==0


def test_expired_primary_hands_over_reserve(tmp_path):
    m,_=load_module(tmp_path);market='KRW-X';m.v54.TICK_SIZE[market]=1
    m.ST[market]=m.V55State(stage=3,t_sec=0,t_price=90,cycle_id=1)
    m.RESERVE[market]=m.V55State(stage=3,t_sec=8_000,t_price=95,cycle_id=2)
    m.evaluate_market(market,14_400)
    assert m.ST[market].cycle_id==2;assert m.ST[market].t_sec==8_000


def test_old_stage5_hands_over_only_to_stage5_reserve(tmp_path):
    m,_=load_module(tmp_path);market='KRW-X';m.v54.TICK_SIZE[market]=1
    m.ST[market]=m.V55State(stage=5,t_sec=0,t_price=90,stage5_sec=100,stage5_price=100,stage5_last_trade_sec=14_400,cycle_id=1)
    m.RESERVE[market]=m.V55State(stage=3,t_sec=8_000,t_price=95,cycle_id=2)
    m.v54.TRADE_EVENTS[market]=deque([(14_400_000,100,'BID',10)])
    m.evaluate_market(market,14_400)
    assert m.ST[market].cycle_id==1
    m.RESERVE[market]=m.V55State(stage=5,t_sec=8_000,t_price=95,stage5_sec=14_399,stage5_price=100,stage5_last_trade_sec=14_399,cycle_id=2)
    m.evaluate_market(market,14_401)
    assert m.ST[market].cycle_id==2


def test_btc_and_stablecoin_excluded(tmp_path):
    m,_=load_module(tmp_path)
    assert m.excluded('KRW-BTC');assert m.excluded('KRW-USDT');assert not m.excluded('KRW-DRV')


def test_n01_4m_and_bid68_boundaries(tmp_path):
    m,_=load_module(tmp_path)
    def flow(total,bid_share):
        bid=total*bid_share
        ask=total-bid
        return {'cur':{'bid':bid,'ask':ask,'net':bid-ask,'bid_count':10,'ask_count':10,'share':bid_share},
                'prev':{'bid':0.0,'ask':0.0,'bid_count':0,'ask_count':0}}
    assert m.n01_should_recycle(flow(4_000_000,0.6799))
    assert not m.n01_should_recycle(flow(4_000_001,0.55))
    assert not m.n01_should_recycle(flow(4_000_000,0.68))
    assert not m.n01_should_recycle(flow(4_451_600,0.5540))
    assert m.n01_should_recycle(flow(2_758_999,0.6368499))
    assert m.n01_should_recycle(flow(2_924_054,0.6491900))


def test_f03_bursty_value_and_absorption_axes(tmp_path):
    m,_=load_module(tmp_path);market='KRW-X'
    m.v54.TRADE_EVENTS[market]=deque((100_000+i*100,100,'BID',1) for i in range(7))
    flow={'cur':{'bid':24_000_000.0,'ask':2_000_000.0,'net':22_000_000.0,
                 'bid_count':70,'ask_count':30,'share':24/26},
          'prev':{'bid':0.0,'ask':0.0,'bid_count':0,'ask_count':0}}
    out=m.f03_absorption_metrics(market,100,flow,{'rise_pct':1.0})
    assert out['f01'];assert out['f03']
    flow['cur'].update(bid=3_000_000.0,ask=1_000_000.0,net=2_000_000.0,
                       bid_count=4,ask_count=1,share=.75)
    out=m.f03_absorption_metrics(market,100,flow,{'rise_pct':.04})
    assert out['f02'];assert out['f03']
