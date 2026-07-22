"""
Continuous LOCAL runner - uses the Fyers WebSocket to build 1-minute candles
in memory, instead of polling Fyers' historical REST API every 2 minutes via
GitHub Actions. Meant to run directly on your own machine:

    python live_notifier_local.py

Entry, exit, and Telegram messaging are byte-for-byte the SAME logic as the
cloud version (live_notifier.py) - both call into src/live_engine.py, which
is the one place that actually decides what to do with a bar. Only the data
source differs here: ticks aggregated locally into ~1-minute bars, instead
of re-fetching historical candles from Fyers' REST API every run.

Usage notes:
  - Fyers access tokens are valid for that trading day only - regenerate
    FYERS_ACCESS_TOKEN each morning before starting this, same as your other
    Fyers scripts.
  - Safe to start this any time before market open (e.g. 07:00) - it just
    idles with no network connection open until 09:15 IST, then connects to
    the websocket and starts fetching. Runs through the 15:15 square-off,
    then exits on its own. Start it fresh each morning.
  - Consider running this under `tmux`/`screen` (or as a systemd user
    service) so it keeps running if you close the terminal.
  - State is saved to data/live_state.json after every processed bar, same
    file format as the cloud version, so you can inspect it any time. No git
    commit needed here since this isn't running on ephemeral CI infra.
"""

import time

from src.env_loader import load_dotenv_if_present
load_dotenv_if_present()

import config
from src.ist_time import IST, ist_datetime, ist_to_epoch, is_weekend
from src.vwap import compute_cumulative_vwap
from src.straddle_backtest import merge_basket_series, compute_basket_atr
from src.fyers_ws_client import FyersWSClient
from src.live_engine import build_strike_plan, announce_strike_fixed, evaluate_bar, finalize_squareoff
from live_notifier import STATE_PATH, _empty_state, load_state, save_state, _now_ist  # noqa: F401 - reused, not redefined

POLL_INTERVAL_SECONDS = 5      # how often we check the clock / look for a new closed bar
PRE_MARKET_POLL_SECONDS = 30   # coarser polling while just waiting for 09:15 - no need for 5s precision hours in advance


def _build_latest_bar(ws_client, leg_symbols, min_epoch=None):
    """Merge whatever finalized 1-min candles exist so far across all 6 legs,
    compute VWAP + ATR, and return the latest merged bar with epoch >=
    min_epoch (if given) - or None if there's no such bar yet.

    The min_epoch filter matters: VWAP itself is computed cumulatively from
    market open using ALL available candles (so it's identical to the batch
    backtest and the REST-polling cloud version), but the bar actually
    handed to evaluate_bar() must be at/after SIGNAL_START_TIME, exactly like
    live_notifier.py's check_market() filters with signal_epoch. Skipping
    this filter would let a pre-09:45 bar trigger the "first bar" entry rule
    early, which is not what the strategy intends.
    """
    leg_candles = {sym: ws_client.get_candles(sym) for sym in leg_symbols}
    if any(not candles for candles in leg_candles.values()):
        return None

    merged = merge_basket_series(leg_candles)
    if not merged:
        return None

    vwap_values = compute_cumulative_vwap(merged)
    atr_values = compute_basket_atr(merged, period=config.ATR_PERIOD)
    for bar, vwap, atr in zip(merged, vwap_values, atr_values):
        bar["vwap"] = vwap
        bar["atr"] = atr

    if min_epoch is not None:
        merged = [b for b in merged if b["epoch"] >= min_epoch]
        if not merged:
            return None

    return merged[-1]


def _current_combined_price(ws_client, leg_symbols):
    """Sum of the latest LTP across all legs - used for the square-off exit
    price, where we want 'right now', not 'as of the last closed minute'."""
    prices = []
    for sym in leg_symbols:
        ltp = ws_client.get_latest_ltp(sym)
        if ltp is None:
            return None
        prices.append(ltp)
    return sum(prices)


def run(ws_override=None, now_func=None, sleep_func=None):
    now_func = now_func or _now_ist
    sleep_func = sleep_func or time.sleep

    date_str = now_func().strftime("%Y-%m-%d")

    if is_weekend(date_str) or date_str in config.NSE_HOLIDAYS:
        print(f"{date_str} is a weekend/holiday - nothing to do today.")
        return

    state = load_state()
    if state is None or state.get("date") != date_str:
        state = _empty_state(date_str)
        print(f"New day - state reset for {date_str}")
    save_state(state)

    if state["squared_off"]:
        print(f"{date_str} already squared off - nothing to do.")
        return

    # --- Wait for market open before touching the websocket at all -----------
    # You can start this script any time (e.g. 07:00) - it just idles here,
    # with no network connection open, until 09:15. Data only starts getting
    # fetched from market open.
    last_wait_print = None
    while True:
        now = now_func()
        time_str = now.strftime("%H:%M")
        if time_str >= config.MARKET_OPEN_TIME:
            break
        if now.minute % 10 == 0 and time_str != last_wait_print:
            print(f"Waiting for market open ({time_str} < {config.MARKET_OPEN_TIME}) - "
                  f"not connecting to Fyers yet.")
            last_wait_print = time_str
        sleep_func(PRE_MARKET_POLL_SECONDS)

    ws = ws_override or FyersWSClient()
    if ws_override is None:
        print("Connecting to Fyers WebSocket...")
        ws.connect(initial_symbols=[config.UNDERLYING_SYMBOL])
        if not ws.is_connected():
            print("WARNING: WebSocket didn't confirm connection within the timeout - "
                  "continuing anyway in case ticks are just arriving slowly.")
    else:
        ws.connect(initial_symbols=[config.UNDERLYING_SYMBOL])

    last_processed_minute = None
    print(f"Running for {date_str}. Waiting for {config.STRIKE_FIX_TIME} to fix the strike...")

    try:
        while True:
            now = now_func()
            time_str = now.strftime("%H:%M")

            # --- 1. Strike not fixed yet ------------------------------------------
            if not state["strike_fixed"]:
                if time_str >= config.STRIKE_FIX_TIME:
                    ws.flush_current_bar(config.UNDERLYING_SYMBOL)
                    spot_ltp = ws.get_latest_ltp(config.UNDERLYING_SYMBOL)
                    if spot_ltp is None:
                        print("No spot tick yet at strike-fix time - waiting...")
                        sleep_func(POLL_INTERVAL_SECONDS)
                        continue

                    atm_strike, strikes, leg_symbols, expiry_date = build_strike_plan(spot_ltp, date_str)
                    state.update({
                        "strike_fixed": True,
                        "atm_strike": atm_strike,
                        "strikes": strikes,
                        "expiry": expiry_date,
                        "leg_symbols": leg_symbols,
                    })
                    save_state(state)

                    ws.unsubscribe([config.UNDERLYING_SYMBOL])
                    ws.subscribe(leg_symbols)

                    announce_strike_fixed(date_str, spot_ltp, atm_strike, strikes, expiry_date)
                    print(f"Strike fixed: {strikes}, expiry {expiry_date}. "
                          f"Watching for entries...")
                else:
                    print(f"Waiting for strike-fix time ({time_str} < {config.STRIKE_FIX_TIME})")
                sleep_func(POLL_INTERVAL_SECONDS)
                continue

            # --- 2. Square-off time --------------------------------------------------
            if time_str >= config.SQUARE_OFF_TIME:
                for sym in state["leg_symbols"]:
                    ws.flush_current_bar(sym)
                exit_price = _current_combined_price(ws, state["leg_symbols"])
                state = finalize_squareoff(state, date_str, exit_price)
                save_state(state)
                if state["squared_off"]:
                    print("Squared off for the day. Exiting.")
                    break
                print("Couldn't get an exit price yet - will retry shortly.")
                sleep_func(POLL_INTERVAL_SECONDS)
                continue

            # --- 3. Before signal-start time ------------------------------------------
            if time_str < config.SIGNAL_START_TIME:
                sleep_func(POLL_INTERVAL_SECONDS)
                continue

            # --- 4. Normal trading window: process once per newly-closed minute ------
            signal_epoch = ist_to_epoch(ist_datetime(date_str, config.SIGNAL_START_TIME))
            bar = _build_latest_bar(ws, state["leg_symbols"], min_epoch=signal_epoch)
            if bar is not None and bar["epoch"] != last_processed_minute:
                state = evaluate_bar(state, date_str, bar)
                save_state(state)
                last_processed_minute = bar["epoch"]

            sleep_func(POLL_INTERVAL_SECONDS)

    except KeyboardInterrupt:
        print("\nInterrupted - saving state and exiting. "
              "Re-run to resume (today's progress is preserved in data/live_state.json).")
        save_state(state)


if __name__ == "__main__":
    run()
