"""
Live notifier - meant to be triggered every ~2 minutes during market hours by
an external cron (cron-job.org calling GitHub's repository_dispatch API,
which fires the .github/workflows/live_notifier.yml workflow).

Mirrors src/straddle_backtest.py's exit logic exactly, evaluated
incrementally instead of over a full day's data at once:
  - First bar at/after 09:45: enter if price < VWAP (regardless of what
    happened before 09:45).
  - Fresh downward VWAP crossings after that -> deploy another basket.
  - Each open basket exits independently, whichever happens first:
      * price crosses back above VWAP ("Price > VWAP Exit"), or
      * price rallies back up through its ATR trailing stop
        ("Trailing Stop Hit"), or
      * 15:15 IST time exit (whatever's still open gets closed together).

Each run:
  1. Loads today's state from data/live_state.json (resets if it's a new day).
  2. Fixes the strike (once) if it's past 09:45 and not fixed yet.
  3. If the strike is fixed and we're not squared off yet, evaluates the
     latest available bar for exits (on open baskets) and entries.
  4. Saves state.json. The calling workflow git-commits it back to the repo
     so state survives between ephemeral runner boots.
"""

import json
import os
from datetime import datetime

import config
from src.ist_time import IST, ist_datetime, ist_to_epoch, is_weekend
from src.vwap import compute_cumulative_vwap
from src.expiry_utils import round_to_nearest_strike, build_option_symbol
from src.straddle_backtest import merge_basket_series, compute_basket_atr
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
        "first_check_done": False,
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
        f"Watching for entries from now..."
    )
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
    """One incremental step of the same logic as straddle_backtest.run_day_backtest's
    main loop, evaluated against just the latest available bar."""
    lot_size = config.get_lot_size(date_str)
    qty_per_leg = config.LOTS_PER_LEG_PER_BASKET * lot_size
    atr_multiplier = config.ATR_MULTIPLIER

    signal_epoch = ist_to_epoch(ist_datetime(date_str, config.SIGNAL_START_TIME))

    merged = _fetch_merged_series(state, date_str, client)
    if not merged:
        print("No data yet this run - skipping.")
        return state

    relevant = [b for b in merged if b["epoch"] >= signal_epoch]
    if not relevant:
        return state

    bar = relevant[-1]
    price, vwap, atr = bar["price"], bar["vwap"], bar["atr"]
    current_epoch = bar["epoch"]
    entry_time = datetime.fromtimestamp(current_epoch, tz=IST).strftime("%H:%M")

    is_below = price < vwap
    is_above = price > vwap

    # --- Check exits on currently open baskets ------------------------------
    for e in state["entries"]:
        if e["exit_price"] is not None:
            continue  # already closed

        if price < e["min_price"]:
            e["min_price"] = price
            e["trailing_stop"] = e["min_price"] + (atr_multiplier * atr)

        exit_reason = None
        if is_above:
            exit_reason = "Price > VWAP Exit"
        elif price >= e["trailing_stop"]:
            exit_reason = f"Trailing Stop Hit ({atr_multiplier}x ATR)"

        if exit_reason:
            pnl = (e["entry_price"] - price) * qty_per_leg
            e["exit_price"] = price
            e["exit_time"] = entry_time
            e["exit_reason"] = exit_reason
            e["pnl"] = pnl
            send_telegram_message(
                f"*EXIT basket #{e['basket_num']}* — {date_str} {entry_time}\n"
                f"Reason: {exit_reason}\n"
                f"Entry {e['entry_price']:.2f} @ {e['entry_time']} -> Exit {price:.2f}\n"
                f"PnL: {'+' if pnl >= 0 else ''}{pnl:.2f}"
            )

    # --- Entry logic ----------------------------------------------------------
    at_cap = (config.MAX_BASKETS_PER_DAY is not None
              and state["baskets_deployed"] >= config.MAX_BASKETS_PER_DAY)
    should_enter = False

    if not state["first_check_done"]:
        state["first_check_done"] = True
        if is_below and not at_cap:
            should_enter = True
    elif is_below and not state["was_below_vwap"] and not at_cap:
        should_enter = True

    if should_enter:
        state["baskets_deployed"] += 1
        state["entries"].append({
            "basket_num": state["baskets_deployed"],
            "entry_epoch": current_epoch,
            "entry_time": entry_time,
            "entry_price": price,
            "min_price": price,
            "trailing_stop": price + (atr_multiplier * atr),
            "exit_price": None,
            "exit_time": None,
            "exit_reason": None,
            "pnl": None,
        })
        send_telegram_message(
            f"*SELL basket #{state['baskets_deployed']}* — {date_str} {entry_time}\n"
            f"Strikes {state['strikes'][0]}/{state['strikes'][1]}/{state['strikes'][2]} "
            f"(expiry {state['expiry']})\n"
            f"Combined price: {price:.2f} (VWAP {vwap:.2f})\n"
            f"Qty per leg: {qty_per_leg} ({config.LOTS_PER_LEG_PER_BASKET} lot x {lot_size})"
        )
    else:
        print(f"No new entry. price={price:.2f} vwap={vwap:.2f} below={is_below}")

    state["was_below_vwap"] = is_below
    return state


def square_off(state, date_str, client):
    lot_size = config.get_lot_size(date_str)
    qty_per_leg = config.LOTS_PER_LEG_PER_BASKET * lot_size

    open_entries = [e for e in state["entries"] if e["exit_price"] is None]
    if not open_entries:
        print("Nothing open at square-off time.")
        state["squared_off"] = True
        return state

    merged = _fetch_merged_series(state, date_str, client)
    exit_price = merged[-1]["price"] if merged else None
    exit_time = config.SQUARE_OFF_TIME

    if exit_price is None:
        print("Could not fetch an exit price this run - will retry next run.")
        return state  # don't mark squared_off, retry next run

    day_pnl = 0.0
    lines = [f"*SQUARE OFF* — {date_str} {exit_time}"]
    for e in open_entries:
        pnl = (e["entry_price"] - exit_price) * qty_per_leg
        e["exit_price"] = exit_price
        e["exit_time"] = exit_time
        e["exit_reason"] = "15:15 Time Exit"
        e["pnl"] = pnl
        day_pnl += pnl
        lines.append(f"#{e['basket_num']}: entry {e['entry_price']:.2f} @ {e['entry_time']} "
                     f"-> exit {exit_price:.2f} = {'+' if pnl >= 0 else ''}{pnl:.2f}")

    total_day_pnl = sum(e["pnl"] for e in state["entries"] if e["pnl"] is not None)
    lines.append(f"\n*Day total: {'+' if total_day_pnl >= 0 else ''}{total_day_pnl:.2f}* "
                 f"across {state['baskets_deployed']} basket(s)")
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
        state = check_market(state, date_str, client)
    else:
        print("Strike fixed, but before signal-start time - skipping.")

    save_state(state)


if __name__ == "__main__":
    main()
