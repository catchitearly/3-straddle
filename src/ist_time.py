"""
Timezone-safe IST helpers.

GitHub Actions runners default to UTC. This module NEVER relies on the host's
local timezone - every conversion is explicit, using a fixed +05:30 offset
(IST does not observe DST, so this is safe year-round).

This is the same class of bug that bit the iron condor project: always
construct timestamps via these helpers, never via naive datetime.now()
or datetime.strptime(...) without an explicit tzinfo.
"""

from datetime import datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))


def ist_datetime(date_str, time_str):
    """
    Build a timezone-aware IST datetime from 'YYYY-MM-DD' and 'HH:MM' strings.
    """
    y, m, d = (int(x) for x in date_str.split("-"))
    hh, mm = (int(x) for x in time_str.split(":"))
    return datetime(y, m, d, hh, mm, tzinfo=IST)


def ist_to_epoch(dt_ist):
    """Convert a timezone-aware IST datetime to a UTC unix epoch (int seconds)."""
    return int(dt_ist.astimezone(timezone.utc).timestamp())


def epoch_to_ist(epoch_seconds):
    """Convert a unix epoch (int seconds) to a timezone-aware IST datetime."""
    return datetime.fromtimestamp(epoch_seconds, tz=timezone.utc).astimezone(IST)


def epoch_to_ist_time_str(epoch_seconds):
    """Convert epoch seconds to an 'HH:MM' IST string, for display/matching."""
    return epoch_to_ist(epoch_seconds).strftime("%H:%M")


def date_range_str(start_date_str, end_date_str):
    """Yield 'YYYY-MM-DD' strings for each calendar day from start to end inclusive."""
    y1, m1, d1 = (int(x) for x in start_date_str.split("-"))
    y2, m2, d2 = (int(x) for x in end_date_str.split("-"))
    cur = datetime(y1, m1, d1, tzinfo=IST)
    end = datetime(y2, m2, d2, tzinfo=IST)
    one_day = timedelta(days=1)
    out = []
    while cur <= end:
        out.append(cur.strftime("%Y-%m-%d"))
        cur += one_day
    return out


def weekday_of(date_str):
    """Return Python weekday (Mon=0..Sun=6) for a 'YYYY-MM-DD' string."""
    y, m, d = (int(x) for x in date_str.split("-"))
    return datetime(y, m, d, tzinfo=IST).weekday()


def is_weekend(date_str):
    return weekday_of(date_str) >= 5
