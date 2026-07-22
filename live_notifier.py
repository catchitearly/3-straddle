"""
Live notifier (cloud) - meant to be triggered every ~2 minutes during market
hours by an external cron (cron-job.org calling GitHub's repository_dispatch
API, which fires .github/workflows/live_notifier.yml).

All entry/exit/Telegram decision logic lives in src/live_engine.py - this
file's only job is: fetch the latest data via Fyers' REST history API, build
a single bar (price/vwap/atr), and hand it to the shared engine. That way
this cloud runner and live_notifier_local.py (the websocket-driven version
for running on your own machine) can never quietly drift apart in behavior.

Each run:
  1. Loads today's state from data/live_state.json (resets if it's a new day).
  2. Fixes the strike (once) if it's past 09:45 and not fixed yet.
  3. If the strike is fixed and we're not squared off yet, fetches the latest
     bar and hands it to src/live_engine.py for the actual entry/exit logic.
  4. Saves state.json. The calling workflow git-commits it back to the repo
     so state survives between ephemeral runner boots.
"""

import json
import os
from datetime import datetime

from src.env_loader import load_dotenv_if_present
load_dotenv_if_present()

import config
from src.ist_time import IST, ist_datetime, ist_to_epoch, is_weekend
from src.vwap import compute_cumulative_vwap
from src.straddle_backtest import merge_basket_series, compute_basket_atr
from src.fyers_client import FyersHistoryClient
from src.live_engine import build_strike_plan, announce_strike_fixed, evaluate_bar, finalize_squareoff

STATE_PATH = "data/live_state.json"


def _now_ist():
    return datetime.now(IST)


def _empty_state(date_str):
    return {
        "date": date_str,
        "strike_fixed": False,
        "atm_strike": None,
        "strikes": None,
        "expiry": None,
        "leg_symbols": None,
        "was_below_vwap": False,
        "first_check_done": False,
        "baskets_deployed": 0,
        "entries": [],
        "squared_off": False,
        "last_price": None,
        "last_vwap": None,
        "last_updated": None,
    }


def load_state():
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH, "r") as f:
            return json.load(f)
    return None


def save_state(state):
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    with open(STATE_PATH, "w") as f:
        json.dump(state, f, indent=2)


def fix_strike(state, date_str, client):
    spot_candles = client.get_day_candles(
        config.UNDERLYING_SYMBOL, date_str, persist_cache=False)
    if not spot_candles:
        print("No spot candles yet - can't fix strike this run, will retry next run.")
        return state

    spot_close = spot_candles[-1]["close"]
    atm_strike, strikes, leg_symbols, expiry_date = build_strike_plan(spot_close, date_str)

    state.update({
        "strike_fixed": True,
        "atm_strike": atm_strike,
        "strikes": strikes,
        "expiry": expiry_date,
        "leg_symbols": leg_symbols,
    })

    announce_strike_fixed(date_str, spot_close, atm_strike, strikes, expiry_date)
    return state


def _fetch_merged_series(state, date_str, client):
    """Fetch all 6 legs (live, no cache-write) and merge into the combined
    basket series with VWAP + ATR computed, same as the backtest engine."""
    leg_candles = {}
    for sym in state["leg_symbols"]:
        candles = client.get_day_candles(sym, date_str, persist_cache=False)
        if not candles:
            return None
        leg_candles[sym] = candles

    merged = merge_basket_series(leg_candles)
    if not merged:
        return None

    vwap_values = compute_cumulative_vwap(merged)
    atr_values = compute_basket_atr(merged, period=config.ATR_PERIOD)
    for bar, vwap, atr in zip(merged, vwap_values, atr_values):
        bar["vwap"] = vwap
        bar["atr"] = atr

    return merged


def check_market(state, date_str, client):
    """Fetch the latest bar via REST and hand it to the shared engine."""
    signal_epoch = ist_to_epoch(ist_datetime(date_str, config.SIGNAL_START_TIME))

    merged = _fetch_merged_series(state, date_str, client)
    if not merged:
        print("No data yet this run - skipping.")
        return state

    relevant = [b for b in merged if b["epoch"] >= signal_epoch]
    if not relevant:
        return state

    return evaluate_bar(state, date_str, relevant[-1])


def square_off(state, date_str, client):
    merged = _fetch_merged_series(state, date_str, client)
    exit_price = merged[-1]["price"] if merged else None
    return finalize_squareoff(state, date_str, exit_price)


def main(now_override=None, client_override=None):
    now = now_override or _now_ist()
    date_str = now.strftime("%Y-%m-%d")
    time_str = now.strftime("%H:%M")

    if is_weekend(date_str) or date_str in config.NSE_HOLIDAYS:
        print(f"{date_str} is a weekend/holiday - nothing to do.")
        return

    state = load_state()
    if state is None or state.get("date") != date_str:
        state = _empty_state(date_str)
        print(f"New day - state reset for {date_str}")

    if state["squared_off"]:
        print(f"{date_str} already squared off - nothing to do.")
        save_state(state)
        return

    if time_str < config.MARKET_OPEN_TIME:
        print(f"Before market open ({time_str} < {config.MARKET_OPEN_TIME}) - skipping.")
        save_state(state)
        return

    client = client_override or FyersHistoryClient()

    if not state["strike_fixed"]:
        if time_str >= config.STRIKE_FIX_TIME:
            state = fix_strike(state, date_str, client)
        else:
            print(f"Before strike-fix time ({time_str} < {config.STRIKE_FIX_TIME}) - skipping.")
        save_state(state)
        return

    if time_str >= config.SQUARE_OFF_TIME:
        state = square_off(state, date_str, client)
    elif time_str >= config.SIGNAL_START_TIME:
        state = check_market(state, date_str, client)
    else:
        print("Strike fixed, but before signal-start time - skipping.")

    save_state(state)


if __name__ == "__main__":
    main()
