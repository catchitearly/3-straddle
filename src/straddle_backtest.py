"""
Comparative Backtest Framework for 3-Straddle Basket Strategy.

Compares 4 Stop Loss Methods:
1. No SL / Standard (VWAP Exit + 15:15 Time Exit only)
2. Plain Fixed Stop Loss (e.g., 30 pts adverse move)
3. Volatility ATR Trailing Stop (2.0x ATR multiplier)
4. Tiered Peak Profit Lock TSL (Approach 2 with +10 / +30 / +50 pt tiers)
"""

from src.ist_time import ist_datetime, ist_to_epoch, epoch_to_ist_time_str
from src.vwap import compute_cumulative_vwap
from src.expiry_utils import round_to_nearest_strike, build_option_symbol
import config


def compute_basket_atr(merged_series, period=14):
    prices = [bar["price"] for bar in merged_series]
    atr_values = []
    for i in range(len(prices)):
        if i < period:
            atr_values.append(prices[i] * 0.01)
            continue
        tr_list = [abs(prices[j] - prices[j - 1]) for j in range(i - period + 1, i + 1)]
        atr_values.append(sum(tr_list) / period)
    return atr_values


def run_day_backtest_with_sl(trade_date_str, fyers_client, sl_mode="NONE", sl_params=None):
    """
    sl_mode options:
      - "NONE": VWAP Cross + 15:15 Exit only
      - "PLAIN_SL": Fixed point stop-loss (e.g., sl_params={"sl_pts": 30.0})
      - "ATR_TSL": Dynamic ATR Trailing Stop (e.g., sl_params={"multiplier": 2.0})
      - "TIERED_TSL": High-Water Mark tiered profit lock
    """
    if sl_params is None:
        sl_params = {}

    lot_size = config.get_lot_size(trade_date_str)
    qty_per_leg = config.LOTS_PER_LEG_PER_BASKET * lot_size

    # 1. Fetch Spot & Fix ATM Strikes
    spot_candles = fyers_client.get_day_candles(config.UNDERLYING_SYMBOL, trade_date_str)
    if not spot_candles:
        return None

    fix_epoch = ist_to_epoch(ist_datetime(trade_date_str, config.STRIKE_FIX_TIME))
    spot_bar = next((c for c in reversed(spot_candles) if c["epoch"] <= fix_epoch), None)
    if spot_bar is None:
        return None

    atm_strike = round_to_nearest_strike(spot_bar["close"])
    strikes = sorted(atm_strike + off for off in config.STRIKE_OFFSETS)

    # 2. Build Options Symbols & Fetch Candles
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

    if missing:
        return None

    # Merge into basket series
    symbols = list(leg_symbols.keys())
    by_epoch = {sym: {c["epoch"]: c for c in candles} for sym, candles in leg_candles_by_symbol.items()}
    common_epochs = set.intersection(*(set(by_epoch[sym].keys()) for sym in symbols)) if symbols else set()

    merged = []
    for epoch in sorted(common_epochs):
        total_price = sum(by_epoch[sym][epoch]["close"] for sym in symbols)
        total_vol = sum(by_epoch[sym][epoch].get("volume") or 0 for sym in symbols)
        merged.append({"epoch": epoch, "price": total_price, "volume": total_vol})

    if not merged:
        return None

    # 3. Compute VWAP & ATR
    vwap_values = compute_cumulative_vwap(merged)
    atr_values = compute_basket_atr(merged, period=14)
    for bar, vwap, atr in zip(merged, vwap_values, atr_values):
        bar["vwap"] = vwap
        bar["atr"] = atr

    signal_epoch = ist_to_epoch(ist_datetime(trade_date_str, config.SIGNAL_START_TIME))
    squareoff_epoch = ist_to_epoch(ist_datetime(trade_date_str, config.SQUARE_OFF_TIME))

    entries = []
    active_baskets = []
    was_below_vwap = False
    baskets_deployed = 0
    first_signal_bar_processed = False

    # 4. Simulation Walk
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

        # --- Evaluate Active Positions against SL / TSL ---
        remaining_active = []
        for b in active_baskets:
            idx = b["idx"]
            entry_p = b["entry_price"]
            peak_gain = entry_p - b["min_price"]  # High water mark (gain in points)
            current_gain = entry_p - price

            # Update tracking minimum price
            if price < b["min_price"]:
                b["min_price"] = price
                peak_gain = entry_p - price

            stop_triggered = False
            exit_reason = ""

            # Check Exit Criteria
            # 1. Base VWAP Exit
            if is_above:
                stop_triggered = True
                exit_reason = "Price > VWAP Exit"

            # 2. Plain Fixed SL
            elif sl_mode == "PLAIN_SL":
                sl_pts = sl_params.get("sl_pts", 30.0)
                if (price - entry_p) >= sl_pts:
                    stop_triggered = True
                    exit_reason = f"Plain SL Hit (+{sl_pts} pts loss)"

            # 3. ATR Trailing Stop
            elif sl_mode == "ATR_TSL":
                mult = sl_params.get("multiplier", 2.0)
                tsl_level = b["min_price"] + (mult * atr)
                if price >= tsl_level:
                    stop_triggered = True
                    exit_reason = f"ATR TSL Hit ({mult}x ATR)"

            # 4. Approach 2: Tiered High-Water Mark TSL
            elif sl_mode == "TIERED_TSL":
                tsl_level = None
                
                # Level 3: Peak gain >= +50 pts -> Lock 70% of peak profit
                if peak_gain >= 50.0:
                    locked_pts = peak_gain * 0.70
                    tsl_level = entry_p - locked_pts
                    
                # Level 2: Peak gain >= +30 pts -> Lock 50% of peak profit
                elif peak_gain >= 30.0:
                    locked_pts = peak_gain * 0.50
                    tsl_level = entry_p - locked_pts

                # Level 1: Peak gain >= +10 pts -> Move SL to Breakeven (+2 pts profit lock)
                elif peak_gain >= 10.0:
                    tsl_level = entry_p - 2.0

                if tsl_level is not None and price >= tsl_level:
                    stop_triggered = True
                    exit_reason = f"Tiered Profit Lock Hit (Peak Gain: +{peak_gain:.1f} pts)"

            if stop_triggered:
                e = entries[idx]
                e["exit_price"] = price
                e["exit_time"] = epoch_to_ist_time_str(current_epoch)
                e["exit_reason"] = exit_reason
                e["pnl"] = (entry_p - price) * qty_per_leg
            else:
                remaining_active.append(b)

        active_baskets = remaining_active

        # --- Check Entry Signals ---
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
            active_baskets.append({
                "idx": len(entries) - 1,
                "entry_price": price,
                "min_price": price
            })

        was_below_vwap = is_below

    day_pnl = sum(e["pnl"] for e in entries)
    return {"entries": entries, "day_pnl": day_pnl}


def calculate_metrics(all_entries):
    if not all_entries:
        return {"total_pnl": 0.0, "win_rate": 0.0, "max_dd": 0.0, "profit_factor": 0.0, "total_trades": 0}

    total_pnl = sum(e["pnl"] for e in all_entries)
    wins = [e["pnl"] for e in all_entries if e["pnl"] > 0]
    losses = [abs(e["pnl"]) for e in all_entries if e["pnl"] < 0]

    win_rate = (len(wins) / len(all_entries)) * 100 if all_entries else 0.0
    profit_factor = (sum(wins) / sum(losses)) if sum(losses) > 0 else float("inf")

    # Max Drawdown calculation
    cum_pnl = 0.0
    peak = 0.0
    max_dd = 0.0
    for e in all_entries:
        cum_pnl += e["pnl"]
        if cum_pnl > peak:
            peak = cum_pnl
        dd = peak - cum_pnl
        if dd > max_dd:
            max_dd = dd

    return {
        "total_pnl": total_pnl,
        "win_rate": win_rate,
        "max_dd": max_dd,
        "profit_factor": profit_factor,
        "total_trades": len(all_entries),
    }


def generate_comparative_report(trading_days, fyers_client):
    """
    Runs all 4 SL configurations across all backtest days and outputs a comparison table.
    """
    configs = [
        ("No SL (VWAP Exit Only)", "NONE", {}),
        ("Plain Fixed SL (30 pts)", "PLAIN_SL", {"sl_pts": 30.0}),
        ("ATR Trailing Stop (2.0x ATR)", "ATR_TSL", {"multiplier": 2.0}),
        ("Tiered Profit Lock (Approach 2)", "TIERED_TSL", {}),
    ]

    results = {}

    for name, mode, params in configs:
        all_entries = []
        for day in trading_days:
            res = run_day_backtest_with_sl(day, fyers_client, sl_mode=mode, sl_params=params)
            if res and res["entries"]:
                all_entries.extend(res["entries"])
        results[name] = calculate_metrics(all_entries)

    # Output Comparative Markdown Table
    print("\n### Strategy Comparative Results Across Tested Period\n")
    print("| Stop Loss Strategy | Total Trades | Win Rate (%) | Total PnL (₹) | Max Drawdown (₹) | Profit Factor |")
    print("| :--- | :---: | :---: | :---: | :---: | :---: |")
    for name, m in results.items():
        pf_str = f"{m['profit_factor']:.2f}" if m['profit_factor'] != float("inf") else "∞"
        print(f"| **{name}** | {m['total_trades']} | {m['win_rate']:.1f}% | ₹{m['total_pnl']:,.2f} | ₹{m['max_dd']:,.2f} | {pf_str} |")
