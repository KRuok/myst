import os

CACHE_DIR = os.path.join(os.path.dirname(__file__), "cache")

# Historical period definitions (in trading days)
PERIODS = {
    "week1": 5,
    "week2": 10,
    "week3": 15,
    "month1": 20,
}

AKSHARE_CALENDAR_PATH = "/usr/local/lib/python3.11/dist-packages/akshare/file_fold/calendar.json"

FETCH_TIMEOUT = 20

MAX_PARALLEL_FETCHES = 5
