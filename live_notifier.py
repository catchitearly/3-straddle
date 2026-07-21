"""
Live notifier - meant to be triggered every ~2 minutes during market hours by
an external cron (cron-job.org calling GitHub's repository_dispatch API,
which fires the .github/workflows/live_notifier.yml workflow).

Each run:
  1. Loads today's state from data/live_state.json (resets if it's a new day).
  2. If it's past 09:45 and the strike isn't fixed yet for today, fixes it
     (ATM-100 / ATM / ATM+100) and saves state.
  3. If the strike is fixed and we're not squared off yet:
       - past 15:15 -> square off all open baskets, send a summary Telegram
         message, mark the day done.
       - else -> fetch the combined 6-leg price/VWAP series so far today,
         check for a FRESH downward cross -> sell another basket, Telegram
         alert.
  4. Saves state.json. The calling workflow is responsible for git-committing
     it back to the repo (state must survive between ephemeral runner boots).

This intentionally reuses the exact same crossing/PnL logic as the backtest
(src/straddle_backtest.py) is built on, just evaluated incrementally.
"""

import json
import os
from datetime import datetime

import config
from src.ist_time import IST, ist_datetime, ist_to_epoch, is_weekend
from src.vwap import compute_cumulative_vwap
from src.expiry_utils import round_to_nearest_strike, build_option_symbol
from src.straddle_backtest import merge_basket_series
from src.fyers_client import FyersHistoryClient
from src.telegram_notifier import send_telegram_message

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
        "baskets_deployed": 0,
        "entries": [],
        "squared_off": False,
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
    atm_strike = round_to_nearest_strike(spot_close)
    strikes = sorted(atm_strike + off for off in config.STRIKE_OFFSETS)

    leg_symbols = []
    expiry_date = None
    for strike in strikes:
        for opt_type in ("CE", "PE"):
            sym, expiry_date = build_option_symbol(date_str, strike, opt_type)
            leg_symbols.append(sym)

    state.update({
        "strike_fixed": True,
        "atm_strike": atm_strike,
        "strikes": strikes,
        "expiry": expiry_date,
        "leg_symbols": leg_symbols,
    })

    send_telegram_message(
        f"*Straddle basket fixed* — {date_str}\n"
        f"Spot: {spot_close:.1f} -> ATM {atm_strike}\n"
        f"Strikes: {strikes[0]} / {strikes[1]} / {strikes[2]}\n"
        f"Expiry: {expiry_date}\n"
        f"Watching for a downward VWAP cross from now..."
    )
    return state


def check_for_entry(state, date_str, client):
    lot_size = config.get_lot_size(date_str)
    qty_per_leg = config.LOTS_PER_LEG_PER_BASKET * lot_size

    leg_candles = {}
    for sym in state["leg_symbols"]:
        candles = client.get_day_candles(sym, date_str, persist_cache=False)
        if not candles:
            print(f"No data yet for {sym} this run - skipping entry check.")
            return state
        leg_candles[sym] = candles

    merged = merge_basket_series(leg_candles)
    if not merged:
        print("No overlapping bars across all 6 legs yet - skipping.")
        return state

    vwap_values = compute_cumulative_vwap(merged)
    for bar, vwap in zip(merged, vwap_values):
        bar["vwap"] = vwap

    signal_epoch = ist_to_epoch(ist_datetime(date_str, config.SIGNAL_START_TIME))
    relevant = [b for b in merged if b["epoch"] >= signal_epoch]
    if not relevant:
        return state

    last = relevant[-1]
    is_below = last["price"] < last["vwap"]
    fresh_cross_down = is_below and not state["was_below_vwap"]

    at_cap = (config.MAX_BASKETS_PER_DAY is not None
              and state["baskets_deployed"] >= config.MAX_BASKETS_PER_DAY)

    if fresh_cross_down and not at_cap:
        state["baskets_deployed"] += 1
        entry_time = datetime.fromtimestamp(last["epoch"], tz=IST).strftime("%H:%M")
        state["entries"].append({
            "basket_num": state["baskets_deployed"],
            "entry_epoch": last["epoch"],
            "entry_time": entry_time,
            "entry_price": last["price"],
            "exit_price": None,
            "exit_time": None,
            "pnl": None,
        })
        send_telegram_message(
            f"*SELL basket #{state['baskets_deployed']}* — {date_str} {entry_time}\n"
            f"Strikes {state['strikes'][0]}/{state['strikes'][1]}/{state['strikes'][2]} "
            f"(expiry {state['expiry']})\n"
            f"Combined price: {last['price']:.2f} (VWAP {last['vwap']:.2f})\n"
            f"Qty per leg: {qty_per_leg} ({config.LOTS_PER_LEG_PER_BASKET} lot x {lot_size})"
        )
    else:
        print(f"No fresh cross. price={last['price']:.2f} vwap={last['vwap']:.2f} "
              f"below={is_below}")

    state["was_below_vwap"] = is_below
    return state


def square_off(state, date_str, client):
    lot_size = config.get_lot_size(date_str)
    qty_per_leg = config.LOTS_PER_LEG_PER_BASKET * lot_size

    leg_candles = {}
    for sym in state["leg_symbols"]:
        candles = client.get_day_candles(sym, date_str, persist_cache=False)
        leg_candles[sym] = candles or []

    merged = merge_basket_series(leg_candles)
    exit_price = merged[-1]["price"] if merged else None
    exit_time = config.SQUARE_OFF_TIME

    day_pnl = 0.0
    lines = [f"*SQUARE OFF* — {date_str} {exit_time}"]
    if exit_price is None:
        lines.append("Could not fetch an exit price this run - will retry next run.")
        send_telegram_message("\n".join(lines))
        return state  # don't mark squared_off, retry next run

    open_entries = [e for e in state["entries"] if e["exit_price"] is None]
    if not open_entries:
        lines.append("No open baskets today - nothing to close.")
    for e in open_entries:
        pnl = (e["entry_price"] - exit_price) * qty_per_leg
        e["exit_price"] = exit_price
        e["exit_time"] = exit_time
        e["pnl"] = pnl
        day_pnl += pnl
        lines.append(f"#{e['basket_num']}: entry {e['entry_price']:.2f} @ {e['entry_time']} "
                     f"-> exit {exit_price:.2f} = {'+' if pnl >= 0 else ''}{pnl:.2f}")

    lines.append(f"\n*Day total: {'+' if day_pnl >= 0 else ''}{day_pnl:.2f}* "
                 f"across {len(open_entries)} basket(s)")
    send_telegram_message("\n".join(lines))

    state["squared_off"] = True
    return state


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
        state = check_for_entry(state, date_str, client)
    else:
        print(f"Strike fixed, but before signal-start time - skipping.")

    save_state(state)


if __name__ == "__main__":
    main()
