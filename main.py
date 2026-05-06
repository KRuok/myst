import os
import re
import asyncio
import logging
from contextlib import asynccontextmanager

import requests as _req

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, Response as RawResponse
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

# ── Wencai reverse proxy ────────────────────────────────────────────────────
_WENCAI_ORIGIN = "https://www.iwencai.com"
_PROXY_PREFIX  = "/proxy/wencai"

# Persistent session keeps Wencai cookies (login state) across requests
_wencai_session = _req.Session()
_wencai_session.headers.update({
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "Sec-Ch-Ua": '"Chromium";v="120", "Google Chrome";v="120"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "same-origin",
})

_STRIP_HEADERS = {
    "x-frame-options", "content-security-policy",
    "content-security-policy-report-only",
    "transfer-encoding", "content-encoding",
    "content-length", "connection",
}

# JS injected into every proxied HTML page.
# Rewrites fetch() / XHR so the SPA's API calls go through our proxy instead
# of directly to iwencai.com (which would be CORS-blocked from our origin).
_PROXY_JS = r"""<script>
(function(){
  var O='https://www.iwencai.com',P='/proxy/wencai';
  function rw(u){
    if(typeof u!=='string')return u;
    if(u.startsWith(O))return P+u.slice(O.length)||P+'/';
    if(u.startsWith('//www.iwencai.com'))return P+u.slice('//www.iwencai.com'.length);
    return u;
  }
  var _f=window.fetch;
  window.fetch=function(i,o){return _f.call(this,typeof i==='string'?rw(i):i,o);};
  var _x=XMLHttpRequest.prototype.open;
  XMLHttpRequest.prototype.open=function(m,u){
    return _x.apply(this,[m,rw(u)].concat([].slice.call(arguments,2)));
  };
})();
</script>"""


def _rewrite_html(html: str) -> str:
    """Inject base href + JS interceptor; rewrite static attribute URLs."""
    # base href routes relative paths through our proxy
    inject = (
        f'<base href="{_PROXY_PREFIX}/">'
        '<meta name="referrer" content="no-referrer">'
        + _PROXY_JS
    )
    if "<head>" in html:
        html = html.replace("<head>", "<head>" + inject, 1)
    elif re.search(r"<head[\s>]", html):
        html = re.sub(r"(<head[^>]*>)", r"\1" + inject, html, count=1)
    else:
        html = inject + html

    # Rewrite absolute Wencai URLs in HTML attribute values
    html = html.replace(f'="{_WENCAI_ORIGIN}/', f'="{_PROXY_PREFIX}/')
    html = html.replace(f"='{_WENCAI_ORIGIN}/", f"='{_PROXY_PREFIX}/")
    return html


def _sync_proxy(method: str, url: str, req_headers: dict,
                cookies: dict, body: bytes) -> _req.Response:
    return _wencai_session.request(
        method, url,
        headers=req_headers, cookies=cookies,
        data=body if method in ("POST", "PUT", "PATCH") else None,
        timeout=20, allow_redirects=True, stream=False,
    )


async def _proxy(path: str, request: Request) -> RawResponse:
    qs = str(request.url.query)
    target = f"{_WENCAI_ORIGIN}/{path}" + (f"?{qs}" if qs else "")

    method  = request.method
    body    = await request.body()
    cookies = dict(request.cookies)
    req_headers = {
        "Accept":          request.headers.get("accept", "text/html,*/*"),
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Referer":         _WENCAI_ORIGIN + "/",
        "X-Requested-With": request.headers.get("x-requested-with", ""),
        "Content-Type":    request.headers.get("content-type", ""),
    }

    try:
        resp = await asyncio.get_event_loop().run_in_executor(
            None, lambda: _sync_proxy(method, target, req_headers, cookies, body)
        )
    except Exception as exc:
        logger.error("Wencai proxy error %s: %s", target, exc)
        return RawResponse(
            content=f"<html><body style='font:14px sans-serif;padding:20px'>"
                    f"<b>代理请求失败</b><br>{exc}</body></html>".encode(),
            status_code=502, media_type="text/html; charset=utf-8",
        )

    ct = resp.headers.get("content-type", "")
    if "text/html" in ct:
        content    = _rewrite_html(resp.text).encode("utf-8")
        media_type = "text/html; charset=utf-8"
    else:
        content    = resp.content
        media_type = ct or "application/octet-stream"

    out_headers: dict[str, str] = {}
    for k, v in resp.headers.items():
        if k.lower() not in _STRIP_HEADERS:
            out_headers[k] = v

    return RawResponse(
        content=content, status_code=resp.status_code,
        headers=out_headers, media_type=media_type,
    )
# ───────────────────────────────────────────────────────────────────────────


async def _warmup_wencai_session():
    """Visit Wencai homepage to acquire initial session cookies."""
    try:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            None, lambda: _wencai_session.get(_WENCAI_ORIGIN + "/", timeout=15)
        )
        logger.info("Wencai session initialized (%d cookies)", len(_wencai_session.cookies))
    except Exception as exc:
        logger.warning("Wencai session warmup failed: %s", exc)


@asynccontextmanager
async def lifespan(app: FastAPI):
    os.makedirs(CACHE_DIR, exist_ok=True)
    asyncio.create_task(warm_zt_cache(35))
    asyncio.create_task(_warmup_wencai_session())
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


@app.api_route("/proxy/wencai", methods=["GET", "POST", "HEAD"])
async def proxy_wencai_root(request: Request):
    return await _proxy("", request)


@app.api_route("/proxy/wencai/{path:path}", methods=["GET", "POST", "HEAD"])
async def proxy_wencai_path(path: str, request: Request):
    return await _proxy(path, request)


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
