"""
Simulates a full day of cron-job.org triggering live_notifier.py every
2 minutes, using the same synthetic option-price generator as
test_synthetic.py, but feeding it incrementally (as if "now" keeps advancing)
rather than all at once. Also stubs out send_telegram_message so we can see
exactly what alerts would have fired, without needing real credentials.
"""

import os
from datetime import timedelta

import config
import live_notifier
from src.ist_time import ist_datetime
from test_synthetic import FakeClient

SENT_MESSAGES = []


def fake_send_telegram_message(text, parse_mode="Markdown"):
    SENT_MESSAGES.append(text)
    print("---- TELEGRAM ----")
    print(text)
    print("------------------")
    return True


class TruncatingFakeClient(FakeClient):
    """Same synthetic generator, but only returns candles up to `now`, to
    mimic a trading day that's still in progress."""
    def __init__(self, seed, now_epoch_holder):
        super().__init__(seed=seed)
        self.now_epoch_holder = now_epoch_holder

    def get_day_candles(self, symbol, date_str, resolution=None, use_cache=True,
                         persist_cache=True):
        full = super().get_day_candles(symbol, date_str, resolution, use_cache)
        cutoff = self.now_epoch_holder[0]
        return [c for c in full if c["epoch"] <= cutoff]


def main():
    live_notifier.send_telegram_message = fake_send_telegram_message

    if os.path.exists(live_notifier.STATE_PATH):
        os.remove(live_notifier.STATE_PATH)

    date_str = "2026-07-16"
    now_holder = [0]
    client = TruncatingFakeClient(seed=42, now_epoch_holder=now_holder)

    start = ist_datetime(date_str, config.MARKET_OPEN_TIME)
    end = ist_datetime(date_str, config.MARKET_CLOSE_TIME) + timedelta(minutes=2)
    t = start
    while t <= end:
        now_holder[0] = int(t.timestamp())
        print(f"\n=== run at {t.strftime('%H:%M')} ===")
        live_notifier.main(now_override=t, client_override=client)
        t += timedelta(minutes=2)

    print(f"\n\nTotal Telegram messages sent: {len(SENT_MESSAGES)}")
    with open(live_notifier.STATE_PATH) as f:
        print("\nFinal state:")
        print(f.read())


if __name__ == "__main__":
    main()
