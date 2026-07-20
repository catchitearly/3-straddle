"""
Entry point. Loops over trading days in [BACKTEST_START_DATE, BACKTEST_END_DATE],
runs the straddle-VWAP backtest for each, and writes:
    output/results.json   - raw data consumed by the dashboard generator
    output/index.html     - the Plotly dashboard (tabs per day)
"""

import json
import os
import sys

import config
from src.ist_time import date_range_str, is_weekend
from src.fyers_client import FyersHistoryClient
from src.straddle_backtest import run_day_backtest
from src.dashboard_generator import generate_dashboard


def main(start_date=None, end_date=None):
    start_date = start_date or config.BACKTEST_START_DATE
    end_date = end_date or config.BACKTEST_END_DATE

    client = FyersHistoryClient()

    day_results = []
    for date_str in date_range_str(start_date, end_date):
        if is_weekend(date_str):
            continue
        if date_str in config.NSE_HOLIDAYS:
            continue

        print(f"Running backtest for {date_str} ...")
        try:
            result = run_day_backtest(date_str, client)
        except Exception as e:  # noqa: BLE001 - keep the loop going across bad days
            print(f"  ERROR on {date_str}: {e}")
            continue

        if result is None:
            print(f"  no spot data for {date_str}, skipping (holiday/no session)")
            continue

        if result.get("error"):
            print(f"  {date_str}: {result['error']} (strike {result.get('strike')})")
        else:
            print(
                f"  {date_str}: strike={result['strike']} "
                f"straddles={result['num_straddles_deployed']} "
                f"day_pnl={result['day_pnl']:.2f}"
            )

        day_results.append(result)

    os.makedirs(config.OUTPUT_DIR, exist_ok=True)
    with open(config.RESULTS_JSON, "w") as f:
        json.dump(day_results, f, indent=2)

    generate_dashboard(day_results, config.OUTPUT_HTML)
    print(f"\nWrote {config.RESULTS_JSON} and {config.OUTPUT_HTML}")

    total_pnl = sum(r.get("day_pnl", 0.0) for r in day_results)
    total_straddles = sum(r.get("num_straddles_deployed", 0) for r in day_results)
    print(f"Total PnL across {len(day_results)} days: {total_pnl:.2f} "
          f"({total_straddles} straddles deployed)")


if __name__ == "__main__":
    # allow: python run_backtest.py 2025-11-01 2025-11-30
    args = sys.argv[1:]
    start = args[0] if len(args) > 0 else None
    end = args[1] if len(args) > 1 else None
    main(start, end)
