"""
Weekly expiry calculation + Fyers option symbol construction.

Fyers weekly option symbol convention (established in prior projects):
    NSE:NIFTY{EXPIRY_STR}{STRIKE}{CE|PE}
    EXPIRY_STR = YY + M(no leading zero) + DD   e.g. 2025-09-16 -> "25916"

Weekly expiry weekday changed from Thursday to Tuesday starting the week of
2025-09-01 (see config.WEEKLY_EXPIRY_SWITCH_DATE) - verify against the NSE
circular for your exact backtest window.
"""

from datetime import datetime, timedelta

import config
from src.ist_time import IST, weekday_of


def _expiry_weekday_for(date_str):
    """Which weekday (Mon=0..Sun=6) is the weekly expiry day, for this date."""
    if date_str >= config.WEEKLY_EXPIRY_SWITCH_DATE:
        return config.EXPIRY_WEEKDAY_AFTER_SWITCH
    return config.EXPIRY_WEEKDAY_BEFORE_SWITCH


def _shift_back_for_holidays(date_str):
    """If expiry falls on an NSE holiday or weekend, move to the previous trading day."""
    y, m, d = (int(x) for x in date_str.split("-"))
    dt = datetime(y, m, d, tzinfo=IST)
    while True:
        ds = dt.strftime("%Y-%m-%d")
        if dt.weekday() < 5 and ds not in config.NSE_HOLIDAYS:
            return ds
        dt -= timedelta(days=1)


def get_weekly_expiry(trade_date_str):
    """
    Given a trade date 'YYYY-MM-DD', return the expiry date 'YYYY-MM-DD' of the
    weekly option series that is current (nearest expiry on/after trade_date).
    """
    target_weekday = _expiry_weekday_for(trade_date_str)
    y, m, d = (int(x) for x in trade_date_str.split("-"))
    dt = datetime(y, m, d, tzinfo=IST)
    days_ahead = (target_weekday - dt.weekday()) % 7
    expiry_dt = dt + timedelta(days=days_ahead)
    expiry_str = expiry_dt.strftime("%Y-%m-%d")
    return _shift_back_for_holidays(expiry_str)


def expiry_code(expiry_date_str):
    """Format 'YYYY-MM-DD' as Fyers weekly expiry code: YY + M(no leading zero) + DD."""
    y, m, d = (int(x) for x in expiry_date_str.split("-"))
    yy = y % 100
    return f"{yy}{m}{d:02d}"


def round_to_nearest_strike(spot_price, step=None):
    """Round spot to the nearest multiple of `step` (both directions)."""
    step = step or config.STRIKE_ROUND_TO
    return int(round(spot_price / step) * step)


def build_option_symbol(trade_date_str, strike, opt_type):
    """
    Build the Fyers option symbol, e.g. NSE:NIFTY25916 24800CE
    opt_type must be 'CE' or 'PE'.
    """
    assert opt_type in ("CE", "PE")
    expiry_date_str = get_weekly_expiry(trade_date_str)
    code = expiry_code(expiry_date_str)
    return f"NSE:NIFTY{code}{strike}{opt_type}", expiry_date_str
