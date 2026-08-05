"""
Replays a REAL trading day through the actual live pipeline - real Fyers
credentials, real REST historical data - so you can verify the whole thing
is wired correctly WITHOUT waiting for tomorrow morning or being limited by
current market hours.

Why this works outside market hours: Fyers' historical REST API returns
whatever candles actually happened on the given date, regardless of what
time you call it. Once the market closes at 15:30, that day's data is
"frozen" - fetching it at 4pm, 8pm, or the next morning returns the exact
same, complete day's candles every time.

IMPORTANT DESIGN NOTE: because Fyers' REST API always returns the FULL day
at once (it has no concept of "as of what time"), simply calling
live_notifier.main() in a loop with different now_override values would just
re-evaluate the same final bar of the day over and over - it would NOT
actually replay the day's progression. To make this a genuine minute-by-
minute replay (so every entry/exit that would have fired throughout the day
actually gets exercised), _ReplayClient below fetches each symbol's full day
ONCE via the real FyersHistoryClient, then truncates what it hands back to
only the candles up to the current simulated "now".

SAFETY: this uses a SEPARATE state file (data/live_state_replay.json), not
your real data/live_state.json - so it will never interfere with tomorrow's
actual live session.

Usage:
    python3 replay_real_day.py                # replays today
    python3 replay_real_day.py 2026-07-21     # replays a specific past date
"""

import os
import sys
from datetime import timedelta

from src.env_loader import load_dotenv_if_present
load_dotenv_if_present()

import config
import live_notifier
from src.ist_time import ist_datetime, IST
from src.fyers_client import FyersHistoryClient
from datetime import datetime as _dt


REPLAY_STATE_PATH = "data/live_state_replay.json"


class _ReplayClient:
    """
    Wraps the real FyersHistoryClient: fetches each symbol's full day ONCE
    (real REST call, real data), then truncates what get_day_candles()
    returns to only bars at/before the current simulated "now" - so looping
    through the day minute by minute actually replays it progressively,
    instead of seeing the whole day at once on every call.
    """
    def __init__(self):
        self._real = FyersHistoryClient()
        self._full_day_cache = {}
        self.now_epoch = 0  # updated by the replay loop before each step

    def get_day_candles(self, symbol, date_str, resolution=None, use_cache=True,
                         persist_cache=True):
        key = (symbol, date_str, resolution)
        if key not in self._full_day_cache:
            self._full_day_cache[key] = self._real.get_day_candles(
                symbol, date_str, resolution=resolution, use_cache=use_cache,
                persist_cache=persist_cache)
        full = self._full_day_cache[key]
        return [c for c in full if c["epoch"] <= self.now_epoch]


def main():
    date_str = sys.argv[1] if len(sys.argv) > 1 else _dt.now(IST).strftime("%Y-%m-%d")
    print(f"Replaying {date_str} through the real live pipeline "
          f"(real Fyers REST data, separate state file).")

    live_notifier.STATE_PATH = REPLAY_STATE_PATH
    if os.path.exists(REPLAY_STATE_PATH):
        os.remove(REPLAY_STATE_PATH)
        print(f"Cleared stale {REPLAY_STATE_PATH} from a previous replay run.")

    client = _ReplayClient()

    t = ist_datetime(date_str, config.STRIKE_FIX_TIME)
    end = ist_datetime(date_str, config.SQUARE_OFF_TIME) + timedelta(minutes=1)

    step_minutes = 1
    while t <= end:
        client.now_epoch = int(t.timestamp())
        print(f"\n--- simulated time {t.strftime('%H:%M')} ---")
        live_notifier.main(now_override=t, client_override=client)
        t += timedelta(minutes=step_minutes)

    print(f"\nReplay of {date_str} complete. Final state written to {REPLAY_STATE_PATH} "
          f"(separate from your real data/live_state.json).")


if __name__ == "__main__":
    main()
