"""
Simulates a full day of live_notifier_local.py running against the Fyers
WebSocket, WITHOUT a real connection, real sleeps, or real wall-clock time.

A FakeClock stands in for `now_func`/`sleep_func` (advancing instantly
instead of actually sleeping), and a FakeWSClient stands in for
FyersWSClient, revealing the same synthetic candle data used elsewhere
(test_synthetic.FakeClient's generator) progressively as the fake clock
advances - so get_candles() only returns minutes that have "elapsed" so far,
exactly like the real websocket client would.
"""

import os
from datetime import timedelta

import config
import live_notifier
import live_notifier_local
import src.live_engine as live_engine
from src.ist_time import ist_datetime
from test_synthetic import FakeClient

SENT_MESSAGES = []


def fake_send_telegram_message(text, parse_mode="Markdown"):
    SENT_MESSAGES.append(text)
    print("---- TELEGRAM ----")
    print(text)
    print("------------------")
    return True


class FakeClock:
    def __init__(self, start_dt):
        self.now = start_dt

    def advance(self, seconds):
        self.now = self.now + timedelta(seconds=seconds)


class FakeWSClient:
    """Stand-in for FyersWSClient: reveals the synthetic day's candles
    progressively as the fake clock advances, instead of aggregating real
    ticks."""
    def __init__(self, clock, seed=42):
        self.clock = clock
        self.gen = FakeClient(seed=seed)
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

    def _minute_epoch(self):
        return (int(self.clock.now.timestamp()) // 60) * 60

    def get_candles(self, symbol):
        minute_epoch = self._minute_epoch()
        return [c for c in self._full_day(symbol) if c["epoch"] < minute_epoch]

    def get_latest_ltp(self, symbol):
        minute_epoch = self._minute_epoch()
        candles = self._full_day(symbol)
        for c in candles:
            if c["epoch"] == minute_epoch:
                return c["close"]
        completed = [c for c in candles if c["epoch"] < minute_epoch]
        return completed[-1]["close"] if completed else None

    def flush_current_bar(self, symbol):
        pass  # no-op - get_candles/get_latest_ltp are already clock-driven


def main():
    live_engine.send_telegram_message = fake_send_telegram_message

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
    with open(live_notifier.STATE_PATH) as f:
        print("\nFinal state:")
        print(f.read())


if __name__ == "__main__":
    main()
