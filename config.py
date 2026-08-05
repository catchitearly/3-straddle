"""
Central configuration for the Nifty Straddle-VWAP backtest and live notifier.
No numpy / pandas anywhere in this project - pure python only.
"""

# ---------------------------------------------------------------------------
# Instrument / sizing
# ---------------------------------------------------------------------------
UNDERLYING_SYMBOL = "NSE:NIFTY50-INDEX"     # Fyers symbol for Nifty spot
LOT_SIZE = 65                               # NOTE: verify historical lot size for
                                             # your backtest window - Nifty lot size
                                             # has changed multiple times (50 -> 25 ->
                                             # 75 -> 65). If backtesting older dates,
                                             # set LOT_SIZE_BY_DATE below instead.
LOT_SIZE_BY_DATE = [
    # (effective_from_date_str "YYYY-MM-DD", lot_size)
    # Fill this in if your backtest range spans a lot-size change.
    ("2000-01-01", 65),
]

STRIKE_ROUND_TO = 100                       # round spot to nearest 100 (both ways)

# THREE overlapping 3-strike basket sets, each traded and tracked
# independently (own VWAP, own entries/exits, own PnL, own chart). Offsets
# are relative to the ATM strike. With ATM=24200 this gives:
#   Set A: 24100 / 24200 / 24300   (offsets -100, 0, +100)
#   Set B: 24000 / 24100 / 24200   (offsets -200, -100, 0)
#   Set C: 24200 / 24300 / 24400   (offsets 0, +100, +200)
# Together these need 5 distinct strikes (ATM-200 .. ATM+200), fetched once
# over the websocket and shared across sets - e.g. the 24100 and 24200
# strikes are each used by two different sets.
BASKET_SETS = {
    "A": [-100, 0, 100],
    "B": [-200, -100, 0],
    "C": [0, 100, 200],
}

LOTS_PER_LEG_PER_BASKET = 1                 # 1 lot on each of the 6 legs per basket, PER SET

# --- Batch backtest / cloud REST path (unchanged single basket) ---
# The batch backtest (run_backtest.py) still uses the ORIGINAL single
# 3-strike basket, kept separate from the live-only BASKET_SETS above so the
# two paths can't collide with each other's strike selection.
BACKTEST_STRIKE_OFFSETS = [-100, 0, 100]

# Every fresh downward cross of combined-price below combined-VWAP (after
# 09:45) sells one full basket again - exposure compounds, PER SET
# independently. Set an int here to cap baskets sold per day per set; leave
# as None for unlimited.
MAX_BASKETS_PER_DAY = None

# ATR trailing-stop exit (added alongside the VWAP-cross exit and 15:15 time
# exit). ATR here is computed on the combined basket price series itself
# (see src/straddle_backtest.compute_basket_atr) since we don't have true
# high/low ticks for the synthetic combined series - it's a close-to-close
# proxy, not textbook ATR.
ATR_PERIOD = 14
ATR_MULTIPLIER = 2.0

# --- Live (websocket) bar resolution ---
# The live websocket runner (live_notifier_local.py) builds candles at this
# resolution instead of Fyers' 1-minute REST candles - the batch backtest and
# the cloud/REST cron path (live_notifier.py) still operate on 1-minute bars.
# Entries/exits/VWAP/ATR on the LIVE path now decide on much finer bars.
# This is a deliberate divergence from the 1-min backtest, chosen explicitly -
# expect more (and faster) round-trips live than the backtest predicts.
LIVE_BAR_SECONDS = 5

# ATR period *in bars* for the live engine specifically. ATR_PERIOD=14 above
# means "14 one-minute bars" (~14 minutes) for the backtest/cloud path. At
# LIVE_BAR_SECONDS=5, an equivalent ~14-minute lookback would be
# 14 * 60 / 5 = 168 bars - but a much shorter window is often more
# appropriate at this granularity (a 14-minute lookback made of 5-second bars
# reacts far more slowly, in wall-clock terms, than 14 one-minute bars did).
# Tune this directly; it's independent of ATR_PERIOD above.
LIVE_ATR_PERIOD = 60   # 60 bars x 5s = 5 minutes of lookback

# How often to send a PnL heartbeat to Telegram during the live session
# (booked / unbooked / total PnL), independent of whether an entry or exit
# just happened. 15 minutes by default. Only takes effect if
# SEND_TRADE_TEXT_MESSAGES is True below.
HEARTBEAT_INTERVAL_SECONDS = 15 * 60

# If True, entry/exit/strike-fix/heartbeat TEXT alerts are sent via
# Telegram. If False (default), those events are only printed to the
# console, not sent to Telegram - the dashboard HTML file (sent separately,
# see DASHBOARD_SEND_INTERVAL_SECONDS) already shows every entry/exit and
# the current PnL, so per-trade pings become redundant with it running.
SEND_TRADE_TEXT_MESSAGES = False

# How often to send the live dashboard HTML file itself to Telegram as a
# document, during the local live session. 5 minutes by default. This is
# independent of SEND_TRADE_TEXT_MESSAGES above - the dashboard file keeps
# getting sent either way.
DASHBOARD_SEND_INTERVAL_SECONDS = 5 * 60

# ---------------------------------------------------------------------------
# Timing (all times are IST / Asia-Kolkata, NOT server local time)
# ---------------------------------------------------------------------------
MARKET_OPEN_TIME = "09:15"
STRIKE_FIX_TIME = "09:45"                   # spot sampled here to fix ATM strike
SIGNAL_START_TIME = "09:45"                 # entries only considered from here
SQUARE_OFF_TIME = "15:15"                   # all open straddles closed here
MARKET_CLOSE_TIME = "15:30"

CANDLE_RESOLUTION = "1"                     # 1-minute candles (Fyers REST API resolution
                                             # param - used only by src/fyers_client.py's
                                             # REST history fetches; unrelated to
                                             # LIVE_BAR_SECONDS, which governs the
                                             # separate websocket tick-aggregation path)

# ---------------------------------------------------------------------------
# Expiry
# ---------------------------------------------------------------------------
# Weekly Nifty expiry was Thursday until Sept 2025, shifted to Tuesday after.
# This date is the cutover - the LAST Thursday-expiry week before the switch.
# Verify against the official NSE circular before relying on this for real trades.
WEEKLY_EXPIRY_SWITCH_DATE = "2025-09-01"    # from this date onward -> Tuesday expiry
EXPIRY_WEEKDAY_BEFORE_SWITCH = 3             # Thursday (Mon=0 ... Sun=6)
EXPIRY_WEEKDAY_AFTER_SWITCH = 1              # Tuesday

# Optional: list of exchange holidays (YYYY-MM-DD strings). If an expiry falls on
# a holiday, NSE moves it to the previous trading day. Populate this list for
# accurate historical backtests - left empty by default.
NSE_HOLIDAYS = set([
    # "2025-08-15",
])

# Manual expiry override - set this to hardcode the expiry used for every
# option symbol built this week, instead of relying on the automatic weekly
# calculation above. Update it yourself each week/month as needed.
#
# Accepts either format, exactly as it appears in a real Fyers symbol:
#   - Weekly numeric:  "26804"  (YY + M no-leading-zero + DD  ->  2026-08-04)
#   - Monthly letters: "26JUL"  (YY + 3-letter month, e.g. 2026 July monthly)
#
# Set to None to go back to automatic weekly calculation (get_weekly_expiry
# in src/expiry_utils.py).
MANUAL_EXPIRY_CODE = None
# MANUAL_EXPIRY_CODE = "26804"
# MANUAL_EXPIRY_CODE = "26JUL"

# ---------------------------------------------------------------------------
# Backtest date range
# ---------------------------------------------------------------------------
BACKTEST_START_DATE = "2026-07-14"          # YYYY-MM-DD, override via env/CLI
BACKTEST_END_DATE = "2026-07-20"

# ---------------------------------------------------------------------------
# Fyers API
# ---------------------------------------------------------------------------
FYERS_BASE_URL = "https://api-t1.fyers.in/data/history"  # Fyers v3 historical data
FYERS_APP_ID_ENV = "FYERS_APP_ID"
FYERS_ACCESS_TOKEN_ENV = "FYERS_ACCESS_TOKEN"

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
CACHE_DIR = "data/cache"
OUTPUT_DIR = "docs"
OUTPUT_HTML = "docs/index.html"
RESULTS_JSON = "docs/results.json"


def get_lot_size(date_str):
    """Return the lot size in effect for a given YYYY-MM-DD date string."""
    size = LOT_SIZE
    for eff_date, lots in LOT_SIZE_BY_DATE:
        if date_str >= eff_date:
            size = lots
    return size
