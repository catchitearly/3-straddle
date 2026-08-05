# Nifty 3-Straddle Basket VWAP Backtest & Live Trader

Backtests and live-trades a 3-straddle basket mean-reversion strategy on Nifty weekly options, using the Fyers v3 API. Pure Python throughout — no numpy/pandas.

**Note:** the batch backtest and the live paths run *different* strategies now:
- **Batch backtest** (`run_backtest.py`) — single 3-strike basket (ATM-100/ATM/ATM+100), 1-minute REST candles.
- **Live** (`live_notifier.py` cloud / `live_notifier_local.py` local) — **three independent, overlapping basket sets**, described below.

## Strategy (batch backtest)

1. **09:45 IST** — sample Nifty spot, round to nearest 100 → fixes the ATM strike. Two more strikes derived: **ATM-100** and **ATM+100** (`config.BACKTEST_STRIKE_OFFSETS`).
2. Build the **combined price** = sum of all 6 legs' 1-min closes, and its **combined VWAP** (cumulative, volume-weighted) from market open (09:15).
3. **Entry**: first bar at/after 09:45 enters if price < VWAP. After that, every fresh downward VWAP crossing deploys another basket — capped only by `config.MAX_BASKETS_PER_DAY` (default uncapped).
4. **Exit** per basket, whichever comes first: price crosses back **above VWAP**, price rallies through its **ATR trailing stop** (`config.ATR_MULTIPLIER` × ATR above the lowest price since entry), or **15:15 time exit**.

ATR (`compute_basket_atr` in `src/straddle_backtest.py`) is a close-to-close proxy on the combined series — not textbook ATR (no real high/low for a synthetic 6-leg sum), but a consistent volatility proxy for the trailing-stop distance.

### Tunables
- `config.BACKTEST_STRIKE_OFFSETS` — backtest-only basket offsets (default `[-100, 0, 100]`)
- `config.LOTS_PER_LEG_PER_BASKET`, `config.MAX_BASKETS_PER_DAY`
- `config.ATR_PERIOD` / `config.ATR_MULTIPLIER` (default 14-bar / 2.0x)

## Live: three basket sets

Both live paths run **three independent, overlapping basket sets** via `config.BASKET_SETS`:

```python
BASKET_SETS = {
    "A": [-100, 0, 100],
    "B": [-200, -100, 0],
    "C": [0, 100, 200],
}
```

With ATM = 24200: **Set A** = 24100/24200/24300, **Set B** = 24000/24100/24200, **Set C** = 24200/24300/24400 — **5 distinct strikes total** (ATM-200..ATM+200), each strike's CE/PE fetched/subscribed only once regardless of how many sets use it. Each set otherwise runs completely independently — own price, VWAP, entries/exits, PnL.

Telegram text messages (when enabled — see below) are labeled per set: `*SELL Set A basket #1*`, `*EXIT Set B basket #2*`. The dashboard shows **one price-vs-VWAP chart per set, no ATR chart** (ATR is still computed/used internally for the trailing stop, just not plotted). `config.MAX_BASKETS_PER_DAY` applies per set independently.

## Telegram behavior

**By default, `config.SEND_TRADE_TEXT_MESSAGES = False`** — entry/exit/strike-fix/heartbeat text alerts are **not** sent to Telegram, just printed to the console. **Only the dashboard HTML file itself** gets pushed to Telegram, as a document attachment, every `config.DASHBOARD_SEND_INTERVAL_SECONDS` (5 minutes by default) — it already shows every entry/exit and current PnL per set, so per-trade text pings are redundant with it.

Set `SEND_TRADE_TEXT_MESSAGES = True` in `config.py` if you want the old per-trade text alerts back — the dashboard-file push happens either way, independent of this flag.

## Setup

1. **Credentials — local runs**: `cp .env.example .env`, fill in `FYERS_APP_ID`, `FYERS_ACCESS_TOKEN`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`. Loaded automatically by `src/env_loader.py`. **Fyers tokens expire daily** — regenerate each morning.
2. **Credentials — cloud (GitHub Actions)**: same 4 values as repo Secrets (Settings → Secrets and variables → Actions).
3. **Expiry**: set `config.MANUAL_EXPIRY_CODE` to hardcode it (`"26804"` weekly-numeric or `"26JUL"` monthly-letter format — both work, just get concatenated into the Fyers symbol string). Set to `None` for automatic weekly calculation (`src/expiry_utils.get_weekly_expiry()`, using `WEEKLY_EXPIRY_SWITCH_DATE` / `NSE_HOLIDAYS`).
4. **GitHub Pages**: Settings → Pages → Source = **"Deploy from a branch"**, Branch = `main`, Folder = `/docs`. Both workflows run `render_pages.py` and commit `docs/` — no separate deploy step; GitHub republishes automatically.

## Running locally on Linux Mint (recommended over the cloud cron)

`live_notifier_local.py` runs continuously on your machine using the **Fyers WebSocket**, building candles at `config.LIVE_BAR_SECONDS` resolution (**5 seconds by default**) — no REST polling, no GitHub Actions, no cron-job.org.

**Entry/exit/Telegram logic runs through the exact same code** as the cloud version (`live_notifier.py`) — both call `src/live_engine.py`. What differs: data source, and bar resolution (this path: 5-second bars via `LIVE_BAR_SECONDS`/`LIVE_ATR_PERIOD`; cloud + batch backtest: 1-minute bars). Deliberate divergence — expect more, faster round-trips here than the 1-min backtest predicts.

**Safe to start any time before market open** (e.g. 07:00) — idles with no network connection until 09:15, then connects.

```bash
cd nifty-straddle-vwap
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env && nano .env   # fill in credentials

# every trading morning: refresh FYERS_ACCESS_TOKEN in .env, then:
python3 live_notifier_local.py
```

Keep it running via `tmux` (`tmux new -s straddle`, detach with Ctrl-b d) or a systemd user service. If killed/crashed, just re-run — resumes from `data/live_state.json` rather than re-fixing strikes or re-entering baskets that already fired.

### Where to see the dashboard

`docs/live.html` regenerates automatically every time state saves — open it directly:
```bash
xdg-open docs/live.html
```
Auto-refreshes every 60s, one section per basket set with its own price-vs-VWAP chart (entry markers red/down, exit markers green/up) and entries table. It's also pushed to Telegram as a document every 5 minutes, so you can check it from a phone.

If you also want the full backtest dashboard locally: `python3 render_pages.py` after a `run_backtest.py` run.

### Things worth double-checking before real trading

- **Tick field names** in `src/fyers_ws_client.py`'s `_parse_tick()` are best-effort (`ltp`/`vol_traded_today`) — watch for `WARNING: ws tick missing symbol/ltp keys` on first run; adjust if your fyers-apiv3 version uses different keys.
- **Per-bar volume** is derived by diffing cumulative day-volume across bar boundaries — matters more at 5-second bars than 1-minute (less volume per bar to smooth over a missed tick).
- Test without real API calls: `python3 test_live_notifier_local.py` (fake clock + synthetic data, no real network).

## Testing outside market hours with real data

`replay_real_day.py` replays a real day (today once closed, or any past date) through the **actual live pipeline** — real Fyers REST data:
```bash
python3 replay_real_day.py          # replays today
python3 replay_real_day.py 2026-07-21
```
Fyers' REST API always returns a closed day's *entire* set of candles regardless of what time you call it — so this fetches each symbol's full day once, then feeds `live_notifier.py` only the portion up to a simulated "now" as it steps forward, for a genuine minute-by-minute replay. Writes to a separate `data/live_state_replay.json` — never touches your real state.

## Known caveats

- **Option history depth**: Fyers typically retains intraday option data only ~60-90 days.
- **Lot size**: defaults to 65 (`config.LOT_SIZE_BY_DATE` for historical changes).
- **Expiry weekday cutover**: confirm `WEEKLY_EXPIRY_SWITCH_DATE` (2025-09-01, Thursday→Tuesday) against the actual NSE circular.
- **No slippage/costs modeled** in the batch backtest.
- **VWAP fallback**: if volume is zero in early bars, falls back to a simple running average (`src/vwap.py`).

## Project layout

```
config.py                       # all tunables
src/ist_time.py                  # timezone-safe IST helpers
src/expiry_utils.py              # expiry calc + Fyers symbol construction (+ manual override)
src/vwap.py                      # pure-python cumulative VWAP
src/fyers_client.py              # Fyers v3 REST history client + caching
src/fyers_ws_client.py           # Fyers websocket wrapper - builds candles from live ticks
src/straddle_backtest.py         # batch backtest engine (single basket, 1-min)
src/dashboard_generator.py       # docs/index.html - backtest dashboard, tabs per day
src/live_engine.py                # SHARED entry/exit/Telegram logic - three basket sets
src/live_dashboard.py             # docs/live.html - one chart per basket set
src/telegram_notifier.py         # Telegram message + document sender (text gated by config flag)
src/env_loader.py                 # tiny .env loader, no dependency
run_backtest.py                  # batch backtest entrypoint
live_notifier.py                 # cloud/REST live notifier (cron-job.org triggered)
live_notifier_local.py           # local websocket live notifier (run on your PC)
render_pages.py                   # rebuilds docs/index.html + docs/live.html together
replay_real_day.py                # replay a real day through the live pipeline, real data
test_synthetic.py                # batch backtest smoke test, fake data
test_live_notifier.py             # cloud/REST smoke test, fake data
test_live_notifier_local.py       # local websocket smoke test, fake data
.github/workflows/backtest.yml           # CI: batch backtest -> commit docs/
.github/workflows/live_notifier.yml      # CI: cron-job.org trigger -> commit docs/ + state
```
