from pathlib import Path

from paper_trader import PaperTrader


class Clock:
    def __init__(self): self.now = 0.0
    def __call__(self): return self.now
    def sleep(self, seconds): self.now += seconds


def book(ask_price=100.0, ask_size=20_000.0):
    return [{"orderbook_units": [{"ask_price": ask_price, "ask_size": ask_size}]}]


def test_initial_budget_is_half_of_two_million(tmp_path: Path):
    clock = Clock()
    trader = PaperTrader(lambda _url: book(), "https://api.upbit.com/v1",
                         tmp_path / "state.json", fee_rate=0.0005,
                         clock=clock, sleeper=clock.sleep)
    position = trader.buy("KRW-X", 100.0)
    assert position is not None
    assert position.target_budget_krw == 1_000_000
    assert position.gross_spent_krw + position.fee_krw <= 1_000_000.01
    assert position.status == "filled"


def test_thin_books_retry_for_two_seconds(tmp_path: Path):
    clock = Clock()
    calls = []
    def thin(_url):
        calls.append(1)
        return book(100.0, 100.0)  # 10,000 KRW per synthetic attempt
    trader = PaperTrader(thin, "https://api.upbit.com/v1", tmp_path / "state.json",
                         fee_rate=0.0, clock=clock, sleeper=clock.sleep)
    position = trader.buy("KRW-X", 100.0)
    assert position is not None
    assert 20 <= position.attempts <= 24
    assert position.status == "partial_timeout"
    assert position.gross_spent_krw == position.attempts * 10_000


def test_existing_position_blocks_second_signal(tmp_path: Path):
    clock = Clock()
    trader = PaperTrader(lambda _url: book(), "https://api.upbit.com/v1",
                         tmp_path / "state.json", fee_rate=0.0,
                         clock=clock, sleeper=clock.sleep)
    assert trader.buy("KRW-A", 100.0) is not None
    assert trader.buy("KRW-B", 100.0) is None
