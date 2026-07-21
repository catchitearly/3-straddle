"""
Synthetic smoke test for the 3-straddle BASKET engine. Fabricates CE/PE 1-min
candles for 3 strikes (ATM-100, ATM, ATM+100) with a known price path, runs it
through the real backtest engine (bypassing the Fyers network call), and
generates the dashboard.
"""

import math
import random

import config
from src.ist_time import ist_datetime, ist_to_epoch, date_range_str, is_weekend
from src.straddle_backtest import run_day_backtest
from src.dashboard_generator import generate_dashboard


class FakeClient:
    def __init__(self, seed=0):
        self.rng = random.Random(seed)

    def get_day_candles(self, symbol, date_str, resolution=None, use_cache=True):
        start = ist_to_epoch(ist_datetime(date_str, config.MARKET_OPEN_TIME))
        end = ist_to_epoch(ist_datetime(date_str, config.MARKET_CLOSE_TIME))

        if "NIFTY50-INDEX" in symbol:
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

        # option leg: figure out which of the known test strikes this symbol
        # is for, so wings price sensibly relative to the ATM straddle
        # (further OTM = cheaper). Symbol strike codes can vary in digit
        # length (expiry code isn't fixed-width) so match against known
        # candidates rather than slicing blindly.
        is_ce = symbol.endswith("CE")
        body = symbol[:-2]  # strip CE/PE
        strike = 24800
        for candidate in (24700, 24800, 24900):
            if body.endswith(str(candidate)):
                strike = candidate
                break
        distance = abs(strike - 24800)

        candles = []
        walk = 0.0
        for epoch in range(start, end, 60):
            t = (epoch - start) / 60.0
            decay = 220 - 0.15 * t - distance * 0.6    # mild theta decay, wings cheaper
            wiggle = 18 * math.sin(t / 12.0 + (0 if is_ce else 1.5))
            walk += self.rng.uniform(-2.5, 2.5)         # random walk so it crosses VWAP
            walk = max(-25.0, min(25.0, walk))
            price = max(3.0, decay / 2 + wiggle + walk)
            vol = int(500 + 200 * abs(math.sin(t / 10.0)))
            candles.append({
                "epoch": epoch, "open": price, "high": price + 1,
                "low": price - 1, "close": price, "volume": vol,
            })
        return candles


def main():
    client = FakeClient(seed=42)
    results = []
    for date_str in date_range_str(config.BACKTEST_START_DATE, config.BACKTEST_END_DATE):
        if is_weekend(date_str):
            continue
        r = run_day_backtest(date_str, client)
        if r is None:
            continue
        results.append(r)
        print(date_str, "->", {k: v for k, v in r.items() if k not in ("series",)})

    generate_dashboard(results, config.OUTPUT_HTML)
    print(f"\nWrote {config.OUTPUT_HTML}")


if __name__ == "__main__":
    main()
