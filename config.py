import os
import sys
import types

# AKShare jsonpath stub must be injected before any akshare import
if "jsonpath" not in sys.modules:
    sys.modules["jsonpath"] = types.ModuleType("jsonpath")

import akshare as _ak

CACHE_DIR = os.path.join(os.path.dirname(__file__), "cache")

# Historical period definitions (in trading days)
PERIODS = {
    "week1": 5,
    "week2": 10,
    "week3": 15,
    "month1": 20,
}

# Dynamically locate the bundled trading calendar (works on any OS / install path)
AKSHARE_CALENDAR_PATH = os.path.join(
    os.path.dirname(_ak.__file__), "file_fold", "calendar.json"
)

FETCH_TIMEOUT = 25

MAX_PARALLEL_FETCHES = 3   # lower = fewer simultaneous connections to EastMoney
