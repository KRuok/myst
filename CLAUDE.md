# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

A股涨停板行情 — a single-page web app for screening A-share limit-up (涨停) stocks. FastAPI backend fetches data from AKShare (东方财富 API wrapper) with disk-based caching, served to a vanilla JS/CSS/HTML frontend with no build step.

## Running the App

```bash
# Install (use Aliyun mirror if in China or PyPI is blocked)
pip install -r requirements.txt
pip install -r requirements.txt -i https://mirrors.aliyun.com/pypi/simple/

# Start the server (port 8000)
uvicorn main:app --host 0.0.0.0 --port 8000 --reload

# For Render / Docker, the PORT env var is used:
uvicorn main:app --host 0.0.0.0 --port $PORT
```

There are no tests and no lint configuration. Verify backend imports cleanly with:
```bash
python3 -c "import main; print('OK')"
```

## Critical AKShare Workaround

AKShare (v1.18+) depends on a `jsonpath` package that isn't installed. A stub must be injected **before any `import akshare`** statement:

```python
import sys, types
if "jsonpath" not in sys.modules:
    sys.modules["jsonpath"] = types.ModuleType("jsonpath")
import akshare as ak
```

This stub is present in both `config.py` and `data_service.py`. If you add a new file that imports AKShare, add the stub there too. Do not remove the duplicate — import order between modules is not guaranteed.

## Architecture

```
config.py           → constants + AKShare stub (imported first by everything)
trading_calendar.py → trading date utilities (bisect-based, O(log n))
data_service.py     → all data fetching, caching, scoring, aggregation
main.py             → FastAPI endpoints (thin wrappers around data_service)
static/index.html   → single HTML shell with three tabs
static/style.css    → light theme, CSS variables, no preprocessor
static/app.js       → all frontend logic (~880 lines, no framework)
```

### Trading Calendar

`trading_calendar.py` loads the calendar bundled with AKShare at import time:
```python
AKSHARE_CALENDAR_PATH = os.path.join(os.path.dirname(_ak.__file__), "file_fold", "calendar.json")
```
The path is resolved dynamically in `config.py` — never hardcode `/usr/local/lib/...`. All date strings in this project are `YYYYMMDD` (8-digit strings, no separators).

### Caching Layer (`data_service.py`)

Cache files live in `cache/` (gitignored). File naming: `cache/{prefix}_{YYYYMMDD}.json` where prefix is `zt` or `strong`.

Cache validity rules:
- Historical dates (< today): **permanently valid**
- Today's date: valid only if fetched **after 15:30** (market close)
- Empty results are also cached to avoid repeated failed requests

All AKShare calls run via `loop.run_in_executor(None, sync_fn, ...)` wrapped in `asyncio.wait_for(..., timeout=25)` to avoid blocking the event loop.

Concurrent history fetches (needed for 20-day lookback) use `asyncio.Semaphore(MAX_PARALLEL_FETCHES)` where `MAX_PARALLEL_FETCHES = 5`.

### Field Mapping

AKShare returns Chinese column names. Both `_ZT_COLUMNS` and `_STRONG_COLUMNS` dicts in `data_service.py` map them to English JSON keys. The key mappings to know:

| Chinese | JSON key | Notes |
|---|---|---|
| 首次封板时间 | `first_time` | 6-digit string e.g. `"092530"` = 09:25:30 |
| 炸板次数 | `break_count` | "open board" count |
| 连板数 | `consecutive` | streak of consecutive limit-up days |
| 封板资金 | `seal_fund` | money locked at limit-up |
| 流通市值 | `circ_cap` | circulating market cap in yuan |
| 涨停统计 | `zt_stat` | raw string from EastMoney e.g. "5/10" |

### Seal Quality Score

Computed in `compute_seal_score()` and stored on each record as `score` (0–100):

- `first_time < 093100` → +40 (auction seal)
- `first_time < 100000` → +25
- `first_time < 140000` → +10
- `break_count == 0` → +30
- `break_count == 1` → +15
- `seal_fund / volume > 0.3` → +30
- `seal_fund / volume > 0.15` → +15

### Frontend State (`app.js`)

Global state variables:
- `allRecords` — raw records from the current date's `/api/zt/{date}` response
- `currentDate` — active date in `YYYYMMDD` format
- `sortState` — `{ col, dir }` for main table sorting
- `tierFilter` — consecutive board filter (`'all'` or integer bucket 1–5)
- `auctionOnly` — boolean, filter to 竞价封板 only
- `capFilter` — `'all' | 'small' | 'mid' | 'large'` (based on `circ_cap`)
- `leaderCodes` — `Set` of codes identified as sector leaders
- `klineChart` — LightweightCharts instance; must be `.remove()`d before creating a new one

### Leader Detection Logic

`computeLeaders(records)` only marks a leader if **2 or more stocks share the same industry**. Within a sector, ranked by: consecutive boards desc → `first_time` asc (earlier = stronger) → score desc.

### Cap Tier Boundaries

`capTier(circ_cap)` in `app.js` (values are in yuan):
- `small`: circ_cap < 5×10⁹ (< 50亿)
- `mid`: circ_cap < 2×10¹⁰ (< 200亿)
- `large`: circ_cap ≥ 2×10¹⁰

### K-Line Chart

Uses [LightweightCharts 4.1.1](https://unpkg.com/lightweight-charts@4.1.1) loaded from CDN. A-share color convention: **red = up, green = down** (opposite of Western default). Volume histogram uses a separate price scale `'vol'` with `scaleMargins: { top: 0.8, bottom: 0 }`. The candlestick scale uses `scaleMargins: { top: 0.05, bottom: 0.22 }` to leave room.

The `klineChart` instance **must** be destroyed with `klineChart.remove()` before creating a new one (done in `closeModal()` and at the start of `openStockModal()`).

### Analysis Tab Loading

`maybeLoadAnalysis()` fires only when the 市场分析 tab is first activated for a given date. It fires three parallel fetches: `/api/market-trend/{date}`, `/api/nextday-stats/{date}`, `/api/tier-promotion/{date}`. Chart.js 4.4.0 (CDN) handles the trend and sector charts.

## API Endpoints Reference

| Endpoint | Key behavior |
|---|---|
| `GET /api/zt/{date}` | Main pool; includes history counts (`include_history=true`) and `score`; validates trading date |
| `GET /api/sentiment/{date}` | `total_zt`, `broke_rate`, `promotion_rate` vs previous trading day |
| `GET /api/strong/{date}` | Yesterday-ZT-today-strong pool (`stock_zt_pool_strong_em`) |
| `GET /api/market-trend/{date}?days=20` | Per-day stats for past N trading days |
| `GET /api/stock-timeline/{code}/{date}?days=30` | Day-by-day ZT hit/miss for one stock |
| `GET /api/nextday-stats/{date}` | Next-day ZT rate by tier; returns `available:false` if next day hasn't closed |
| `GET /api/kline/{code}/{date}?days=40` | OHLCV via `stock_zh_a_hist(..., adjust="qfq")` |
| `GET /api/tier-promotion/{date}?days=30` | Historical N→N+1 board advancement rate |

All endpoints return HTTP 400 for non-trading dates with `{"error": "not_a_trading_date", "nearest": "YYYYMMDD"}` and HTTP 502 if AKShare/network fails.

## Deployment

**Render**: `render.yaml` configures a Python web service. Uses `$PORT` env var. Cache directory is ephemeral on Render (recreated each deploy).

**Docker**: `Dockerfile` uses `python:3.11-slim`, pre-creates `cache/` directory.

Note: EastMoney's API may be blocked from overseas servers (Render US regions). A Chinese VPS or proxy may be required for production use.
