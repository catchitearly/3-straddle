"""
Core backtest engine - 3-straddle BASKET strategy.

Strategy recap
--------------
1. At 09:45 IST, sample Nifty spot, round to nearest 100 (both directions) to
   fix the ATM strike for the day. Two more strikes are derived from it:
   ATM-100 and ATM+100 (config.STRIKE_OFFSETS). Three straddles = 6 option legs.
2. Build the COMBINED price series = sum of all 6 legs' 1-min close prices,
   and its combined volume = sum of all 6 legs' volumes. Compute the
   cumulative VWAP of this combined series starting from market open (09:15).
3. From 09:45 onward, every time the combined price makes a FRESH downward
   crossing of its own VWAP (was >= VWAP, now < VWAP), SELL one full basket:
   1 lot each of ATM-100 straddle, ATM straddle, ATM+100 straddle (6 legs,
   1 lot each). This can fire multiple times per day - exposure compounds
   with each fresh cross (config.MAX_BASKETS_PER_DAY caps it if set).
4. All open baskets are squared off together at 15:15 IST (pure time exit).

No numpy / pandas - pure python throughout.
"""

from src.ist_time import ist_datetime, ist_to_epoch, epoch_to_ist_time_str
from src.vwap import compute_cumulative_vwap
from src.expiry_utils import round_to_nearest_strike, build_option_symbol
import config


def _find_spot_at(candles, target_epoch):
    """Find the candle closest to (but not after) target_epoch; fall back to
    the first candle at/after it if none precede it."""
    before = [c for c in candles if c["epoch"] <= target_epoch]
    if before:
        return before[-1]
    after = [c for c in candles if c["epoch"] > target_epoch]
    return after[0] if after else None


def merge_basket_series(leg_candles_by_symbol):
    """
    leg_candles_by_symbol: dict of symbol -> list of candle dicts (6 entries:
    3 strikes x CE/PE).

    Merge on epochs common to ALL 6 legs into a combined basket series:
        [{"epoch", "price" (sum of 6 closes), "volume" (sum of 6 volumes),
          "legs": {symbol: close, ...}}, ...]
    Timestamps missing from any single leg are dropped (illiquid/missing minute).
    """
    symbols = list(leg_candles_by_symbol.keys())
    by_epoch = {sym: {c["epoch"]: c for c in candles}
                for sym, candles in leg_candles_by_symbol.items()}

    if not symbols:
        return []

    common_epochs = set(by_epoch[symbols[0]].keys())
    for sym in symbols[1:]:
        common_epochs &= set(by_epoch[sym].keys())

    merged = []
    for epoch in sorted(common_epochs):
        total_price = 0.0
        total_vol = 0
        legs = {}
        for sym in symbols:
            c = by_epoch[sym][epoch]
            total_price += c["close"]
            total_vol += c.get("volume") or 0
            legs[sym] = c["close"]
        merged.append({"epoch": epoch, "price": total_price, "volume": total_vol, "legs": legs})

    return merged


def run_day_backtest(trade_date_str, fyers_client):
    """
    Run the basket strategy for a single trading day.
    Returns a dict describing the day's result, or None if the day should be
    skipped entirely (no underlying data at all - holiday/no session).
    """
    lot_size = config.get_lot_size(trade_date_str)
    qty_per_leg = config.LOTS_PER_LEG_PER_BASKET * lot_size

    # --- 1. Spot at 09:45, fix ATM + the two wing strikes -------------------
    spot_candles = fyers_client.get_day_candles(config.UNDERLYING_SYMBOL, trade_date_str)
    if not spot_candles:
        return None

    fix_epoch = ist_to_epoch(ist_datetime(trade_date_str, config.STRIKE_FIX_TIME))
    spot_bar = _find_spot_at(spot_candles, fix_epoch)
    if spot_bar is None:
        return None

    atm_strike = round_to_nearest_strike(spot_bar["close"])
    strikes = sorted(atm_strike + off for off in config.STRIKE_OFFSETS)

    # --- 2. Build all 6 option symbols, fetch candles ------------------------
    leg_symbols = {}       # symbol -> (strike, opt_type)
    expiry_date = None
    for strike in strikes:
        for opt_type in ("CE", "PE"):
            sym, expiry_date = build_option_symbol(trade_date_str, strike, opt_type)
            leg_symbols[sym] = (strike, opt_type)

    leg_candles_by_symbol = {}
    missing = []
    for sym in leg_symbols:
        candles = fyers_client.get_day_candles(sym, trade_date_str)
        if not candles:
            missing.append(sym)
        leg_candles_by_symbol[sym] = candles

    base_info = {
        "date": trade_date_str,
        "atm_strike": atm_strike,
        "strikes": strikes,
        "expiry": expiry_date,
        "leg_symbols": list(leg_symbols.keys()),
        "lot_size": lot_size,
        "qty_per_leg": qty_per_leg,
    }

    if missing:
        return {
            **base_info,
            "error": f"missing_option_data: {', '.join(missing)}",
            "series": [],
            "entries": [],
            "day_pnl": 0.0,
        }

    merged = merge_basket_series(leg_candles_by_symbol)
    if not merged:
        return {
            **base_info,
            "error": "no_overlapping_bars_across_all_6_legs",
            "series": [],
            "entries": [],
            "day_pnl": 0.0,
        }

    # --- 3. Combined VWAP from market open -----------------------------------
    vwap_values = compute_cumulative_vwap(merged)
    for bar, vwap in zip(merged, vwap_values):
        bar["vwap"] = vwap

    signal_epoch = ist_to_epoch(ist_datetime(trade_date_str, config.SIGNAL_START_TIME))
    squareoff_epoch = ist_to_epoch(ist_datetime(trade_date_str, config.SQUARE_OFF_TIME))

    # --- 4. Walk the series: detect fresh downward crossings -> sell basket -
    entries = []
    was_below_vwap = False
    baskets_deployed = 0

    for bar in merged:
        if bar["epoch"] < signal_epoch:
            was_below_vwap = bar["price"] < bar["vwap"]
            continue
        if bar["epoch"] >= squareoff_epoch:
            break

        is_below = bar["price"] < bar["vwap"]
        fresh_cross_down = is_below and not was_below_vwap

        at_cap = (config.MAX_BASKETS_PER_DAY is not None
                  and baskets_deployed >= config.MAX_BASKETS_PER_DAY)

        if fresh_cross_down and not at_cap:
            baskets_deployed += 1
            entries.append({
                "basket_num": baskets_deployed,
                "entry_epoch": bar["epoch"],
                "entry_time": epoch_to_ist_time_str(bar["epoch"]),
                "entry_price": bar["price"],
            })

        was_below_vwap = is_below

    # --- 5. Square off all open baskets at 15:15 -----------------------------
    squareoff_bar = _find_spot_at(merged, squareoff_epoch)
    exit_price = squareoff_bar["price"] if squareoff_bar else None
    exit_time = epoch_to_ist_time_str(squareoff_bar["epoch"]) if squareoff_bar else config.SQUARE_OFF_TIME

    day_pnl = 0.0
    for e in entries:
        if exit_price is None:
            e["exit_price"] = None
            e["pnl"] = 0.0
            continue
        # SHORT basket (3 straddles, 6 legs): profit = (entry - exit) * qty_per_leg
        # (combined price already sums all 6 legs at 1 lot each, so a single
        # multiply by qty_per_leg gives total basket PnL)
        pnl = (e["entry_price"] - exit_price) * qty_per_leg
        e["exit_price"] = exit_price
        e["exit_time"] = exit_time
        e["pnl"] = pnl
        day_pnl += pnl

    return {
        **base_info,
        "series": merged,
        "entries": entries,
        "day_pnl": day_pnl,
        "num_baskets_deployed": baskets_deployed,
    }
