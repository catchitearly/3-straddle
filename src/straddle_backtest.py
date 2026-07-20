"""
Core backtest engine - 3-straddle BASKET strategy.
"""

from src.ist_time import ist_datetime, ist_to_epoch, epoch_to_ist_time_str
from src.vwap import compute_cumulative_vwap
from src.expiry_utils import round_to_nearest_strike, build_option_symbol
import config


def _find_spot_at(candles, target_epoch):
    before = [c for c in candles if c["epoch"] <= target_epoch]
    if before:
        return before[-1]
    after = [c for c in candles if c["epoch"] > target_epoch]
    return after[0] if after else None


def merge_basket_series(leg_candles_by_symbol):
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
        total_price = sum(by_epoch[sym][epoch]["close"] for sym in symbols)
        total_vol = sum(by_epoch[sym][epoch].get("volume") or 0 for sym in symbols)
        legs = {sym: by_epoch[sym][epoch]["close"] for sym in symbols}
        merged.append({"epoch": epoch, "price": total_price, "volume": total_vol, "legs": legs})

    return merged


def run_day_backtest(trade_date_str, fyers_client):
    """
    Runs backtest for a single trading day.
    """
    lot_size = config.get_lot_size(trade_date_str)
    qty_per_leg = config.LOTS_PER_LEG_PER_BASKET * lot_size

    # 1. Spot at 09:45 IST -> Fix ATM Strike
    spot_candles = fyers_client.get_day_candles(config.UNDERLYING_SYMBOL, trade_date_str)
    if not spot_candles:
        return None

    fix_epoch = ist_to_epoch(ist_datetime(trade_date_str, config.STRIKE_FIX_TIME))
    spot_bar = _find_spot_at(spot_candles, fix_epoch)
    if spot_bar is None:
        return None

    atm_strike = round_to_nearest_strike(spot_bar["close"])
    strikes = sorted(atm_strike + off for off in config.STRIKE_OFFSETS)

    # 2. Build 6 option symbols & fetch data
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

    # 3. Cumulative VWAP calculation
    vwap_values = compute_cumulative_vwap(merged)
    for bar, vwap in zip(merged, vwap_values):
        bar["vwap"] = vwap

    signal_epoch = ist_to_epoch(ist_datetime(trade_date_str, config.SIGNAL_START_TIME))
    squareoff_epoch = ist_to_epoch(ist_datetime(trade_date_str, config.SQUARE_OFF_TIME))

    # 4. Simulation Walk
    entries = []
    active_baskets = []
    was_below_vwap = False
    baskets_deployed = 0
    first_signal_bar_processed = False

    for bar in merged:
        current_epoch = bar["epoch"]
        price = bar["price"]
        vwap = bar["vwap"]

        if current_epoch < signal_epoch:
            was_below_vwap = price < vwap
            continue

        # 15:15 IST Time Exit
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

        # Dynamic Exit: price > VWAP
        if is_above and active_baskets:
            for idx in active_baskets:
                pnl = (entries[idx]["entry_price"] - price) * qty_per_leg
                entries[idx]["exit_price"] = price
                entries[idx]["exit_time"] = epoch_to_ist_time_str(current_epoch)
                entries[idx]["exit_reason"] = "Price > VWAP Exit"
                entries[idx]["pnl"] = pnl
            active_baskets = []

        # Entry logic
        at_cap = (config.MAX_BASKETS_PER_DAY is not None
                  and baskets_deployed >= config.MAX_BASKETS_PER_DAY)

        should_entry = False
        if not first_signal_bar_processed:
            first_signal_bar_processed = True
            if is_below and not at_cap:
                should_entry = True
        elif is_below and not was_below_vwap and not at_cap:
            should_entry = True

        if should_entry:
            baskets_deployed += 1
            entries.append({
                "basket_num": baskets_deployed,
                "entry_epoch": current_epoch,
                "entry_time": epoch_to_ist_time_str(current_epoch),
                "entry_price": price,
                "exit_price": None,
                "exit_time": None,
                "exit_reason": None,
                "pnl": 0.0,
            })
            active_baskets.append(len(entries) - 1)

        was_below_vwap = is_below

    day_pnl = sum(e["pnl"] for e in entries)

    return {
        **base_info,
        "series": merged,
        "entries": entries,
        "day_pnl": day_pnl,
        "num_baskets_deployed": baskets_deployed,
    }
