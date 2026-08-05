"""
Core backtest engine - 3-straddle BASKET strategy with ATR Trailing Stop.

Key Features:
- First Entry at 09:45 if price < VWAP.
- Fresh downward crossings (was >= VWAP, now < VWAP) deploy new baskets.
- Exits when price > VWAP OR when ATR Trailing Stop is hit OR at 15:15 IST.

This is the BATCH backtest engine - single 3-strike basket
(config.BACKTEST_STRIKE_OFFSETS), 1-minute REST candles. The live paths
(live_notifier.py / live_notifier_local.py) run a different, multi-set
strategy via src/live_engine.py - see README's "Live: three basket sets"
section.
"""

from src.ist_time import ist_datetime, ist_to_epoch, epoch_to_ist_time_str
from src.vwap import compute_cumulative_vwap
from src.expiry_utils import round_to_nearest_strike, build_option_symbol
import config


def compute_basket_atr(merged_series, period=14):
    """
    Computes ATR on the merged basket price series.
    True Range for short basket = max(High-Low, abs(High-PrevClose), abs(Low-PrevClose))
    Since 1-min candles in merge series only have close prices, High/Low are estimated.
    """
    atr_values = []
    prices = [bar["price"] for bar in merged_series]

    for i in range(len(prices)):
        if i < period:
            # Fallback estimation for initial bars: 1% of basket price as baseline volatility
            atr_values.append(prices[i] * 0.01)
            continue

        # Calculate Average True Range over period
        tr_list = [abs(prices[j] - prices[j-1]) for j in range(i - period + 1, i + 1)]
        atr = sum(tr_list) / period
        atr_values.append(atr)

    return atr_values


def merge_basket_series(leg_candles_by_symbol):
    """
    leg_candles_by_symbol: dict of symbol -> list of candle dicts.

    Merge on epochs common to ALL legs into a combined basket series:
        [{"epoch", "price" (sum of closes), "volume" (sum of volumes),
          "legs": {symbol: close, ...}}, ...]
    Timestamps missing from any single leg are dropped (illiquid/missing minute).
    """
    symbols = list(leg_candles_by_symbol.keys())
    if not symbols:
        return []

    by_epoch = {sym: {c["epoch"]: c for c in candles}
                for sym, candles in leg_candles_by_symbol.items()}
    common_epochs = set.intersection(*(set(by_epoch[sym].keys()) for sym in symbols))

    merged = []
    for epoch in sorted(common_epochs):
        total_price = sum(by_epoch[sym][epoch]["close"] for sym in symbols)
        total_vol = sum(by_epoch[sym][epoch].get("volume") or 0 for sym in symbols)
        legs = {sym: by_epoch[sym][epoch]["close"] for sym in symbols}
        merged.append({"epoch": epoch, "price": total_price, "volume": total_vol, "legs": legs})

    return merged


def run_day_backtest(trade_date_str, fyers_client, atr_multiplier=None):
    """
    atr_multiplier: defaults to config.ATR_MULTIPLIER (2.0) - provides a good
                   balance between avoiding noise stop-outs and protecting
                   peak profits.
    """
    atr_multiplier = config.ATR_MULTIPLIER if atr_multiplier is None else atr_multiplier
    atr_period = config.ATR_PERIOD

    lot_size = config.get_lot_size(trade_date_str)
    qty_per_leg = config.LOTS_PER_LEG_PER_BASKET * lot_size

    # --- 1. Fetch Spot & Fix Strikes ---
    spot_candles = fyers_client.get_day_candles(config.UNDERLYING_SYMBOL, trade_date_str)
    if not spot_candles:
        return None

    fix_epoch = ist_to_epoch(ist_datetime(trade_date_str, config.STRIKE_FIX_TIME))
    spot_bar = next((c for c in reversed(spot_candles) if c["epoch"] <= fix_epoch), None)
    if spot_bar is None:
        return None

    atm_strike = round_to_nearest_strike(spot_bar["close"])
    strikes = sorted(atm_strike + off for off in config.BACKTEST_STRIKE_OFFSETS)

    # --- 2. Build Option Symbols & Fetch Candles ---
    leg_symbols = {}
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
        return {**base_info, "error": f"missing_option_data: {', '.join(missing)}", "series": [], "entries": [], "day_pnl": 0.0}

    merged = merge_basket_series(leg_candles_by_symbol)
    if not merged:
        return {**base_info, "error": "no_overlapping_bars", "series": [], "entries": [], "day_pnl": 0.0}

    # --- 3. Compute VWAP & ATR ---
    vwap_values = compute_cumulative_vwap(merged)
    atr_values = compute_basket_atr(merged, period=atr_period)

    for bar, vwap, atr in zip(merged, vwap_values, atr_values):
        bar["vwap"] = vwap
        bar["atr"] = atr

    signal_epoch = ist_to_epoch(ist_datetime(trade_date_str, config.SIGNAL_START_TIME))
    squareoff_epoch = ist_to_epoch(ist_datetime(trade_date_str, config.SQUARE_OFF_TIME))

    # --- 4. Main Simulation Loop ---
    entries = []
    active_baskets = []  # dicts tracking position state
    was_below_vwap = False
    baskets_deployed = 0
    first_signal_bar_processed = False

    for bar in merged:
        current_epoch = bar["epoch"]
        price = bar["price"]
        vwap = bar["vwap"]
        atr = bar["atr"]

        if current_epoch < signal_epoch:
            was_below_vwap = price < vwap
            continue

        # 15:15 Time Exit
        if current_epoch >= squareoff_epoch:
            if active_baskets:
                for b in active_baskets:
                    e = entries[b["idx"]]
                    e["exit_price"] = price
                    e["exit_time"] = epoch_to_ist_time_str(current_epoch)
                    e["exit_reason"] = "15:15 Time Exit"
                    e["pnl"] = (e["entry_price"] - price) * qty_per_leg
                active_baskets = []
            break

        is_below = price < vwap
        is_above = price > vwap

        # --- Update Active Trailing Stops & Check Trailing Exit ---
        remaining_active = []
        for b in active_baskets:
            idx = b["idx"]

            # Record lowest price reached (High-Water Mark for Short trade)
            if price < b["min_price"]:
                b["min_price"] = price
                # Update trailing stop line: min_price + (2.0 * ATR)
                b["trailing_stop"] = b["min_price"] + (atr_multiplier * atr)

            # CHECK EXITS
            # Exit 1: VWAP Cross Exit
            if is_above:
                e = entries[idx]
                e["exit_price"] = price
                e["exit_time"] = epoch_to_ist_time_str(current_epoch)
                e["exit_reason"] = "Price > VWAP Exit"
                e["pnl"] = (e["entry_price"] - price) * qty_per_leg

            # Exit 2: Trailing Stop Hit (Price bounced up above trailing stop)
            elif price >= b["trailing_stop"]:
                e = entries[idx]
                e["exit_price"] = price
                e["exit_time"] = epoch_to_ist_time_str(current_epoch)
                e["exit_reason"] = f"Trailing Stop Hit ({atr_multiplier}x ATR)"
                e["pnl"] = (e["entry_price"] - price) * qty_per_leg

            else:
                remaining_active.append(b)

        active_baskets = remaining_active

        # --- Entry Logic ---
        at_cap = (config.MAX_BASKETS_PER_DAY is not None and baskets_deployed >= config.MAX_BASKETS_PER_DAY)
        should_entry = False

        if not first_signal_bar_processed:
            first_signal_bar_processed = True
            if is_below and not at_cap:
                should_entry = True
        elif is_below and not was_below_vwap and not at_cap:
            should_entry = True

        if should_entry:
            baskets_deployed += 1
            e_dict = {
                "basket_num": baskets_deployed,
                "entry_epoch": current_epoch,
                "entry_time": epoch_to_ist_time_str(current_epoch),
                "entry_price": price,
                "exit_price": None,
                "exit_time": None,
                "exit_reason": None,
                "pnl": 0.0,
            }
            entries.append(e_dict)

            # Track min_price and trailing stop level
            active_baskets.append({
                "idx": len(entries) - 1,
                "min_price": price,
                "trailing_stop": price + (atr_multiplier * atr)
            })

        was_below_vwap = is_below

    day_pnl = sum(e["pnl"] for e in entries)

    return {
        **base_info,
        "series": merged,
        "entries": entries,
        "day_pnl": day_pnl,
        "num_baskets_deployed": baskets_deployed,
    }
