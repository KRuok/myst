import os
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware

from config import CACHE_DIR
from trading_calendar import (
    get_latest_trading_date,
    get_trading_dates_last_n_years,
    is_trading_date,
    get_nearest_trading_date,
)
from data_service import (
    fetch_zt_data,
    fetch_strong_data,
    compute_history_counts,
    compute_sentiment,
    compute_tiers,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    os.makedirs(CACHE_DIR, exist_ok=True)
    yield


app = FastAPI(title="涨停板行情", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
async def index():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


@app.get("/api/latest-trading-date")
async def latest_trading_date():
    return {"date": get_latest_trading_date()}


@app.get("/api/trading-dates")
async def trading_dates():
    return {"dates": get_trading_dates_last_n_years(2)}


@app.get("/api/zt/{date}")
async def zt_data(
    date: str,
    include_history: bool = Query(default=True),
):
    if not is_trading_date(date):
        nearest = get_nearest_trading_date(date)
        raise HTTPException(
            status_code=400,
            detail={"error": "not_a_trading_date", "nearest": nearest},
        )

    try:
        records = await fetch_zt_data(date)
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))

    if include_history and records:
        try:
            records = await compute_history_counts(date, records)
        except Exception as e:
            logger.warning("History count failed for %s: %s", date, e)

    tiers = compute_tiers(records)

    return {
        "date": date,
        "total": len(records),
        "records": records,
        "tiers": tiers,
    }


@app.get("/api/sentiment/{date}")
async def sentiment(date: str):
    if not is_trading_date(date):
        nearest = get_nearest_trading_date(date)
        raise HTTPException(
            status_code=400,
            detail={"error": "not_a_trading_date", "nearest": nearest},
        )
    try:
        data = await compute_sentiment(date)
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))
    return data


@app.get("/api/strong/{date}")
async def strong_stocks(date: str):
    if not is_trading_date(date):
        nearest = get_nearest_trading_date(date)
        raise HTTPException(
            status_code=400,
            detail={"error": "not_a_trading_date", "nearest": nearest},
        )
    try:
        records = await fetch_strong_data(date)
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))
    return {"date": date, "total": len(records), "records": records}
