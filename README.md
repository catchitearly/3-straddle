# Nifty Straddle-VWAP Backtest

Backtests a short-straddle mean-reversion strategy on Nifty weekly options using the Fyers v3 history API, with a Plotly dashboard (tabs per day) auto-deployed to GitHub Pages via GitHub Actions.

## Strategy

1. **09:45 IST** — sample Nifty spot, round to nearest 100 (both directions) → fixes the ATM strike for the day.
2. Build the **combined straddle** (ATM CE + ATM PE) 1-min price series for the full day, and its **cumulative VWAP** starting from market open (09:15).
3. From 09:45 onward, every **fresh downward crossing** of the straddle price below its own VWAP → **sell 1 straddle** (1 lot CE + 1 lot PE), up to a cap of **3 straddles/day**.
4. All open straddles are **squared off together at 15:15** — pure time exit, no intraday stop/target.

### Design decision worth double-checking
You gave two answers that needed reconciling: "re-enter every time it dips below VWAP" + "3 lots total exposure". I implemented this as: **each fresh VWAP cross sells 1 straddle, capped at 3 for the day**, all closed together at 15:15. If you actually meant something else (e.g. exit+re-enter each time, always at 3 lots per entry, or a cap higher/lower than 3), it's a one-line change:
- `config.MAX_STRADDLES_PER_DAY` — total cap
- `config.LOTS_PER_STRADDLE_ENTRY` — lots per leg per entry
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

3. **Set your backtest range** in `config.py` (`BACKTEST_START_DATE` / `BACKTEST_END_DATE`), or pass them as workflow inputs when manually triggering the Action.

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
