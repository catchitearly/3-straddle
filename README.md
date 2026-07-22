# Nifty 3-Straddle Basket VWAP Backtest

Backtests a 3-straddle basket mean-reversion strategy on Nifty weekly options using the Fyers v3 history API, with a Plotly dashboard (tabs per day) auto-deployed to GitHub Pages via GitHub Actions.

## Strategy

1. **09:45 IST** — sample Nifty spot, round to nearest 100 (both directions) → fixes the ATM strike for the day. Two more strikes are derived: **ATM-100** and **ATM+100** (`config.STRIKE_OFFSETS`).
2. Build the **combined price** series = sum of all 6 legs' 1-min close prices (ATM-100 CE+PE, ATM CE+PE, ATM+100 CE+PE), and its **combined VWAP** (cumulative, using summed volume across all 6 legs) starting from market open (09:15).
3. **Entry**: the very first bar at/after 09:45 enters immediately if price < VWAP. After that, every **fresh downward crossing** of combined price below VWAP deploys another basket (1 lot each of ATM-100/ATM/ATM+100 straddles, 6 legs) — this repeats through the day, capped only by `config.MAX_BASKETS_PER_DAY` (default: uncapped).
4. **Exit** — each basket exits independently, whichever comes first:
   - price crosses back **above VWAP** ("Price > VWAP Exit"), or
   - price rallies back up through its **ATR trailing stop** (`config.ATR_MULTIPLIER` x ATR above the lowest price seen since entry — a favorable move gets a tighter stop as it goes), or
   - **15:15 IST time exit** for anything still open.

   ATR here (`compute_basket_atr` in `src/straddle_backtest.py`) is a close-to-close proxy computed on the combined basket series itself, since we don't have true high/low ticks for a synthetically-summed 6-leg series — not textbook ATR, but a consistent volatility proxy for the trailing-stop distance.

### Tunables
- `config.STRIKE_OFFSETS` — which strikes make up the basket relative to ATM (default `[-100, 0, 100]`)
- `config.LOTS_PER_LEG_PER_BASKET` — lots per leg per basket (default 1)
- `config.MAX_BASKETS_PER_DAY` — cap on baskets sold per day; `None` = unlimited (default)
- `config.ATR_PERIOD` / `config.ATR_MULTIPLIER` — trailing-stop tuning (default 14-bar / 2.0x)
- The core loop is in `src/straddle_backtest.py`, `run_day_backtest()`.

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

2. **Enable Pages**: Settings → Pages → Source = **"Deploy from a branch"**, Branch = `main`, Folder = **`/docs`**. This repo publishes via a committed `docs/` folder, not the Actions-based Pages deployment — there's no separate deploy step in either workflow; committing to `docs/` is all it takes, and GitHub republishes automatically (usually within a minute).

3. **Backtest range** is already set to `2026-07-14` → `2026-07-20` in `config.py` (`BACKTEST_START_DATE` / `BACKTEST_END_DATE`) — change it there, or pass different dates as workflow inputs when manually triggering the Action. Expiry for this window resolves to 2026-07-21 (Tuesday), i.e. `26721` in Fyers' weekly code — confirmed this matches by running `src/expiry_utils.get_weekly_expiry()` directly.

4. **Run locally first** (recommended, to catch symbol/expiry issues before burning API calls in CI):
   ```bash
   pip install -r requirements.txt
   export FYERS_APP_ID=...
   export FYERS_ACCESS_TOKEN=...
   python run_backtest.py 2025-11-01 2025-11-30
   open docs/index.html
   ```

5. **Run in CI**: Actions tab → "Straddle VWAP Backtest" → Run workflow, optionally overriding the date range. The historical candle cache (`data/cache/`) and `docs/results.json` get committed back to the repo so re-runs don't re-fetch days you've already pulled; the dashboard site (docs/) gets committed and Pages republishes automatically.

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
live_notifier.py                # cloud: REST-polling entry/exit checker + Telegram (cron-job.org triggered)
live_notifier_local.py          # local: websocket-driven entry/exit checker + Telegram (run on your own PC)
render_pages.py                  # rebuilds BOTH index.html and live.html together before every Pages deploy
src/telegram_notifier.py        # minimal Telegram Bot API sender
src/live_engine.py               # SHARED entry/exit/Telegram logic used by both live_notifier.py and live_notifier_local.py
src/fyers_ws_client.py           # Fyers websocket wrapper - builds 1-min candles from live ticks
src/env_loader.py                # tiny .env file loader for local runs (no python-dotenv dependency)
.env.example                     # copy to .env and fill in credentials for local runs
src/live_dashboard.py            # renders docs/live.html from live_notifier's state
test_synthetic.py              # smoke test with fabricated data, no API calls needed
test_live_notifier.py           # simulates a full day of 2-min cloud/REST live-notifier runs
test_live_notifier_local.py     # simulates a full day of local websocket runs (fake clock, no real network)
.github/workflows/backtest.yml # CI: run backtest -> deploy to Pages
.github/workflows/live_notifier.yml  # CI: triggered by cron-job.org, runs live_notifier.py
```

No numpy/pandas anywhere in core logic, per your usual constraint — everything is pure Python (VWAP, PnL, expiry math all hand-rolled).

## Live Telegram notifier (entries/exits, every 2 minutes)

`live_notifier.py` checks the live combined-basket price against VWAP and sends Telegram alerts on entries and the final square-off — same crossing logic as the backtest, evaluated incrementally.

**Why it's built this way:** cron-job.org can only call a URL on a schedule — it can't run a script on your machine directly. So the flow is:

```
cron-job.org (every 2 min)  --POST-->  GitHub repository_dispatch API
                                              |
                                              v
                          .github/workflows/live_notifier.yml runs
                                              |
                                              v
                    live_notifier.py checks price vs VWAP, alerts Telegram
                                              |
                                              v
                  data/live_state.json committed back to repo (persists
                  which baskets have already fired, across ephemeral runs)
```

### Setup

1. **Telegram bot**: message [@BotFather](https://t.me/BotFather) → `/newbot` → get your bot token. Message your new bot once, then hit `https://api.telegram.org/bot<TOKEN>/getUpdates` to find your `chat_id`.

2. **Add secrets** (same place as `FYERS_APP_ID`/`FYERS_ACCESS_TOKEN`):
   - `TELEGRAM_BOT_TOKEN`
   - `TELEGRAM_CHAT_ID`

3. **GitHub Personal Access Token** for cron-job.org to trigger the workflow: Settings → Developer settings → Fine-grained tokens → new token, scoped to this repo only (Resource owner = your account, Repository access = "Only select repositories" → this repo). Under **Repository permissions**, set **Contents: Read and write** — that's the only permission the `repository_dispatch` endpoint actually checks; leave everything else at "No access." Copy the token — you won't see it again. Fine-grained tokens expire after at most a year, so put a reminder in to rotate it.

4. **Configure the cron-job.org job**:
   - URL: `https://api.github.com/repos/<your-username>/<your-repo>/dispatches`
   - Method: `POST`
   - Schedule: every 2 minutes
   - Headers:
     - `Authorization: Bearer <your PAT from step 3>`
     - `Accept: application/vnd.github+json`
     - `Content-Type: application/json`
   - Body: `{"event_type": "live_check"}`

   Note cron-job.org's free tier may not support sub-5-minute intervals — check their current plan limits if 2 minutes isn't available.

5. **Test it manually first**: Actions tab → "Live Straddle Notifier" → Run workflow, to confirm secrets/permissions are wired up before relying on the external cron.

### Behavior notes

- Only fires between `STRIKE_FIX_TIME` (09:45) and `SQUARE_OFF_TIME` (15:15) — outside that window it just checks the time and exits (no API calls, no alerts).
- **2-minute granularity is a real limitation**: a cross that happens and reverses within a 2-minute gap between runs will be missed or misread. This mirrors real intraday conditions but isn't the same as tick-by-tick monitoring — worth knowing before trusting it fully for live trading.
- State resets automatically at the start of a new trading day (compares `data/live_state.json`'s stored date against today).
- If square-off can't get a valid exit price (data hiccup), it retries on the next run rather than silently giving up.
- Test the whole day's state machine locally without spending API calls or needing real Telegram creds: `python test_live_notifier.py` (simulates 2-min-interval runs against the synthetic data generator, prints every alert that would have fired).

### Both workflows keep the Pages site in sync

GitHub Pages here deploys **from a branch** (`main` / `/docs`), so if `backtest.yml` and `live_notifier.yml` each only rebuilt their own page before committing, every commit from either one would silently wipe out the other's page (since `docs/` is one shared folder). To avoid that, both workflows run `render_pages.py` right before committing:

- It rebuilds `docs/index.html` (backtest tabs) from whatever's in the committed `docs/results.json`, if it exists — otherwise writes a small placeholder that links to the live page.
- It rebuilds `docs/live.html` (today's live status: strike, current price vs VWAP, every entry/exit so far) from `data/live_state.json`, if it exists.

So a live-notifier run (every 2 minutes) recommits the *whole* site with both pages current, and a manual backtest run does the same. They also share the same `docs-pages` concurrency group so overlapping commits queue instead of racing. `docs/live.html` links back to `docs/index.html` and vice versa.

## Running locally on Linux Mint (recommended over the 2-minute cloud cron)

`live_notifier_local.py` runs continuously on your own machine, using the **Fyers WebSocket** to build 1-minute candles from live ticks — no REST polling, no GitHub Actions, no cron-job.org. This is meaningfully better than the cloud version: the cloud path only samples every 2 minutes (missing a lot of crossings in between), while this samples every completed minute, matching the batch backtest far more closely.

**Entry, exit, and Telegram logic are identical** between this and the cloud version — both funnel through `src/live_engine.py`, the single place that actually decides what to do with a bar. Only the data source differs.

**Safe to start any time before market open** (e.g. 07:00) — it idles with no network connection open until 09:15 IST, then connects and starts fetching. Data is only ever fetched from market open onward.

### Step-by-step: first-time setup

```bash
# 1. Unzip the project and cd into it
cd nifty-straddle-vwap

# 2. Check your Python version (3.8+ needed; Mint usually ships 3.10+ already)
python3 --version

# 3. Create a virtual environment (keeps this project's packages separate
#    from anything else on your system - recommended on an older shared PC)
python3 -m venv venv
source venv/bin/activate

# 4. Install dependencies
pip install -r requirements.txt

# 5. Set up your credentials (see "Fyers credentials" section below)
cp .env.example .env
nano .env   # or your preferred editor - fill in the 4 values
```

### Every trading morning

```bash
cd nifty-straddle-vwap
source venv/bin/activate   # if you made a venv in step 3 above

# Regenerate FYERS_ACCESS_TOKEN in .env - Fyers tokens expire daily (see below)

python3 live_notifier_local.py
```

Leave that running. It prints its progress (waiting for market open, strike fixed, each entry/exit) to the terminal, and sends the same events to Telegram. It exits on its own after the 15:15 square-off.

### Keeping it running without keeping a terminal window open

Since this is a long-running process, not a one-off script, you want it to survive closing the terminal / logging out. Two simple options on Mint:

**Option A - `tmux` (simplest)**:
```bash
sudo apt install tmux   # if not already installed
tmux new -s straddle
# inside the tmux session:
cd nifty-straddle-vwap && source venv/bin/activate && python3 live_notifier_local.py
# detach with: Ctrl-b then d  -- it keeps running in the background
# reattach later with:
tmux attach -t straddle
```

**Option B - systemd user service (auto-starts, auto-restarts on crash)**:
```bash
mkdir -p ~/.config/systemd/user
cat > ~/.config/systemd/user/straddle-notifier.service << 'EOF'
[Unit]
Description=Nifty straddle live notifier

[Service]
WorkingDirectory=%h/nifty-straddle-vwap
ExecStart=%h/nifty-straddle-vwap/venv/bin/python3 live_notifier_local.py
Restart=on-failure

[Install]
WantedBy=default.target
EOF

systemctl --user daemon-reload
systemctl --user start straddle-notifier
systemctl --user enable straddle-notifier   # optional: auto-start on login

# check on it:
systemctl --user status straddle-notifier
journalctl --user -u straddle-notifier -f   # live log tail
```
Since the access token needs refreshing every morning anyway, with this option you'd `systemctl --user restart straddle-notifier` each morning after updating `.env`, rather than it truly running unattended for weeks.

### If you kill it and restart the same day

State is saved to `data/live_state.json` after every processed bar. If you `Ctrl-C` it or it crashes, just re-run `python3 live_notifier_local.py` — it picks up from wherever it left off (won't re-fix the strike or re-enter baskets that already fired that day) rather than starting over.

### Things worth double-checking before trusting this for real trading

- **Tick field names**: `src/fyers_ws_client.py`'s `_parse_tick()` tries the common `ltp`/`vol_traded_today` key names, but these have varied slightly across `fyers-apiv3` versions in different reports. The first time you run this, watch the console for `WARNING: ws tick missing symbol/ltp keys` — if you see it, print the raw tick dict once to see your installed version's actual field names and adjust `_parse_tick()`.
- **Per-minute volume** is derived by diffing Fyers' cumulative "volume traded today" figure across each minute boundary — a missed tick right at a boundary can make that minute's volume slightly off. This only affects VWAP's volume weighting, not the price itself.
- **Test it first without spending real API calls**: `python3 test_live_notifier_local.py` simulates a full trading day using a fake clock and the same synthetic price generator as the other tests — no real WebSocket connection, no real waiting, prints every alert that would have fired.

## Where to update Fyers credentials

- **Local runs** (`live_notifier_local.py`, and `run_backtest.py`/`live_notifier.py` if you run them locally): edit `.env` (copied from `.env.example`, gitignored so it's safe to put real secrets in). All four values go there: `FYERS_APP_ID`, `FYERS_ACCESS_TOKEN`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`. `src/env_loader.py` loads this file automatically at the top of each script — no `export` needed.
- **Fyers access tokens expire daily** — this is a Fyers platform limitation, not something this project can work around. You need to regenerate `FYERS_ACCESS_TOKEN` (via your usual Fyers login/auth flow) and update `.env` every trading morning before running the local script.
- **Cloud runs** (GitHub Actions): unchanged from before — set as repo Secrets under Settings → Secrets and variables → Actions, not in `.env` (which doesn't exist in that environment).

## Where to update expiry

**Manual override (what you'll likely use week to week):** set `config.MANUAL_EXPIRY_CODE` to hardcode the exact expiry code used in every option symbol, in whichever format Fyers uses for that contract:
```python
MANUAL_EXPIRY_CODE = "26804"   # weekly numeric: YY + M(no leading zero) + DD -> 2026-08-04
MANUAL_EXPIRY_CODE = "26JUL"   # monthly letters: YY + 3-letter month
```
Set it back to `MANUAL_EXPIRY_CODE = None` to return to automatic weekly calculation. This one setting is read by `build_option_symbol()` in `src/expiry_utils.py`, which every part of the project (batch backtest, cloud notifier, local websocket notifier) already funnels through — so setting it once in `config.py` updates the expiry everywhere consistently. An invalid code (typo) raises a clear error immediately rather than silently building a wrong Fyers symbol.

**Automatic calculation (when `MANUAL_EXPIRY_CODE` is `None`):** the two things that affect it, both in `config.py`:

- **`WEEKLY_EXPIRY_SWITCH_DATE`** — the cutover date after which weekly expiry moved from Thursday to Tuesday. Already set based on what you told me earlier, but worth confirming against the actual NSE circular for extra certainty.
- **`NSE_HOLIDAYS`** — empty by default. If an expiry falls on a holiday you haven't listed here, the calculated expiry will be one day early (since holidays get skipped backward to the previous trading day). Worth populating this with the NSE holiday calendar for whatever period you're trading.

If you ever need to sanity-check what expiry the code will use for a given date (with `MANUAL_EXPIRY_CODE` set to `None`), run this directly:
```bash
python3 -c "
from src.expiry_utils import get_weekly_expiry, expiry_code
d = '2026-07-16'   # change to whatever date you want to check
e = get_weekly_expiry(d)
print(d, '->', e, expiry_code(e))
"
```

## Testing without live API access

`test_synthetic.py` fabricates a plausible CE/PE price path (decaying + oscillating) and runs it through the *real* backtest engine and dashboard generator, so you can sanity-check the VWAP crossing / entry / exit / PnL logic and the dashboard rendering before spending real API calls:

```bash
python test_synthetic.py
open docs/index.html
```
