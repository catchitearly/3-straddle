# Nifty 3-Straddle Basket VWAP Backtest

Backtests a 3-straddle basket mean-reversion strategy on Nifty weekly options using the Fyers v3 history API, with a Plotly dashboard (tabs per day) auto-deployed to GitHub Pages via GitHub Actions.

## Strategy

1. **09:45 IST** — sample Nifty spot, round to nearest 100 (both directions) → fixes the ATM strike for the day. Two more strikes are derived: **ATM-100** and **ATM+100** (`config.STRIKE_OFFSETS`).
2. Build the **combined price** series = sum of all 6 legs' 1-min close prices (ATM-100 CE+PE, ATM CE+PE, ATM+100 CE+PE), and its **combined VWAP** (cumulative, using summed volume across all 6 legs) starting from market open (09:15).
3. From 09:45 onward, every **fresh downward crossing** of the combined price below its own combined VWAP → **sell the full basket**: 1 lot each of the ATM-100, ATM, and ATM+100 straddles (6 legs, 1 lot each). This **repeats on every fresh cross** — exposure compounds through the day (no cap by default).
4. All open baskets are **squared off together at 15:15** — pure time exit, no intraday stop/target.

### Tunables
- `config.STRIKE_OFFSETS` — which strikes make up the basket relative to ATM (default `[-100, 0, 100]`)
- `config.LOTS_PER_LEG_PER_BASKET` — lots per leg per basket (default 1)
- `config.MAX_BASKETS_PER_DAY` — cap on baskets sold per day; `None` = unlimited (default). Worth considering setting a cap here given exposure compounds uncapped by default — a few bad reversal days could stack up fast.
- The crossing logic itself is in `src/straddle_backtest.py`, `run_day_backtest()`, the "walk the series" loop.

## Known caveats (please verify before trusting PnL numbers)

- **Option history depth**: Fyers typically retains intraday historical data for options only for a limited lookback (commonly ~60-90 days). If you backtest further back, days will come back with `error: missing_option_data` in the dashboard rather than silently faking numbers.
- **Lot size**: defaults to 65 (current). If your backtest window spans a lot-size change, fill in `config.LOT_SIZE_BY_DATE`.
- **Expiry weekday switch**: Tuesday-expiry cutover is set to 2025-09-01 in `config.WEEKLY_EXPIRY_SWITCH_DATE` — this is my best understanding from your notes, but please confirm against the actual NSE circular date for your backtest range, especially around the transition week itself.
- **Holidays**: `config.NSE_HOLIDAYS` is empty by default. An expiry that falls on an unlisted holiday will be computed one day later than it should be. Worth populating this list for the exchange calendar covering your backtest range.
- **No slippage/costs modeled**: PnL is pure (entry - exit) × quantity, no brokerage, STT, or slippage. Easy to bolt on in `run_day_backtest()` if you want realism.
- **VWAP fallback**: if the combined-leg volume is zero in the earliest bars (illiquid open), VWAP falls back to a simple running average of price rather than dividing by zero — see `src/vwap.py`.

## Setup

1. **Secrets**: In your GitHub repo → Settings → Secrets and variables → Actions, add:
   - `FYERS_APP_ID`
   - `FYERS_ACCESS_TOKEN`
   (You mentioned you already have these — same convention as your other Fyers projects.)

2. **Enable Pages**: Settings → Pages → Source = "GitHub Actions".

3. **Backtest range** is already set to `2026-07-14` → `2026-07-20` in `config.py` (`BACKTEST_START_DATE` / `BACKTEST_END_DATE`) — change it there, or pass different dates as workflow inputs when manually triggering the Action. Expiry for this window resolves to 2026-07-21 (Tuesday), i.e. `26721` in Fyers' weekly code — confirmed this matches by running `src/expiry_utils.get_weekly_expiry()` directly.

4. **Run locally first** (recommended, to catch symbol/expiry issues before burning API calls in CI):
   ```bash
   pip install -r requirements.txt
   export FYERS_APP_ID=...
   export FYERS_ACCESS_TOKEN=...
   python run_backtest.py 2025-11-01 2025-11-30
   open output/index.html
   ```

5. **Run in CI**: Actions tab → "Straddle VWAP Backtest" → Run workflow, optionally overriding the date range. The historical candle cache (`data/cache/`) and `output/results.json` get committed back to the repo so re-runs don't re-fetch days you've already pulled; the dashboard gets deployed to Pages automatically.

## Project layout

```
config.py                    # all tunables: sizing, times, expiry rules, paths
src/ist_time.py               # timezone-safe IST datetime helpers (the UTC/IST bug fix)
src/expiry_utils.py           # weekly expiry date + Fyers option symbol construction
src/vwap.py                   # pure-python cumulative VWAP
src/fyers_client.py           # Fyers v3 history API wrapper + local JSON caching
src/straddle_backtest.py      # core strategy engine (one day at a time)
src/dashboard_generator.py    # Plotly HTML dashboard, tabs per day, lazy rendering
run_backtest.py                # main entrypoint - loops over the date range
test_synthetic.py              # smoke test with fabricated data, no API calls needed
.github/workflows/backtest.yml # CI: run backtest -> deploy to Pages
```

No numpy/pandas anywhere in core logic, per your usual constraint — everything is pure Python (VWAP, PnL, expiry math all hand-rolled).

## Testing without live API access

`test_synthetic.py` fabricates a plausible CE/PE price path (decaying + oscillating) and runs it through the *real* backtest engine and dashboard generator, so you can sanity-check the VWAP crossing / entry / exit / PnL logic and the dashboard rendering before spending real API calls:

```bash
python test_synthetic.py
open output/index.html
```
