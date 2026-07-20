"""
Core backtest engine - 3-straddle BASKET strategy.

Strategy Rules:
--------------
1. At 09:45 IST, sample Nifty spot, round to nearest 100 to fix ATM strike.
   Strikes used: ATM-100, ATM, ATM+100 (3 straddles = 6 legs).
2. Build COMBINED price series = sum of 6 legs' close prices and cumulative VWAP.
3. Entry Criteria:
   - At 09:45 IST (exact signal bar), if price < VWAP -> SELL basket.
   - If no entry at 09:45 or after an exit, check for FRESH downward crossing of VWAP
     (was >= VWAP on previous bar, now < VWAP).
   - Can entry multiple times if capped by config.MAX_BASKETS_PER_DAY.
4. Exit Criteria:
   - Dynamic Exit: Whenever price crosses ABOVE VWAP (price > VWAP), exit ALL open positions.
   - Time Exit: Force exit all open positions at 15:15 IST.
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
    Timestamps missing from any single leg are dropped.
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
    Run the basket strategy for a single trading day with modified VWAP exit and entry logic.
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

    # --- 4. Main Simulation Walk ---------------------------------------------
    entries = []
    active_baskets = []  # Track indices of open entries in `entries`
    was_below_vwap = False
    baskets_deployed = 0
    first_signal_bar_processed = False

    for bar in merged:
        current_epoch = bar["epoch"]
        price = bar["price"]
        vwap = bar["vwap"]

        # Track VWAP position before 09:45 IST
        if current_epoch < signal_epoch:
            was_below_vwap = price < vwap
            continue

        # Force time exit at 15:15 IST
        if current_epoch >= squareoff_epoch:
            if active_baskets:
                for idx in active_baskets:
                    pnl = (entries[idx]["entry_price"] - price) * qty_per_leg
                    entries[idx]["exit_price"] = price
                    entries[idx]["exit_time"] = epoch_to_ist_time_str(current_epoch)
                    entries[idx]["exit_reason"] = "15:15 Time Exit"
                    entries[idx]["pnl"] = pnl
                active_baskets = []
            break

        is_below = price < vwap
        is_above = price > vwap

        # --- A. Dynamic Exit: Exit when price > VWAP ---
        if is_above and active_baskets:
            for idx in active_baskets:
                pnl = (entries[idx]["entry_price"] - price) * qty_per_leg
                entries[idx]["exit_price"] = price
                entries[idx]["exit_time"] = epoch_to_ist_time_str(current_epoch)
                entries[idx]["exit_reason"] = "Price > VWAP Exit"
                entries[idx]["pnl"] = pnl
            active_baskets = []

        # --- B. Entry Logic ---
        at_cap = (config.MAX_BASKETS_PER_DAY is not None
                  and baskets_deployed >= config.MAX_BASKETS_PER_DAY)

        should_entry = False

        # 1. First candle at/after 09:45 IST
        if not first_signal_bar_processed:
            first_signal_bar_processed = True
            if is_below and not at_cap:
                should_entry = True

        # 2. Subsequent fresh downward crossings (was >= VWAP, now < VWAP)
        elif is_below and not was_below_vwap and not at_cap:
            should_entry = True

        if should_entry:
            baskets_deployed += 1
            entry_data = {
                "basket_num": baskets_deployed,
                "entry_epoch": current_epoch,
                "entry_time": epoch_to_ist_time_str(current_epoch),
                "entry_price": price,
                "exit_price": None,
                "exit_time": None,
                "exit_reason": None,
                "pnl": 0.0,
            }
            entries.append(entry_data)
            active_baskets.append(len(entries) - 1)

        was_below_vwap = is_below

    # --- 5. Clean up open positions if day ends unexpectedly before 15:15 ----
    if active_baskets and merged:
        last_bar = merged[-1]
        for idx in active_baskets:
            pnl = (entries[idx]["entry_price"] - last_bar["price"]) * qty_per_leg
            entries[idx]["exit_price"] = last_bar["price"]
            entries[idx]["exit_time"] = epoch_to_ist_time_str(last_bar["epoch"])
            entries[idx]["exit_reason"] = "End of Day Market Close"
            entries[idx]["pnl"] = pnl

    day_pnl = sum(e["pnl"] for e in entries)

    return {
        **base_info,
        "series": merged,
        "entries": entries,
        "day_pnl": day_pnl,
        "num_baskets_deployed": baskets_deployed,
    }
