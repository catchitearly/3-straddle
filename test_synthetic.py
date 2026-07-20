"""
Synthetic smoke test. Builds fake CE/PE 1-min candles for a couple of days
with a known price path, runs it through the real backtest engine (bypassing
the Fyers network call), and generates the dashboard - so we can sanity check
the VWAP math, entry/exit logic, and rendering without live API access.
"""

import math
import random

import config
from src.ist_time import ist_datetime, ist_to_epoch
from src.straddle_backtest import run_day_backtest
from src.dashboard_generator import generate_dashboard


class FakeClient:
    def __init__(self, seed=0):
        self.rng = random.Random(seed)

    def get_day_candles(self, symbol, date_str, resolution=None, use_cache=True):
        start = ist_to_epoch(ist_datetime(date_str, config.MARKET_OPEN_TIME))
        end = ist_to_epoch(ist_datetime(date_str, config.MARKET_CLOSE_TIME))

        if "NIFTY50-INDEX" in symbol:
            # underlying spot: wiggle around 24800
            base = 24800
            candles = []
            for epoch in range(start, end, 60):
                t = (epoch - start) / 60.0
                price = base + 40 * math.sin(t / 20.0) + self.rng.uniform(-5, 5)
                candles.append({
                    "epoch": epoch, "open": price, "high": price + 2,
                    "low": price - 2, "close": price, "volume": 1000,
                })
            return candles

        # option legs: fabricate a combined-straddle-like decay + oscillation
        is_ce = symbol.endswith("CE")
        candles = []
        for epoch in range(start, end, 60):
            t = (epoch - start) / 60.0
            decay = 220 - 0.25 * t          # theta decay through the day
            wiggle = 15 * math.sin(t / 15.0 + (0 if is_ce else 1.5))
            price = max(5.0, decay / 2 + wiggle + self.rng.uniform(-3, 3))
            vol = int(500 + 200 * abs(math.sin(t / 10.0)))
            candles.append({
                "epoch": epoch, "open": price, "high": price + 1,
                "low": price - 1, "close": price, "volume": vol,
            })
        return candles


def main():
    client = FakeClient(seed=42)
    results = []
    for date_str in ["2025-11-04", "2025-11-05"]:
        r = run_day_backtest(date_str, client)
        results.append(r)
        print(date_str, "->", {k: v for k, v in r.items() if k not in ("series",)})

    generate_dashboard(results, "output/index.html")
    print("\nWrote output/index.html")


if __name__ == "__main__":
    main()
