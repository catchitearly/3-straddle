"""
Central configuration for the Nifty Straddle-VWAP backtest.
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

MAX_STRADDLES_PER_DAY = 3                   # hard cap on total straddles deployed/day
LOTS_PER_STRADDLE_ENTRY = 1                 # each VWAP-cross entry = this many lots
                                             # per leg (1 lot CE + 1 lot PE = 1 straddle)

# ---------------------------------------------------------------------------
# Timing (all times are IST / Asia-Kolkata, NOT server local time)
# ---------------------------------------------------------------------------
MARKET_OPEN_TIME = "09:15"
STRIKE_FIX_TIME = "09:45"                   # spot sampled here to fix ATM strike
SIGNAL_START_TIME = "09:45"                 # entries only considered from here
SQUARE_OFF_TIME = "15:15"                   # all open straddles closed here
MARKET_CLOSE_TIME = "15:30"

CANDLE_RESOLUTION = "1"                     # 1-minute candles

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

# ---------------------------------------------------------------------------
# Backtest date range
# ---------------------------------------------------------------------------
BACKTEST_START_DATE = "2025-11-01"          # YYYY-MM-DD, override via env/CLI
BACKTEST_END_DATE = "2025-11-30"

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
OUTPUT_DIR = "output"
OUTPUT_HTML = "output/index.html"
RESULTS_JSON = "output/results.json"


def get_lot_size(date_str):
    """Return the lot size in effect for a given YYYY-MM-DD date string."""
    size = LOT_SIZE
    for eff_date, lots in LOT_SIZE_BY_DATE:
        if date_str >= eff_date:
            size = lots
    return size
