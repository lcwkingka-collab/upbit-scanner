#!/usr/bin/env python3
"""V5.5 paper execution engine. This module never sends private Upbit orders."""
from __future__ import annotations

import json
import math
import os
import threading
import time
import urllib.parse
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Optional


INITIAL_EQUITY_KRW = 2_000_000.0
INVESTMENT_RATIO = 0.50
ACCUMULATION_SEC = 2.0
MIN_ORDER_KRW = 5_000.0
DEFAULT_FEE_RATE = 0.0005
ORDER_RATE_PER_SEC = 12


@dataclass
class PaperPosition:
    market: str
    quantity: float
    average_price: float
    gross_spent_krw: float
    fee_krw: float
    target_budget_krw: float
    unspent_budget_krw: float
    attempts: int
    started_at: float
    completed_at: float
    signal_price: float
    status: str


class PaperTrader:
    """One-position paper portfolio with a permanent 50/50 cash/investment rule."""

    def __init__(self, http_json: Callable[[str], object], rest_base: str,
                 state_path: Optional[Path] = None,
                 fee_rate: Optional[float] = None,
                 clock: Callable[[], float] = time.monotonic,
                 sleeper: Callable[[float], None] = time.sleep):
        self.http_json = http_json
        self.rest_base = rest_base.rstrip("/")
        self.state_path = state_path or Path(os.getenv(
            "PAPER_STATE_PATH", "/home/ubuntu/upbit-scanner/data/live/paper_portfolio.json"))
        self.fee_rate = float(fee_rate if fee_rate is not None else os.getenv(
            "PAPER_FEE_RATE", str(DEFAULT_FEE_RATE)))
        self.clock, self.sleeper = clock, sleeper
        self.lock = threading.Lock()
        self.position: Optional[PaperPosition] = None
        self.realized_equity_krw = INITIAL_EQUITY_KRW
        self._load()

    def _load(self) -> None:
        try:
            obj = json.loads(self.state_path.read_text(encoding="utf-8"))
            self.realized_equity_krw = float(obj.get("realized_equity_krw", INITIAL_EQUITY_KRW))
            if obj.get("position"):
                self.position = PaperPosition(**obj["position"])
        except (FileNotFoundError, ValueError, TypeError, json.JSONDecodeError):
            pass

    def _save(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        obj = {"mode": "paper_only", "realized_equity_krw": self.realized_equity_krw,
               "cash_ratio": 0.5, "investment_ratio": 0.5,
               "position": asdict(self.position) if self.position else None}
        tmp = self.state_path.with_suffix(self.state_path.suffix + ".tmp")
        tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.state_path)

    def can_buy(self) -> bool:
        return self.position is None and self.lock.acquire(blocking=False)

    @staticmethod
    def _walk_asks(orderbook: dict, gross_limit: float) -> tuple[float, float]:
        spent = qty = 0.0
        for unit in orderbook.get("orderbook_units", []):
            price, size = float(unit["ask_price"]), float(unit["ask_size"])
            take = min(size, max(0.0, gross_limit - spent) / price)
            spent += take * price
            qty += take
            if gross_limit - spent < 1e-7:
                break
        return spent, qty

    def buy(self, market: str, signal_price: float) -> Optional[PaperPosition]:
        """Consume live asks for at most two seconds; never calls an order endpoint."""
        if not self.can_buy():
            return None
        try:
            return self._buy_locked(market, signal_price)
        finally:
            self.lock.release()

    def _buy_locked(self, market: str, signal_price: float) -> PaperPosition:
        """Execute a paper buy while ``self.lock`` is already held."""
        started = self.clock()
        target_budget = self.realized_equity_krw * INVESTMENT_RATIO
        target_gross = target_budget / (1.0 + self.fee_rate)
        spent = quantity = 0.0
        attempts = 0
        max_attempts = math.ceil(ACCUMULATION_SEC * ORDER_RATE_PER_SEC)
        min_interval = 1.0 / ORDER_RATE_PER_SEC
        while (self.clock() - started < ACCUMULATION_SEC
               and attempts < max_attempts):
            remaining = target_gross - spent
            if remaining < MIN_ORDER_KRW:
                break
            url = f"{self.rest_base}/orderbook?markets={urllib.parse.quote(market)}&count=30"
            rows = self.http_json(url)
            book = rows[0] if isinstance(rows, list) and rows else {}
            fill_spent, fill_qty = self._walk_asks(book, remaining)
            attempts += 1
            spent += fill_spent
            quantity += fill_qty
            if target_gross - spent < MIN_ORDER_KRW:
                break
            elapsed = self.clock() - started
            wait = min_interval - (elapsed - (attempts - 1) * min_interval)
            if wait > 0:
                self.sleeper(wait)
        fee = spent * self.fee_rate
        total_debit = spent + fee
        unspent = max(0.0, target_budget - total_debit)
        status = "filled" if unspent < MIN_ORDER_KRW else "partial_timeout"
        self.position = PaperPosition(
            market=market, quantity=quantity,
            average_price=(spent / quantity if quantity else 0.0),
            gross_spent_krw=spent, fee_krw=fee,
            target_budget_krw=target_budget, unspent_budget_krw=unspent,
            attempts=attempts, started_at=started, completed_at=self.clock(),
            signal_price=float(signal_price), status=status)
        self._save()
        return self.position

    def buy_async(self, market: str, signal_price: float,
                  callback: Optional[Callable[[Optional[PaperPosition]], None]] = None) -> bool:
        if self.position is not None or not self.lock.acquire(blocking=False):
            return False
        def worker() -> None:
            try:
                result = self._buy_locked(market, signal_price)
                if callback:
                    callback(result)
            finally:
                self.lock.release()
        threading.Thread(target=worker, name=f"paper-buy-{market}", daemon=True).start()
        return True
