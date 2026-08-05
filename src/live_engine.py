"""
Shared entry/exit/Telegram decision logic - the single source of truth used
by BOTH:
  - live_notifier.py (cloud, REST-polling every ~2 min via GitHub Actions)
  - live_notifier_local.py (local, websocket-driven, config.LIVE_BAR_SECONDS bars)

Runs THREE independently-tracked basket sets (config.BASKET_SETS - "A", "B",
"C" by default), each with its own combined price, VWAP, ATR, entries,
exits, and PnL. They share strikes (e.g. set A's 24200 strike is also set
B's), so the strikes are fetched once, but each set's price/VWAP/entries are
otherwise completely independent of the others.

Whichever runner is fetching/building the data, it hands one already-
computed bar ({"epoch","price","vwap","atr"}) per set to evaluate_bar() here
for THAT set, so the actual strategy behavior can never quietly drift
between the two runners.

Telegram TEXT alerts (entry/exit/strike-fix/heartbeat) are gated by
config.SEND_TRADE_TEXT_MESSAGES inside src/telegram_notifier.py - when off
(the default), these calls just print to the console instead. The dashboard
HTML file is pushed separately (src/telegram_notifier.send_telegram_document),
unaffected by that flag.
"""

from datetime import datetime

import config
from src.ist_time import IST
from src.expiry_utils import round_to_nearest_strike, build_option_symbol
from src.telegram_notifier import send_telegram_message


def build_multi_basket_plan(spot_price, date_str):
    """
    Given a spot price and trade date, compute everything needed for all
    basket sets at once:
      - atm_strike: the rounded ATM strike
      - all_strikes: sorted list of every distinct strike needed across all
        sets (fetched once, shared)
      - set_strikes: {"A": [s1,s2,s3], "B": [...], "C": [...]}
      - set_symbols: {"A": [6 option symbols], "B": [...], "C": [...]}
      - all_leg_symbols: every distinct option symbol needed (for subscribing)
      - expiry_date: the expiry label used for all of them (same expiry for
        every strike/set)
    """
    atm_strike = round_to_nearest_strike(spot_price)

    set_strikes = {
        label: sorted(atm_strike + off for off in offsets)
        for label, offsets in config.BASKET_SETS.items()
    }
    all_strikes = sorted(set(s for strikes in set_strikes.values() for s in strikes))

    symbols_by_strike = {}
    expiry_date = None
    for strike in all_strikes:
        for opt_type in ("CE", "PE"):
            sym, expiry_date = build_option_symbol(date_str, strike, opt_type)
            symbols_by_strike.setdefault(strike, {})[opt_type] = sym

    set_symbols = {}
    for label, strikes in set_strikes.items():
        syms = []
        for s in strikes:
            syms.append(symbols_by_strike[s]["CE"])
            syms.append(symbols_by_strike[s]["PE"])
        set_symbols[label] = syms

    all_leg_symbols = sorted(set(sym for syms in set_symbols.values() for sym in syms))

    return atm_strike, all_strikes, set_strikes, set_symbols, all_leg_symbols, expiry_date


def announce_strike_fixed(date_str, spot_price, atm_strike, set_strikes, expiry_date):
    set_lines = "\n".join(
        f"  Set {label}: {'/'.join(str(s) for s in strikes)}"
        for label, strikes in sorted(set_strikes.items())
    )
    send_telegram_message(
        f"*Basket sets fixed* — {date_str}\n"
        f"Spot: {spot_price:.1f} -> ATM {atm_strike}\n"
        f"{set_lines}\n"
        f"Expiry: {expiry_date}\n"
        f"Watching for entries from now..."
    )


def compute_pnl_summary(basket_state, date_str):
    """Returns (booked_pnl, unbooked_pnl, total_pnl) for ONE basket set.

    booked = sum of realized pnl on already-closed baskets in this set.
    unbooked = mark-to-market on still-open baskets in this set, using
    basket_state["last_price"] - None if we don't have a current price yet.
    """
    lot_size = config.get_lot_size(date_str)
    qty_per_leg = config.LOTS_PER_LEG_PER_BASKET * lot_size

    booked = sum(e["pnl"] for e in basket_state["entries"] if e.get("pnl") is not None)

    unbooked = 0.0
    last_price = basket_state.get("last_price")
    if last_price is not None:
        for e in basket_state["entries"]:
            if e.get("exit_price") is None:
                unbooked += (e["entry_price"] - last_price) * qty_per_leg

    return booked, unbooked, booked + unbooked


def maybe_send_combined_heartbeat(state, date_str, now_epoch):
    """
    Sends ONE Telegram heartbeat covering all basket sets together, at most
    once every config.HEARTBEAT_INTERVAL_SECONDS - tracked via
    state["last_heartbeat_epoch"] (top-level, shared across sets) so it
    works the same whether called from a continuous loop (websocket local
    runner) or a stateless-per-invocation cron (cloud REST runner). Mutates
    and returns state.
    """
    last = state.get("last_heartbeat_epoch")
    if last is not None and (now_epoch - last) < config.HEARTBEAT_INTERVAL_SECONDS:
        return state
    state["last_heartbeat_epoch"] = now_epoch

    lines = [f"*Heartbeat* — {date_str}"]
    grand_booked = grand_unbooked = 0.0

    for label in sorted(state["basket_sets"].keys()):
        bs = state["basket_sets"][label]
        booked, unbooked, total = compute_pnl_summary(bs, date_str)
        grand_booked += booked
        grand_unbooked += unbooked

        open_count = sum(1 for e in bs["entries"] if e.get("exit_price") is None)
        closed_count = bs["baskets_deployed"] - open_count
        price_bit = (f"{bs['last_price']:.2f} vs {bs['last_vwap']:.2f}"
                     if bs.get("last_price") is not None else "no price yet")
        lines.append(
            f"Set {label}: price {price_bit} | open {open_count}/closed {closed_count} "
            f"| booked {booked:+.2f} | unbooked {unbooked:+.2f}"
        )

    grand_total = grand_booked + grand_unbooked
    lines.append(f"\n*Grand total PnL: {grand_total:+.2f}* "
                 f"(booked {grand_booked:+.2f}, unbooked {grand_unbooked:+.2f})")
    send_telegram_message("\n".join(lines))
    return state


def evaluate_bar(basket_state, date_str, bar, set_label, strikes, expiry_date):
    """
    Core entry/exit decision for ONE already-computed bar, for ONE basket
    set. Mutates and returns `basket_state`. Sends a Telegram alert on every
    entry and every exit, labeled with which set it's for.

    Entry: the first bar ever evaluated for this set enters if price < VWAP;
    after that, every fresh downward VWAP crossing deploys another basket
    (capped by config.MAX_BASKETS_PER_DAY if set) - all independent per set.

    Exit (checked per open basket in this set, independently): price crosses
    back above this set's VWAP, OR price rallies back through its ATR
    trailing stop. (The 15:15 time exit is handled separately by
    finalize_squareoff().)
    """
    lot_size = config.get_lot_size(date_str)
    qty_per_leg = config.LOTS_PER_LEG_PER_BASKET * lot_size
    atr_multiplier = config.ATR_MULTIPLIER

    price, vwap, atr = bar["price"], bar["vwap"], bar["atr"]
    current_epoch = bar["epoch"]
    bar_time = datetime.fromtimestamp(current_epoch, tz=IST).strftime("%H:%M:%S")

    is_below = price < vwap
    is_above = price > vwap

    basket_state["last_price"] = price
    basket_state["last_vwap"] = vwap
    basket_state["last_updated"] = bar_time
    basket_state.setdefault("series", []).append({
        "time": bar_time,
        "price": round(price, 2),
        "vwap": round(vwap, 2),
    })

    # --- Exits on currently open baskets in this set -------------------------
    for e in basket_state["entries"]:
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
                f"*EXIT Set {set_label} basket #{e['basket_num']}* — {date_str} {bar_time}\n"
                f"Reason: {exit_reason}\n"
                f"Entry {e['entry_price']:.2f} @ {e['entry_time']} -> Exit {price:.2f}\n"
                f"PnL: {'+' if pnl >= 0 else ''}{pnl:.2f}"
            )

    # --- Entry logic ------------------------------------------------------------
    at_cap = (config.MAX_BASKETS_PER_DAY is not None
              and basket_state["baskets_deployed"] >= config.MAX_BASKETS_PER_DAY)
    should_enter = False

    if not basket_state["first_check_done"]:
        basket_state["first_check_done"] = True
        if is_below and not at_cap:
            should_enter = True
    elif is_below and not basket_state["was_below_vwap"] and not at_cap:
        should_enter = True

    if should_enter:
        basket_state["baskets_deployed"] += 1
        basket_state["entries"].append({
            "basket_num": basket_state["baskets_deployed"],
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
            f"*SELL Set {set_label} basket #{basket_state['baskets_deployed']}* — "
            f"{date_str} {bar_time}\n"
            f"Strikes {strikes[0]}/{strikes[1]}/{strikes[2]} (expiry {expiry_date})\n"
            f"Combined price: {price:.2f} (VWAP {vwap:.2f})\n"
            f"Qty per leg: {qty_per_leg} ({config.LOTS_PER_LEG_PER_BASKET} lot x {lot_size})"
        )

    basket_state["was_below_vwap"] = is_below
    return basket_state


def finalize_squareoff(basket_state, date_str, exit_price, set_label):
    """
    Close every still-open basket in this set at `exit_price` (the 15:15
    time exit). Returns basket_state unchanged (with a per-set
    "squared_off" left False) if exit_price is None, so the caller can retry
    once a valid price is available.
    """
    lot_size = config.get_lot_size(date_str)
    qty_per_leg = config.LOTS_PER_LEG_PER_BASKET * lot_size
    exit_time = config.SQUARE_OFF_TIME

    open_entries = [e for e in basket_state["entries"] if e["exit_price"] is None]
    if not open_entries:
        print(f"Set {set_label}: nothing open at square-off time.")
        basket_state["squared_off"] = True
        return basket_state

    if exit_price is None:
        print(f"Set {set_label}: could not get an exit price this run - will retry next run.")
        return basket_state

    lines = [f"*SQUARE OFF Set {set_label}* — {date_str} {exit_time}"]
    for e in open_entries:
        pnl = (e["entry_price"] - exit_price) * qty_per_leg
        e["exit_price"] = exit_price
        e["exit_time"] = exit_time
        e["exit_reason"] = "15:15 Time Exit"
        e["pnl"] = pnl
        lines.append(f"#{e['basket_num']}: entry {e['entry_price']:.2f} @ {e['entry_time']} "
                     f"-> exit {exit_price:.2f} = {'+' if pnl >= 0 else ''}{pnl:.2f}")

    total_day_pnl = sum(e["pnl"] for e in basket_state["entries"] if e["pnl"] is not None)
    lines.append(f"\n*Set {set_label} total: {'+' if total_day_pnl >= 0 else ''}{total_day_pnl:.2f}* "
                 f"across {basket_state['baskets_deployed']} basket(s)")
    send_telegram_message("\n".join(lines))

    basket_state["squared_off"] = True
    return basket_state
