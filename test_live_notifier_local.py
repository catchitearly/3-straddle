"""
Simulates a full day of live_notifier_local.py running against the Fyers
WebSocket, WITHOUT a real connection, real sleeps, or real wall-clock time.

A FakeClock stands in for `now_func`/`sleep_func` (advancing instantly
instead of actually sleeping), and a FakeWSClient stands in for
FyersWSClient, revealing synthetic candle data at config.LIVE_BAR_SECONDS
resolution (5 seconds by default) progressively as the fake clock advances -
so get_candles() only returns bars that have "elapsed" so far, exactly like
the real websocket client would.

This uses its OWN synthetic generator (Fake5SecClient below), not
test_synthetic.FakeClient - that one generates 1-minute bars for the batch
backtest and cloud/REST tests, which now run at a different resolution than
the live path.

Now exercises THREE basket sets (config.BASKET_SETS), and the periodic
dashboard-file send (config.DASHBOARD_SEND_INTERVAL_SECONDS).
"""

import math
import os
import random
from datetime import timedelta

import config
import live_notifier
import live_notifier_local
import src.live_engine as live_engine
from src.ist_time import ist_datetime

SENT_MESSAGES = []
SENT_DOCUMENTS = []


def fake_send_telegram_message(text, parse_mode="Markdown"):
    SENT_MESSAGES.append(text)
    print("---- TELEGRAM ----")
    print(text)
    print("------------------")
    return True


def fake_send_telegram_document(file_path, caption=None):
    SENT_DOCUMENTS.append((file_path, caption))
    print("---- TELEGRAM DOCUMENT ----", caption)
    return True


class FakeClock:
    def __init__(self, start_dt):
        self.now = start_dt

    def advance(self, seconds):
        self.now = self.now + timedelta(seconds=seconds)


class Fake5SecClient:
    """Generates a synthetic day's candles at config.LIVE_BAR_SECONDS
    resolution - same shape of price path as test_synthetic.FakeClient
    (decay + oscillating random walk), just stepped finer."""
    def __init__(self, seed=0):
        self.rng = random.Random(seed)
        step_ratio = 60.0 / config.LIVE_BAR_SECONDS   # e.g. 12 steps/minute at 5s bars
        # scale the per-step random walk increment down so ~1 minute of
        # accumulated noise is comparable to the 1-minute generator's single
        # per-minute step, not step_ratio times noisier
        self._walk_step = 2.5 / math.sqrt(step_ratio)

    def get_day_candles(self, symbol, date_str, resolution=None, use_cache=True,
                         persist_cache=True):
        start = int(ist_datetime(date_str, config.MARKET_OPEN_TIME).timestamp())
        end = int(ist_datetime(date_str, config.MARKET_CLOSE_TIME).timestamp())
        step = config.LIVE_BAR_SECONDS

        if "NIFTY50-INDEX" in symbol:
            base = 24800
            candles = []
            for epoch in range(start, end, step):
                t = (epoch - start) / 60.0
                price = base + 40 * math.sin(t / 20.0) + self.rng.uniform(-5, 5)
                candles.append({
                    "epoch": epoch, "open": price, "high": price + 2,
                    "low": price - 2, "close": price, "volume": max(1, int(1000 / (60 / step))),
                })
            return candles

        is_ce = symbol.endswith("CE")
        body = symbol[:-2]
        strike = 24800
        for candidate in (24600, 24700, 24800, 24900, 25000):
            if body.endswith(str(candidate)):
                strike = candidate
                break
        distance = abs(strike - 24800)

        candles = []
        walk = 0.0
        for epoch in range(start, end, step):
            t = (epoch - start) / 60.0
            decay = 220 - 0.15 * t - distance * 0.6
            wiggle = 18 * math.sin(t / 12.0 + (0 if is_ce else 1.5))
            walk += self.rng.uniform(-self._walk_step, self._walk_step)
            walk = max(-25.0, min(25.0, walk))
            price = max(3.0, decay / 2 + wiggle + walk)
            vol = max(1, int((500 + 200 * abs(math.sin(t / 10.0))) / (60 / step)))
            candles.append({
                "epoch": epoch, "open": price, "high": price + 1,
                "low": price - 1, "close": price, "volume": vol,
            })
        return candles


class FakeWSClient:
    """Stand-in for FyersWSClient: reveals the synthetic day's candles
    progressively as the fake clock advances, instead of aggregating real
    ticks."""
    def __init__(self, clock, seed=42):
        self.clock = clock
        self.gen = Fake5SecClient(seed=seed)
        self._full_day_cache = {}
        self.subscribed = set()
        self.connect_calls = []

    def connect(self, initial_symbols=None, timeout=15):
        self.subscribed = set(initial_symbols or [])
        self.connect_calls.append(self.clock.now.strftime("%H:%M"))

    def is_connected(self):
        return True

    def subscribe(self, symbols):
        self.subscribed |= set(symbols)

    def unsubscribe(self, symbols):
        self.subscribed -= set(symbols)

    def _full_day(self, symbol):
        if symbol not in self._full_day_cache:
            date_str = self.clock.now.strftime("%Y-%m-%d")
            self._full_day_cache[symbol] = self.gen.get_day_candles(symbol, date_str)
        return self._full_day_cache[symbol]

    def _bar_epoch(self):
        step = config.LIVE_BAR_SECONDS
        return (int(self.clock.now.timestamp()) // step) * step

    def get_candles(self, symbol):
        bar_epoch = self._bar_epoch()
        return [c for c in self._full_day(symbol) if c["epoch"] < bar_epoch]

    def get_latest_ltp(self, symbol):
        bar_epoch = self._bar_epoch()
        candles = self._full_day(symbol)
        for c in candles:
            if c["epoch"] == bar_epoch:
                return c["close"]
        completed = [c for c in candles if c["epoch"] < bar_epoch]
        return completed[-1]["close"] if completed else None

    def flush_current_bar(self, symbol):
        pass  # no-op - get_candles/get_latest_ltp are already clock-driven


def main():
    live_engine.send_telegram_message = fake_send_telegram_message
    live_notifier_local.send_telegram_document = fake_send_telegram_document

    if os.path.exists(live_notifier.STATE_PATH):
        os.remove(live_notifier.STATE_PATH)

    date_str = "2026-07-16"
    clock = FakeClock(ist_datetime(date_str, "07:00"))
    ws = FakeWSClient(clock, seed=42)

    end = ist_datetime(date_str, config.MARKET_CLOSE_TIME)

    def now_func():
        return clock.now

    def sleep_func(seconds):
        clock.advance(seconds)
        if clock.now > end:
            raise KeyboardInterrupt  # let run()'s own handler save state and stop cleanly

    live_notifier_local.run(ws_override=ws, now_func=now_func, sleep_func=sleep_func)

    print(f"\nWebSocket connect() was called at: {ws.connect_calls} "
          f"(should be exactly one call, at/after {config.MARKET_OPEN_TIME})")
    print(f"\n\nTotal Telegram messages sent: {len(SENT_MESSAGES)}")
    print(f"Total Telegram documents sent: {len(SENT_DOCUMENTS)}")
    with open(live_notifier.STATE_PATH) as f:
        print("\nFinal state:")
        print(f.read())


if __name__ == "__main__":
    main()
