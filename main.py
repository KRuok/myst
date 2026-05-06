import os
import asyncio
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
    compute_market_trend,
    fetch_stock_timeline,
    compute_nextday_stats,
    fetch_stock_kline,
    compute_tier_promotion_stats,
    compute_feature_backtest,
    compute_zt_trend,
    fetch_wencai_data,
    warm_zt_cache,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    os.makedirs(CACHE_DIR, exist_ok=True)
    asyncio.create_task(warm_zt_cache(35))   # background; non-blocking
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


@app.get("/api/market-trend/{date}")
async def market_trend(date: str, days: int = Query(default=20, ge=5, le=60)):
    if not is_trading_date(date):
        raise HTTPException(status_code=400, detail={"error": "not_a_trading_date"})
    try:
        data = await compute_market_trend(date, days)
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))
    return {"date": date, "days": days, "trend": data}


@app.get("/api/stock-timeline/{code}/{date}")
async def stock_timeline_endpoint(
    code: str, date: str, days: int = Query(default=30, ge=10, le=60)
):
    if not is_trading_date(date):
        raise HTTPException(status_code=400, detail={"error": "not_a_trading_date"})
    try:
        data = await fetch_stock_timeline(code, date, days)
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))
    return {"code": code, "date": date, "timeline": data}


@app.get("/api/nextday-stats/{date}")
async def nextday_stats_endpoint(date: str):
    if not is_trading_date(date):
        raise HTTPException(status_code=400, detail={"error": "not_a_trading_date"})
    try:
        data = await compute_nextday_stats(date)
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))
    return data


@app.get("/api/kline/{code}/{date}")
async def kline_data(code: str, date: str, days: int = Query(default=40, ge=10, le=120)):
    if not is_trading_date(date):
        raise HTTPException(status_code=400, detail={"error": "not_a_trading_date"})
    error = None
    try:
        data = await fetch_stock_kline(code, date, days)
    except Exception as e:
        error = str(e)
        data = []
    return {"code": code, "date": date, "kline": data, "error": error}


@app.get("/api/tier-promotion/{date}")
async def tier_promotion_endpoint(date: str, days: int = Query(default=30, ge=10, le=60)):
    if not is_trading_date(date):
        raise HTTPException(status_code=400, detail={"error": "not_a_trading_date"})
    try:
        data = await compute_tier_promotion_stats(date, days)
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))
    return {"date": date, "days": days, "stats": data}


@app.get("/api/feature-backtest/{date}")
async def feature_backtest_endpoint(
    date: str, window: int = Query(default=20, ge=10, le=40)
):
    if not is_trading_date(date):
        raise HTTPException(status_code=400, detail={"error": "not_a_trading_date"})
    try:
        data = await compute_feature_backtest(date, window)
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))
    return data


@app.get("/api/wencai/{code}")
async def wencai_endpoint(code: str, name: str = Query(default="")):
    try:
        data = await fetch_wencai_data(code, name)
    except Exception as e:
        data = {"available": False, "reason": str(e)}
    return data


@app.get("/api/zt-trend/{date}")
async def zt_trend_endpoint(date: str):
    if not is_trading_date(date):
        raise HTTPException(status_code=400, detail={"error": "not_a_trading_date"})
    try:
        records = await compute_zt_trend(date)
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))
    return {"date": date, "records": records}


@app.get("/api/cache-status")
async def cache_status():
    """Returns count of cached ZT pool dates (used to track warmup progress)."""
    try:
        files = [
            f[3:-5] for f in os.listdir(CACHE_DIR)
            if f.startswith("zt_") and f.endswith(".json")
        ]
        files.sort(reverse=True)
        return {"count": len(files), "latest": files[:5] if files else []}
    except Exception:
        return {"count": 0, "latest": []}
