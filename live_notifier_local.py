"""
Continuous LOCAL runner - uses the Fyers WebSocket to build candles in
memory at config.LIVE_BAR_SECONDS resolution (5 seconds by default), instead
of polling Fyers' historical REST API every 2 minutes via GitHub Actions.
Meant to run directly on your own machine:

    python live_notifier_local.py

Runs THREE independent basket sets (config.BASKET_SETS - "A", "B", "C" by
default), each with its own VWAP/entries/exits/PnL/chart, built from
overlapping strikes fetched once over one websocket connection (e.g. set A's
24200 strike is also set B's, so it's only subscribed once).

Entry, exit, and Telegram messaging logic is the SAME code as the cloud
version (live_notifier.py) - both call into src/live_engine.py, which is the
one place that actually decides what to do with a bar, per set. What
differs here is both the data source (ticks aggregated locally, instead of
re-fetching historical candles from Fyers' REST API every run) AND the bar
resolution: this path decides on config.LIVE_BAR_SECONDS bars (5 seconds by
default) rather than the 1-minute bars the batch backtest and the cloud/REST
path use. That's a deliberate choice (see config.py's LIVE_BAR_SECONDS /
LIVE_ATR_PERIOD comments) - expect this to trade more often, and somewhat
differently, than the 1-minute backtest predicts.

TELEGRAM: entry/exit/strike-fix/heartbeat TEXT alerts are gated by
config.SEND_TRADE_TEXT_MESSAGES (default False) - when off, those events
print to the console only. The dashboard HTML file itself is pushed to
Telegram as a document every config.DASHBOARD_SEND_INTERVAL_SECONDS (5 min
by default), independent of that flag.

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
  - docs/live.html is regenerated locally every time state is saved too - open
    it directly in your browser (it auto-refreshes every 60 seconds) to watch
    all three sets' sessions without needing GitHub Pages or any cloud step.
"""

import os
import time

from src.env_loader import load_dotenv_if_present
load_dotenv_if_present()

import config
from src.ist_time import IST, ist_datetime, ist_to_epoch, is_weekend
from src.vwap import compute_cumulative_vwap
from src.straddle_backtest import merge_basket_series, compute_basket_atr
from src.fyers_ws_client import FyersWSClient
from src.live_engine import (
    build_multi_basket_plan, announce_strike_fixed, evaluate_bar, finalize_squareoff,
    maybe_send_combined_heartbeat,
)
from src.live_dashboard import generate_live_page
from src.telegram_notifier import send_telegram_document
from live_notifier import (  # noqa: F401 - reused, not redefined
    STATE_PATH, _empty_state, all_sets_squared_off, load_state, save_state, _now_ist,
)

POLL_INTERVAL_SECONDS = 5      # how often we check the clock / look for a new closed bar
PRE_MARKET_POLL_SECONDS = 30   # coarser polling while just waiting for 09:15 - no need for 5s precision hours in advance

LIVE_PAGE_PATH = os.path.join(config.OUTPUT_DIR, "live.html")   # docs/live.html


def _save_and_render(state):
    """Save state AND regenerate docs/live.html, so there's always something
    to look at locally - open docs/live.html in your browser (it auto-
    refreshes every 60s) to watch today's session without needing GitHub
    Pages or any cloud step."""
    save_state(state)
    os.makedirs(config.OUTPUT_DIR, exist_ok=True)
    generate_live_page(state, LIVE_PAGE_PATH, backtest_index_exists=os.path.exists(config.OUTPUT_HTML))


def _maybe_send_dashboard_file(state, date_str, now_epoch):
    """Sends docs/live.html itself to Telegram as a document, at most once
    every config.DASHBOARD_SEND_INTERVAL_SECONDS - same last-sent-epoch
    pattern as the heartbeat. NOT gated by config.SEND_TRADE_TEXT_MESSAGES -
    always sends regardless of that flag. Mutates and returns state."""
    last = state.get("last_dashboard_sent_epoch")
    if last is not None and (now_epoch - last) < config.DASHBOARD_SEND_INTERVAL_SECONDS:
        return state
    state["last_dashboard_sent_epoch"] = now_epoch
    if os.path.exists(LIVE_PAGE_PATH):
        send_telegram_document(LIVE_PAGE_PATH, caption=f"Live dashboard — {date_str}")
    return state


def _build_latest_bar(ws_client, symbols, min_epoch=None):
    """Merge whatever finalized bars (config.LIVE_BAR_SECONDS resolution)
    exist so far across the given legs, compute VWAP + ATR, and return the
    latest merged bar with epoch >= min_epoch (if given) - or None if
    there's no such bar yet.

    The min_epoch filter matters: VWAP itself is computed cumulatively from
    market open using ALL available candles (so it's identical to the batch
    backtest and the REST-polling cloud version), but the bar actually
    handed to evaluate_bar() must be at/after SIGNAL_START_TIME, exactly like
    live_notifier.py's check_market() filters with signal_epoch. Skipping
    this filter would let a pre-09:45 bar trigger the "first bar" entry rule
    early, which is not what the strategy intends.
    """
    leg_candles = {sym: ws_client.get_candles(sym) for sym in symbols}
    if any(not candles for candles in leg_candles.values()):
        return None

    merged = merge_basket_series(leg_candles)
    if not merged:
        return None

    vwap_values = compute_cumulative_vwap(merged)
    atr_values = compute_basket_atr(merged, period=config.LIVE_ATR_PERIOD)
    for bar, vwap, atr in zip(merged, vwap_values, atr_values):
        bar["vwap"] = vwap
        bar["atr"] = atr

    if min_epoch is not None:
        merged = [b for b in merged if b["epoch"] >= min_epoch]
        if not merged:
            return None

    return merged[-1]


def _current_combined_price(ws_client, symbols):
    """Sum of the latest LTP across the given legs - used for the
    square-off exit price, where we want 'right now', not 'as of the last
    closed bar'."""
    prices = []
    for sym in symbols:
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
    _save_and_render(state)

    if all_sets_squared_off(state):
        print(f"{date_str} already squared off - nothing to do.")
        return

    # If this is (re-)started after today's square-off time has already
    # passed AND strikes were never even fixed yet - e.g. restarted mid-
    # afternoon while debugging - there's no point connecting, fixing
    # strikes, and firing a "Strikes fixed" alert for a window that's
    # already over, since no entries could possibly exist. (If strikes WERE
    # already fixed - e.g. restarting after a crash with open baskets still
    # live in some set - fall through to the normal flow instead, so those
    # still get properly squared off at a real price rather than silently
    # discarded.)
    now_at_start = now_func()
    if (not state["strike_fixed"]
            and now_at_start.strftime("%H:%M") >= config.SQUARE_OFF_TIME):
        print(f"It's already past {config.SQUARE_OFF_TIME} for {date_str} and no "
              f"strikes were fixed - nothing to trade today. Marking as squared off.")
        for bs in state["basket_sets"].values():
            bs["squared_off"] = True
        _save_and_render(state)
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

    last_processed_bar_epoch = {label: None for label in config.BASKET_SETS}
    last_strike_wait_print = None
    print(f"Running for {date_str}. Waiting for {config.STRIKE_FIX_TIME} to fix the strikes...")

    try:
        while True:
            now = now_func()
            time_str = now.strftime("%H:%M")

            # --- 1. Strikes not fixed yet ------------------------------------------
            if not state["strike_fixed"]:
                if time_str >= config.STRIKE_FIX_TIME:
                    ws.flush_current_bar(config.UNDERLYING_SYMBOL)
                    spot_ltp = ws.get_latest_ltp(config.UNDERLYING_SYMBOL)
                    if spot_ltp is None:
                        print("No spot tick yet at strike-fix time - waiting...")
                        sleep_func(POLL_INTERVAL_SECONDS)
                        continue

                    atm_strike, all_strikes, set_strikes, set_symbols, all_leg_symbols, expiry_date = \
                        build_multi_basket_plan(spot_ltp, date_str)

                    state.update({
                        "strike_fixed": True,
                        "atm_strike": atm_strike,
                        "all_strikes": all_strikes,
                        "all_leg_symbols": all_leg_symbols,
                        "expiry": expiry_date,
                    })
                    for label in state["basket_sets"]:
                        state["basket_sets"][label]["strikes"] = set_strikes[label]
                        state["basket_sets"][label]["symbols"] = set_symbols[label]
                    _save_and_render(state)

                    ws.unsubscribe([config.UNDERLYING_SYMBOL])
                    ws.subscribe(all_leg_symbols)

                    announce_strike_fixed(date_str, spot_ltp, atm_strike, set_strikes, expiry_date)
                    print(f"Strikes fixed: {set_strikes}, expiry {expiry_date}. "
                          f"Watching for entries...")
                else:
                    if now.minute % 5 == 0 and time_str != last_strike_wait_print:
                        print(f"Waiting for strike-fix time ({time_str} < {config.STRIKE_FIX_TIME})")
                        last_strike_wait_print = time_str
                sleep_func(POLL_INTERVAL_SECONDS)
                continue

            # --- 2. Square-off time --------------------------------------------------
            if time_str >= config.SQUARE_OFF_TIME:
                for sym in state["all_leg_symbols"]:
                    ws.flush_current_bar(sym)
                for label, bs in state["basket_sets"].items():
                    if bs["squared_off"]:
                        continue
                    exit_price = _current_combined_price(ws, bs["symbols"])
                    state["basket_sets"][label] = finalize_squareoff(bs, date_str, exit_price, label)
                _save_and_render(state)
                if all_sets_squared_off(state):
                    print("All sets squared off for the day. Exiting.")
                    break
                print("At least one set couldn't get an exit price yet - will retry shortly.")
                sleep_func(POLL_INTERVAL_SECONDS)
                continue

            # --- 3. Before signal-start time ------------------------------------------
            if time_str < config.SIGNAL_START_TIME:
                sleep_func(POLL_INTERVAL_SECONDS)
                continue

            # --- 4. Normal trading window: process once per newly-closed bar, per set -
            signal_epoch = ist_to_epoch(ist_datetime(date_str, config.SIGNAL_START_TIME))
            for label, bs in state["basket_sets"].items():
                bar = _build_latest_bar(ws, bs["symbols"], min_epoch=signal_epoch)
                if bar is not None and bar["epoch"] != last_processed_bar_epoch[label]:
                    state["basket_sets"][label] = evaluate_bar(
                        bs, date_str, bar, label, bs["strikes"], state["expiry"])
                    last_processed_bar_epoch[label] = bar["epoch"]

            # Heartbeat and dashboard-file push run on wall-clock time,
            # independent of whether a new bar happened to close this
            # iteration - so you still get periodic updates even in a quiet
            # stretch.
            state = maybe_send_combined_heartbeat(state, date_str, int(now.timestamp()))
            state = _maybe_send_dashboard_file(state, date_str, int(now.timestamp()))
            _save_and_render(state)

            sleep_func(POLL_INTERVAL_SECONDS)

    except KeyboardInterrupt:
        print("\nInterrupted - saving state and exiting. "
              "Re-run to resume (today's progress is preserved in data/live_state.json).")
        _save_and_render(state)


if __name__ == "__main__":
    run()
