"""
Shared entry/exit/Telegram decision logic - the single source of truth used
by BOTH:
  - live_notifier.py (cloud, REST-polling every ~2 min via GitHub Actions)
  - live_notifier_local.py (local, websocket-driven, ~1 min bars)

Whichever one is fetching/building the data, it hands a single already-
computed bar ({"epoch","price","vwap","atr"}) to evaluate_bar() here, so the
actual strategy behavior can never quietly drift between the two runners.
"""

from datetime import datetime

import config
from src.ist_time import IST
from src.expiry_utils import round_to_nearest_strike, build_option_symbol
from src.telegram_notifier import send_telegram_message


def build_strike_plan(spot_price, date_str):
    """Given a spot price and trade date, return (atm_strike, strikes,
    leg_symbols, expiry_date) - the same strike-fixing logic used everywhere
    else in this project."""
    atm_strike = round_to_nearest_strike(spot_price)
    strikes = sorted(atm_strike + off for off in config.STRIKE_OFFSETS)

    leg_symbols = []
    expiry_date = None
    for strike in strikes:
        for opt_type in ("CE", "PE"):
            sym, expiry_date = build_option_symbol(date_str, strike, opt_type)
            leg_symbols.append(sym)

    return atm_strike, strikes, leg_symbols, expiry_date


def announce_strike_fixed(date_str, spot_price, atm_strike, strikes, expiry_date):
    send_telegram_message(
        f"*GH Straddle basket fixed* — {date_str}\n"
        f"Spot: {spot_price:.1f} -> ATM {atm_strike}\n"
        f"Strikes: {strikes[0]} / {strikes[1]} / {strikes[2]}\n"
        f"Expiry: {expiry_date}\n"
        f"Watching for entries from now..."
    )


def evaluate_bar(state, date_str, bar):
    """
    Core entry/exit decision for ONE already-computed bar. Mutates and
    returns `state`. Sends a Telegram alert on every entry and every exit.

    Entry: the first bar ever evaluated enters if price < VWAP; after that,
    every fresh downward VWAP crossing deploys another basket (capped by
    config.MAX_BASKETS_PER_DAY if set).

    Exit (checked per open basket, independently): price crosses back above
    VWAP, OR price rallies back through its ATR trailing stop. (The 15:15
    time exit is handled separately by finalize_squareoff().)
    """
    lot_size = config.get_lot_size(date_str)
    qty_per_leg = config.LOTS_PER_LEG_PER_BASKET * lot_size
    atr_multiplier = config.ATR_MULTIPLIER

    price, vwap, atr = bar["price"], bar["vwap"], bar["atr"]
    current_epoch = bar["epoch"]
    bar_time = datetime.fromtimestamp(current_epoch, tz=IST).strftime("%H:%M")

    is_below = price < vwap
    is_above = price > vwap

    state["last_price"] = price
    state["last_vwap"] = vwap
    state["last_updated"] = bar_time

    # --- Exits on currently open baskets -------------------------------------
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
            e["exit_time"] = bar_time
            e["exit_reason"] = exit_reason
            e["pnl"] = pnl
            send_telegram_message(
                f"*GH EXIT basket #{e['basket_num']}* — {date_str} {bar_time}\n"
                f"Reason: {exit_reason}\n"
                f"Entry {e['entry_price']:.2f} @ {e['entry_time']} -> Exit {price:.2f}\n"
                f"PnL: {'+' if pnl >= 0 else ''}{pnl:.2f}"
            )

    # --- Entry logic ------------------------------------------------------------
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
            "entry_time": bar_time,
            "entry_price": price,
            "min_price": price,
            "trailing_stop": price + (atr_multiplier * atr),
            "exit_price": None,
            "exit_time": None,
            "exit_reason": None,
            "pnl": None,
        })
        send_telegram_message(
            f"*GH SELL basket #{state['baskets_deployed']}* — {date_str} {bar_time}\n"
            f"Strikes {state['strikes'][0]}/{state['strikes'][1]}/{state['strikes'][2]} "
            f"(expiry {state['expiry']})\n"
            f"Combined price: {price:.2f} (VWAP {vwap:.2f})\n"
            f"Qty per leg: {qty_per_leg} ({config.LOTS_PER_LEG_PER_BASKET} lot x {lot_size})"
        )
    else:
        print(f"No new entry. price={price:.2f} vwap={vwap:.2f} below={is_below}")

    state["was_below_vwap"] = is_below
    return state


def finalize_squareoff(state, date_str, exit_price):
    """
    Close every still-open basket at `exit_price` (the 15:15 time exit).
    Returns state unchanged (squared_off stays False) if exit_price is None,
    so the caller can retry once a valid price is available.
    """
    lot_size = config.get_lot_size(date_str)
    qty_per_leg = config.LOTS_PER_LEG_PER_BASKET * lot_size
    exit_time = config.SQUARE_OFF_TIME

    open_entries = [e for e in state["entries"] if e["exit_price"] is None]
    if not open_entries:
        print("Nothing open at square-off time.")
        state["squared_off"] = True
        return state

    if exit_price is None:
        print("Could not get an exit price this run - will retry next run.")
        return state

    lines = [f"*SQUARE OFF* — {date_str} {exit_time}"]
    for e in open_entries:
        pnl = (e["entry_price"] - exit_price) * qty_per_leg
        e["exit_price"] = exit_price
        e["exit_time"] = exit_time
        e["exit_reason"] = "15:15 Time Exit"
        e["pnl"] = pnl
        lines.append(f"#{e['basket_num']}: entry {e['entry_price']:.2f} @ {e['entry_time']} "
                     f"-> exit {exit_price:.2f} = {'+' if pnl >= 0 else ''}{pnl:.2f}")

    total_day_pnl = sum(e["pnl"] for e in state["entries"] if e["pnl"] is not None)
    lines.append(f"\n*Day total: {'+' if total_day_pnl >= 0 else ''}{total_day_pnl:.2f}* "
                 f"across {state['baskets_deployed']} basket(s)")
    send_telegram_message("\n".join(lines))

    state["squared_off"] = True
    return state
