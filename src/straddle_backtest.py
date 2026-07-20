"""
Core backtest engine for the Nifty Straddle-VWAP mean-reversion strategy.

Strategy recap
--------------
1. At 09:45 IST, sample the Nifty spot price and round to the nearest 100
   (both directions) to fix the ATM strike for the day.
2. Build the combined straddle (ATM CE + ATM PE) 1-min price series for the
   whole day, and its cumulative VWAP starting from market open (09:15).
3. From 09:45 onward, every time the combined straddle price makes a fresh
   downward crossing of its own VWAP (was >= VWAP, now < VWAP), SELL one
   straddle (1 lot CE + 1 lot PE) - up to a cap of MAX_STRADDLES_PER_DAY.
4. All open straddles are squared off together at 15:15 IST (no intraday
   profit target / stop - purely a time exit).

No numpy / pandas - pure python throughout.
"""

from src.ist_time import ist_datetime, ist_to_epoch, epoch_to_ist_time_str
from src.vwap import compute_cumulative_vwap
from src.expiry_utils import round_to_nearest_strike, build_option_symbol
import config


def _bars_from(candles, start_epoch=None):
    """Convert raw candle dicts into (epoch, price, volume) bars, optionally
    filtered to epoch >= start_epoch."""
    out = []
    for c in candles:
        if start_epoch is not None and c["epoch"] < start_epoch:
            continue
        out.append(c)
    return out


def _find_spot_at(candles, target_epoch):
    """Find the candle closest to (but not after) target_epoch; fall back to
    the first candle at/after it if none precede it."""
    before = [c for c in candles if c["epoch"] <= target_epoch]
    if before:
        return before[-1]
    after = [c for c in candles if c["epoch"] > target_epoch]
    return after[0] if after else None


def merge_straddle_series(ce_candles, pe_candles):
    """
    Merge CE and PE 1-min candles on matching epoch timestamps into a combined
    straddle series: [{"epoch", "price" (CE.close+PE.close), "volume" (CE.vol+PE.vol),
    "ce_close", "pe_close"}], sorted ascending. Timestamps present in only one
    leg are dropped (rare, usually a missing/illiquid minute).
    """
    pe_by_epoch = {c["epoch"]: c for c in pe_candles}
    merged = []
    for ce in ce_candles:
        pe = pe_by_epoch.get(ce["epoch"])
        if pe is None:
            continue
        merged.append({
            "epoch": ce["epoch"],
            "price": ce["close"] + pe["close"],
            "volume": (ce.get("volume") or 0) + (pe.get("volume") or 0),
            "ce_close": ce["close"],
            "pe_close": pe["close"],
        })
    merged.sort(key=lambda b: b["epoch"])
    return merged


def run_day_backtest(trade_date_str, fyers_client):
    """
    Run the strategy for a single trading day.

    Returns a dict describing the day's result, or None if the day should be
    skipped (e.g. no data - holiday, or option history unavailable).
    """
    lot_size = config.get_lot_size(trade_date_str)
    qty_per_leg_per_straddle = config.LOTS_PER_STRADDLE_ENTRY * lot_size

    # --- 1. Spot at 09:45, fix ATM strike -----------------------------------
    spot_candles = fyers_client.get_day_candles(config.UNDERLYING_SYMBOL, trade_date_str)
    if not spot_candles:
        return None

    fix_epoch = ist_to_epoch(ist_datetime(trade_date_str, config.STRIKE_FIX_TIME))
    spot_bar = _find_spot_at(spot_candles, fix_epoch)
    if spot_bar is None:
        return None

    strike = round_to_nearest_strike(spot_bar["close"])
    ce_symbol, expiry_date = build_option_symbol(trade_date_str, strike, "CE")
    pe_symbol, _ = build_option_symbol(trade_date_str, strike, "PE")

    # --- 2. Fetch CE/PE 1-min candles for the whole day ---------------------
    ce_candles = fyers_client.get_day_candles(ce_symbol, trade_date_str)
    pe_candles = fyers_client.get_day_candles(pe_symbol, trade_date_str)
    if not ce_candles or not pe_candles:
        return {
            "date": trade_date_str,
            "strike": strike,
            "expiry": expiry_date,
            "ce_symbol": ce_symbol,
            "pe_symbol": pe_symbol,
            "error": "missing_option_data",
            "series": [],
            "entries": [],
            "day_pnl": 0.0,
        }

    merged = merge_straddle_series(ce_candles, pe_candles)
    if not merged:
        return {
            "date": trade_date_str,
            "strike": strike,
            "expiry": expiry_date,
            "ce_symbol": ce_symbol,
            "pe_symbol": pe_symbol,
            "error": "no_overlapping_bars",
            "series": [],
            "entries": [],
            "day_pnl": 0.0,
        }

    # --- 3. VWAP from market open --------------------------------------------
    vwap_values = compute_cumulative_vwap(merged)
    for bar, vwap in zip(merged, vwap_values):
        bar["vwap"] = vwap

    signal_epoch = ist_to_epoch(ist_datetime(trade_date_str, config.SIGNAL_START_TIME))
    squareoff_epoch = ist_to_epoch(ist_datetime(trade_date_str, config.SQUARE_OFF_TIME))

    # --- 4. Walk the series: detect fresh downward VWAP crossings -----------
    entries = []          # each: {entry_epoch, entry_price, straddle_num}
    was_below_vwap = False
    straddles_deployed = 0

    for bar in merged:
        if bar["epoch"] < signal_epoch:
            # still track above/below state through the pre-signal window so we
            # don't fire a false "fresh cross" right at 09:45 if it was already
            # below vwap beforehand
            was_below_vwap = bar["price"] < bar["vwap"]
            continue

        if bar["epoch"] >= squareoff_epoch:
            break

        is_below = bar["price"] < bar["vwap"]
        fresh_cross_down = is_below and not was_below_vwap

        if fresh_cross_down and straddles_deployed < config.MAX_STRADDLES_PER_DAY:
            straddles_deployed += 1
            entries.append({
                "entry_epoch": bar["epoch"],
                "entry_time": epoch_to_ist_time_str(bar["epoch"]),
                "entry_price": bar["price"],
                "straddle_num": straddles_deployed,
            })

        was_below_vwap = is_below

    # --- 5. Square off all open entries at 15:15 -----------------------------
    squareoff_bar = _find_spot_at(merged, squareoff_epoch)
    exit_price = squareoff_bar["price"] if squareoff_bar else None
    exit_time = epoch_to_ist_time_str(squareoff_bar["epoch"]) if squareoff_bar else config.SQUARE_OFF_TIME

    day_pnl = 0.0
    for e in entries:
        if exit_price is None:
            e["exit_price"] = None
            e["pnl"] = 0.0
            continue
        # SHORT straddle: profit = (entry_price - exit_price) * qty
        pnl = (e["entry_price"] - exit_price) * qty_per_leg_per_straddle
        e["exit_price"] = exit_price
        e["exit_time"] = exit_time
        e["pnl"] = pnl
        day_pnl += pnl

    return {
        "date": trade_date_str,
        "strike": strike,
        "expiry": expiry_date,
        "ce_symbol": ce_symbol,
        "pe_symbol": pe_symbol,
        "lot_size": lot_size,
        "qty_per_leg_per_straddle": qty_per_leg_per_straddle,
        "series": merged,
        "entries": entries,
        "day_pnl": day_pnl,
        "num_straddles_deployed": straddles_deployed,
    }
