import asyncio
import importlib
import sys
import types
from collections import deque


def load_module():
    sent = []
    fake_r = types.SimpleNamespace(send_telegram=lambda msg: _send(sent, msg))
    fake_v54 = types.ModuleType("realtime_radar_v54")
    fake_v54.r = fake_r
    fake_v54.TRADE_EVENTS = {}
    fake_v54.TICK_SIZE = {}
    fake_v54.krw_tick_size = lambda _price: 1.0
    fake_v54.minute_value_x = lambda _market, _sec: (3.0, 5_000_000, 1_500_000)
    sys.modules["realtime_radar_v54"] = fake_v54
    sys.modules.pop("realtime_radar_v55", None)
    mod = importlib.import_module("realtime_radar_v55")
    return mod, sent


async def _send(sent, msg):
    sent.append(msg)


def add_flow(mod, market, start_sec, seconds, price=100, qty=2000, side="BID"):
    q = mod.v54.TRADE_EVENTS.setdefault(market, deque())
    for i in range(seconds):
        q.append(((start_sec + i) * 1000, price, qty, side))


def test_stage4_and_stage5_are_at_least_ten_seconds_apart():
    mod, sent = load_module()
    market = "KRW-DRV"
    mod.v54.TICK_SIZE[market] = 1
    q = mod.v54.TRADE_EVENTS.setdefault(market, deque())
    # Entry price and pre/post-entry persistent buy flow.
    q.append((100_000, 100, 20_000, "BID"))
    add_flow(mod, market, 101, 10, qty=2_000)
    asyncio.run(mod.evaluate_once(market, 100))
    asyncio.run(mod.evaluate_once(market, 110))
    assert mod.ST[market].stage == 3

    # Four BID trades ending four ticks higher, with no wick giveback.
    for ms, price in [(111_000, 100), (112_000, 101), (112_500, 102),
                      (113_000, 104), (113_500, 104)]:
        q.append((ms, price, 3_000, "BID"))
    asyncio.run(mod.evaluate_once(market, 113))
    assert mod.ST[market].stage == 4
    assert len(sent) == 1

    asyncio.run(mod.evaluate_once(market, 122))
    assert len(sent) == 1
    add_flow(mod, market, 114, 10, price=104, qty=3_000)
    asyncio.run(mod.evaluate_once(market, 123))
    assert mod.ST[market].stage == 5
    assert len(sent) == 2


def test_stale_wait_candidate_is_reset():
    mod, _sent = load_module()
    market = "KRW-FOLD"
    mod.ST[market] = mod.V55State(stage=3, confirm_sec=10)
    asyncio.run(mod.evaluate_once(market, 101))
    assert mod.ST[market].stage == 0


def test_single_trade_and_wick_do_not_launch():
    mod, _sent = load_module()
    market = "KRW-FAKE"
    mod.v54.TICK_SIZE[market] = 1
    mod.v54.TRADE_EVENTS[market] = deque([
        (10_000, 100, 10_000, "BID"),
        (11_000, 106, 10_000, "BID"),
        (12_000, 101, 10_000, "ASK"),
    ])
    ok, *_ = mod.launch_current(market, 12)
    assert not ok


def test_stablecoin_is_ignored():
    mod, _sent = load_module()
    asyncio.run(mod.evaluate_once("KRW-EURC", 100))
    assert "KRW-EURC" in mod.ST
    assert mod.ST["KRW-EURC"].stage == 0
