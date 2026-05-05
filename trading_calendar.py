import json
import datetime
import bisect
from config import AKSHARE_CALENDAR_PATH

with open(AKSHARE_CALENDAR_PATH) as _f:
    _TRADING_DATES: list[str] = json.load(_f)


def get_latest_trading_date() -> str:
    today = datetime.date.today().strftime("%Y%m%d")
    idx = bisect.bisect_right(_TRADING_DATES, today) - 1
    return _TRADING_DATES[max(idx, 0)]


def get_previous_n_trading_dates(anchor: str, n: int) -> list[str]:
    """Returns up to n trading dates ending at anchor (inclusive if anchor is a trading date)."""
    idx = bisect.bisect_right(_TRADING_DATES, anchor) - 1
    if idx < 0:
        return []
    start = max(0, idx - n + 1)
    return _TRADING_DATES[start : idx + 1]


def is_trading_date(date_str: str) -> bool:
    idx = bisect.bisect_left(_TRADING_DATES, date_str)
    return idx < len(_TRADING_DATES) and _TRADING_DATES[idx] == date_str


def get_nearest_trading_date(date_str: str) -> str:
    """Returns the most recent trading date <= date_str."""
    idx = bisect.bisect_right(_TRADING_DATES, date_str) - 1
    return _TRADING_DATES[max(idx, 0)]


def get_trading_dates_last_n_years(n: int = 2) -> list[str]:
    cutoff = (datetime.date.today() - datetime.timedelta(days=365 * n)).strftime("%Y%m%d")
    today = datetime.date.today().strftime("%Y%m%d")
    lo = bisect.bisect_left(_TRADING_DATES, cutoff)
    hi = bisect.bisect_right(_TRADING_DATES, today)
    return _TRADING_DATES[lo:hi]


def get_prev_trading_date(date_str: str) -> str | None:
    """Returns the trading date immediately before date_str."""
    idx = bisect.bisect_left(_TRADING_DATES, date_str)
    # If date_str itself is a trading date, idx points to it; go one back
    if idx < len(_TRADING_DATES) and _TRADING_DATES[idx] == date_str:
        idx -= 1
    else:
        idx -= 1
    if idx < 0:
        return None
    return _TRADING_DATES[idx]
