import sys
import types
import json
import os
import asyncio
import datetime
import logging

if "jsonpath" not in sys.modules:
    sys.modules["jsonpath"] = types.ModuleType("jsonpath")

import akshare as ak
import pandas as pd

from collections import defaultdict

from config import CACHE_DIR, PERIODS, MAX_PARALLEL_FETCHES
from trading_calendar import (
    get_previous_n_trading_dates,
    get_prev_trading_date,
    get_next_trading_date,
)

logger = logging.getLogger(__name__)

os.makedirs(CACHE_DIR, exist_ok=True)


# ---------------------------------------------------------------------------
# Cache helpers
# ---------------------------------------------------------------------------

def _cache_path(date_str: str, prefix: str = "zt") -> str:
    return os.path.join(CACHE_DIR, f"{prefix}_{date_str}.json")


def _is_cache_valid(cache_data: dict, date_str: str) -> bool:
    today = datetime.date.today().strftime("%Y%m%d")
    if date_str < today:
        return True
    fetched_at_str = cache_data.get("fetched_at", "")
    if not fetched_at_str:
        return False
    try:
        fetched_at = datetime.datetime.fromisoformat(fetched_at_str)
    except ValueError:
        return False
    market_close = fetched_at.replace(hour=15, minute=30, second=0, microsecond=0)
    return fetched_at >= market_close


def _read_cache(date_str: str, prefix: str = "zt") -> list[dict] | None:
    path = _cache_path(date_str, prefix)
    if not os.path.exists(path):
        return None
    try:
        with open(path) as f:
            data = json.load(f)
        if not _is_cache_valid(data, date_str):
            return None
        if data.get("empty"):
            return []
        return data.get("records", [])
    except Exception:
        return None


def _write_cache(date_str: str, records: list[dict], prefix: str = "zt") -> None:
    path = _cache_path(date_str, prefix)
    payload = {
        "date": date_str,
        "fetched_at": datetime.datetime.now().isoformat(),
        "empty": len(records) == 0,
        "records": records,
    }
    try:
        with open(path, "w") as f:
            json.dump(payload, f, ensure_ascii=False)
    except Exception as e:
        logger.warning("Failed to write cache %s: %s", path, e)


# ---------------------------------------------------------------------------
# Field mapping: AKShare column names → JSON keys
# ---------------------------------------------------------------------------

_ZT_COLUMNS = {
    "代码": "code",
    "名称": "name",
    "涨跌幅": "change_pct",
    "最新价": "price",
    "成交额": "volume",
    "流通市值": "circ_cap",
    "总市值": "total_cap",
    "换手率": "turnover",
    "封板资金": "seal_fund",
    "首次封板时间": "first_time",
    "最后封板时间": "last_time",
    "炸板次数": "break_count",
    "涨停统计": "zt_stat",
    "连板数": "consecutive",
    "所属行业": "industry",
}

_STRONG_COLUMNS = {
    "代码": "code",
    "名称": "name",
    "涨跌幅": "change_pct",
    "最新价": "price",
    "成交额": "volume",
    "换手率": "turnover",
    "涨速": "rise_speed",
    "是否新高": "new_high",
    "量比": "vol_ratio",
    "涨停统计": "zt_stat",
    "所属行业": "industry",
}


def _df_to_records(df: pd.DataFrame, col_map: dict) -> list[dict]:
    records = []
    for _, row in df.iterrows():
        rec = {}
        for cn, en in col_map.items():
            if cn in row.index:
                val = row[cn]
                if pd.isna(val):
                    rec[en] = None
                elif isinstance(val, (int, float)):
                    rec[en] = val
                else:
                    rec[en] = str(val)
            else:
                rec[en] = None
        records.append(rec)
    return records


# ---------------------------------------------------------------------------
# Seal quality score
# ---------------------------------------------------------------------------

def compute_seal_score(rec: dict) -> int:
    score = 0
    first_time = rec.get("first_time") or ""
    try:
        t = int(first_time)
        if t < 93100:
            score += 40
        elif t < 100000:
            score += 25
        elif t < 140000:
            score += 10
    except (ValueError, TypeError):
        pass

    break_count = rec.get("break_count") or 0
    try:
        bc = int(break_count)
        if bc == 0:
            score += 30
        elif bc == 1:
            score += 15
    except (ValueError, TypeError):
        pass

    volume = rec.get("volume") or 0
    seal_fund = rec.get("seal_fund") or 0
    try:
        vol = float(volume)
        sf = float(seal_fund)
        if vol > 0:
            ratio = sf / vol
            if ratio > 0.3:
                score += 30
            elif ratio > 0.15:
                score += 15
    except (ValueError, TypeError):
        pass

    return score


# ---------------------------------------------------------------------------
# AKShare fetch wrappers (run in executor to avoid blocking the event loop)
# ---------------------------------------------------------------------------

def _fetch_zt_sync(date_str: str) -> pd.DataFrame:
    return ak.stock_zt_pool_em(date=date_str)


def _fetch_strong_sync(date_str: str) -> pd.DataFrame:
    return ak.stock_zt_pool_strong_em(date=date_str)


async def _run_sync(fn, *args):
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, fn, *args)


# ---------------------------------------------------------------------------
# Public fetch functions
# ---------------------------------------------------------------------------

async def fetch_zt_data(date_str: str) -> list[dict]:
    cached = _read_cache(date_str, "zt")
    if cached is not None:
        return cached

    try:
        df = await asyncio.wait_for(_run_sync(_fetch_zt_sync, date_str), timeout=25)
    except asyncio.TimeoutError:
        logger.error("Timeout fetching ZT data for %s", date_str)
        raise
    except Exception as e:
        logger.error("Error fetching ZT data for %s: %s", date_str, e)
        raise

    if df is None or df.empty:
        _write_cache(date_str, [], "zt")
        return []

    records = _df_to_records(df, _ZT_COLUMNS)
    for rec in records:
        rec["score"] = compute_seal_score(rec)
    _write_cache(date_str, records, "zt")
    return records


async def fetch_strong_data(date_str: str) -> list[dict]:
    cached = _read_cache(date_str, "strong")
    if cached is not None:
        return cached

    try:
        df = await asyncio.wait_for(_run_sync(_fetch_strong_sync, date_str), timeout=25)
    except asyncio.TimeoutError:
        logger.error("Timeout fetching strong data for %s", date_str)
        return []
    except Exception as e:
        logger.error("Error fetching strong data for %s: %s", date_str, e)
        return []

    if df is None or df.empty:
        _write_cache(date_str, [], "strong")
        return []

    records = _df_to_records(df, _STRONG_COLUMNS)
    _write_cache(date_str, records, "strong")
    return records


# ---------------------------------------------------------------------------
# Historical limit-up count (20-day lookback covers all 4 windows)
# ---------------------------------------------------------------------------

async def compute_history_counts(date_str: str, records: list[dict]) -> list[dict]:
    dates_20 = get_previous_n_trading_dates(date_str, 20)
    sem = asyncio.Semaphore(MAX_PARALLEL_FETCHES)

    async def fetch_with_sem(d: str):
        async with sem:
            try:
                return d, await fetch_zt_data(d)
            except Exception:
                return d, []

    results = await asyncio.gather(*[fetch_with_sem(d) for d in dates_20])

    date_code_sets: dict[str, set] = {}
    for d, recs in results:
        date_code_sets[d] = {r["code"] for r in recs if r.get("code")}

    for stock in records:
        code = stock.get("code")
        for period_key, n in PERIODS.items():
            window = dates_20[-n:]
            count = sum(1 for d in window if code in date_code_sets.get(d, set()))
            stock[f"{period_key}_count"] = count

    return records


# ---------------------------------------------------------------------------
# Market sentiment
# ---------------------------------------------------------------------------

async def compute_sentiment(date_str: str) -> dict:
    prev_date = get_prev_trading_date(date_str)
    today_records = await fetch_zt_data(date_str)

    total = len(today_records)
    if total == 0:
        return {
            "total_zt": 0,
            "broke_count": 0,
            "broke_rate": 0.0,
            "promotion_rate": None,
            "prev_date": prev_date,
        }

    broke_stocks = sum(1 for r in today_records if (r.get("break_count") or 0) > 0)
    broke_rate = round(broke_stocks / total * 100, 1) if total > 0 else 0.0

    # Promotion rate: yesterday's ZT stocks appearing in today's ZT pool
    promotion_rate = None
    if prev_date:
        try:
            prev_records = await fetch_zt_data(prev_date)
            if prev_records:
                prev_codes = {r["code"] for r in prev_records if r.get("code")}
                today_codes = {r["code"] for r in today_records if r.get("code")}
                promoted = len(prev_codes & today_codes)
                promotion_rate = round(promoted / len(prev_codes) * 100, 1)
        except Exception:
            pass

    return {
        "total_zt": total,
        "broke_count": broke_stocks,
        "broke_rate": broke_rate,
        "promotion_rate": promotion_rate,
        "prev_date": prev_date,
    }


# ---------------------------------------------------------------------------
# Consecutive tier aggregation
# ---------------------------------------------------------------------------

def compute_tiers(records: list[dict]) -> list[dict]:
    tiers: dict[int, list] = {}
    for r in records:
        n = int(r.get("consecutive") or 1)
        bucket = n if n < 5 else 5
        tiers.setdefault(bucket, []).append(r)

    result = []
    for n in sorted(tiers.keys()):
        label = f"{n}板" if n < 5 else "5板+"
        stocks = tiers[n]
        result.append({
            "label": label,
            "consecutive": n,
            "count": len(stocks),
            "codes": [s["code"] for s in stocks if s.get("code")],
        })
    return result


# ---------------------------------------------------------------------------
# Market trend (past N days daily stats)
# ---------------------------------------------------------------------------

async def compute_market_trend(anchor_date: str, days: int = 20) -> list[dict]:
    """Returns per-day stats for the past `days` trading days ending at anchor_date."""
    all_dates = get_previous_n_trading_dates(anchor_date, days + 1)
    if len(all_dates) < 2:
        return []

    sem = asyncio.Semaphore(MAX_PARALLEL_FETCHES)

    async def fetch_safe(d: str):
        async with sem:
            try:
                return d, await fetch_zt_data(d)
            except Exception:
                return d, []

    results = await asyncio.gather(*[fetch_safe(d) for d in all_dates])
    date_data: dict[str, list] = dict(results)

    dates = all_dates[1:]  # the `days` dates we compute stats for
    trend = []
    for i, d in enumerate(dates):
        recs = date_data.get(d, [])
        prev_recs = date_data.get(all_dates[i], [])

        total = len(recs)
        broke = sum(1 for r in recs if (r.get("break_count") or 0) > 0)

        promotion_rate = None
        if prev_recs:
            prev_codes = {r["code"] for r in prev_recs if r.get("code")}
            today_codes = {r["code"] for r in recs if r.get("code")}
            if prev_codes:
                promoted = len(prev_codes & today_codes)
                promotion_rate = round(promoted / len(prev_codes) * 100, 1)

        trend.append({
            "date": d,
            "total_zt": total,
            "broke_rate": round(broke / total * 100, 1) if total > 0 else 0.0,
            "promotion_rate": promotion_rate,
        })

    return trend


# ---------------------------------------------------------------------------
# Stock timeline (30-day per-day ZT status for one stock)
# ---------------------------------------------------------------------------

async def fetch_stock_timeline(code: str, anchor_date: str, days: int = 30) -> list[dict]:
    """Returns day-by-day ZT appearance for a single stock over past `days` trading days."""
    dates = get_previous_n_trading_dates(anchor_date, days)

    sem = asyncio.Semaphore(MAX_PARALLEL_FETCHES)

    async def fetch_safe(d: str):
        async with sem:
            try:
                return d, await fetch_zt_data(d)
            except Exception:
                return d, []

    results = await asyncio.gather(*[fetch_safe(d) for d in dates])

    timeline = []
    for d, recs in results:
        stock = next((r for r in recs if r.get("code") == code), None)
        timeline.append({
            "date": d,
            "hit_zt": stock is not None,
            "consecutive": stock.get("consecutive", 0) if stock else 0,
            "break_count": stock.get("break_count", 0) if stock else 0,
            "first_time": stock.get("first_time") if stock else None,
            "score": stock.get("score") if stock else None,
        })

    return timeline


# ---------------------------------------------------------------------------
# Next-day performance stats (grouped by tier)
# ---------------------------------------------------------------------------

async def compute_nextday_stats(date_str: str) -> dict:
    """For each consecutive tier in today's pool, compute % that hit ZT again next day."""
    next_date = get_next_trading_date(date_str)
    today_str = datetime.date.today().strftime("%Y%m%d")

    if next_date is None or next_date > today_str:
        return {"available": False, "reason": "next_date_not_yet"}

    today_recs, next_recs = await asyncio.gather(
        fetch_zt_data(date_str),
        fetch_zt_data(next_date),
    )

    if not today_recs:
        return {"available": False, "reason": "no_data"}

    next_codes = {r["code"] for r in next_recs if r.get("code")}

    tier_map: dict[int, list] = {}
    for r in today_recs:
        c = int(r.get("consecutive") or 1)
        bucket = c if c <= 3 else 4
        tier_map.setdefault(bucket, []).append(r)

    tiers = []
    for bucket in sorted(tier_map.keys()):
        stocks = tier_map[bucket]
        label = f"{bucket}板" if bucket <= 3 else "4板+"
        hit = sum(1 for s in stocks if s.get("code") in next_codes)
        tiers.append({
            "label": label,
            "total": len(stocks),
            "hit_next_zt": hit,
            "rate": round(hit / len(stocks) * 100, 1),
        })

    return {
        "available": True,
        "date": date_str,
        "next_date": next_date,
        "tiers": tiers,
    }


# ---------------------------------------------------------------------------
# K-line (daily OHLCV for a single stock)
# ---------------------------------------------------------------------------

def _fetch_kline_sync(code: str, start_date: str, end_date: str) -> pd.DataFrame:
    return ak.stock_zh_a_hist(
        symbol=code,
        period="daily",
        start_date=start_date,
        end_date=end_date,
        adjust="qfq",
    )


async def fetch_stock_kline(code: str, anchor_date: str, days: int = 40) -> list[dict]:
    """Fetch daily OHLCV for a stock covering the last `days` trading days."""
    date_list = get_previous_n_trading_dates(anchor_date, days)
    if not date_list:
        return []
    start_date = date_list[0]

    try:
        df = await asyncio.wait_for(
            _run_sync(_fetch_kline_sync, code, start_date, anchor_date),
            timeout=25,
        )
    except Exception as e:
        logger.error("Failed to fetch kline for %s: %s", code, e)
        return []

    if df is None or df.empty:
        return []

    records = []
    for _, row in df.iterrows():
        try:
            records.append({
                "date": str(row["日期"]).replace("-", ""),
                "open":  float(row["开盘"]),
                "high":  float(row["最高"]),
                "low":   float(row["最低"]),
                "close": float(row["收盘"]),
                "volume": int(row["成交量"]),
                "pct":   float(row.get("涨跌幅", 0)),
            })
        except Exception:
            continue
    return records


# ---------------------------------------------------------------------------
# Consecutive tier promotion stats (aggregate over past N days)
# ---------------------------------------------------------------------------

async def compute_tier_promotion_stats(anchor_date: str, days: int = 30) -> list[dict]:
    """
    For each consecutive tier (1,2,3,4+), compute the historical rate at which
    stocks advanced to the next tier the following trading day, over the past
    `days` trading days.
    """
    all_dates = get_previous_n_trading_dates(anchor_date, days + 1)
    if len(all_dates) < 2:
        return []

    sem = asyncio.Semaphore(MAX_PARALLEL_FETCHES)

    async def fetch_safe(d: str):
        async with sem:
            try:
                return d, await fetch_zt_data(d)
            except Exception:
                return d, []

    results = await asyncio.gather(*[fetch_safe(d) for d in all_dates])
    date_data: dict[str, list] = dict(results)

    tier_stats: dict[int, dict] = {}

    for i in range(len(all_dates) - 1):
        d      = all_dates[i]
        d_next = all_dates[i + 1]
        today_recs = date_data.get(d, [])
        next_recs  = date_data.get(d_next, [])
        # Map code → consecutive for next day
        next_map = {
            r["code"]: int(r.get("consecutive") or 1)
            for r in next_recs if r.get("code")
        }

        for r in today_recs:
            code = r.get("code")
            if not code:
                continue
            c = int(r.get("consecutive") or 1)
            bucket = c if c <= 3 else 4
            tier_stats.setdefault(bucket, {"attempts": 0, "advanced": 0})
            tier_stats[bucket]["attempts"] += 1
            # Advanced = appeared next day with consecutive == c + 1
            if next_map.get(code) == c + 1:
                tier_stats[bucket]["advanced"] += 1

    result = []
    for bucket in sorted(tier_stats.keys()):
        s = tier_stats[bucket]
        label = f"{bucket}板→{bucket+1}板" if bucket <= 3 else "4板+→5板+"
        result.append({
            "label":    label,
            "from_tier": bucket,
            "attempts": s["attempts"],
            "advanced": s["advanced"],
            "rate": round(s["advanced"] / s["attempts"] * 100, 1) if s["attempts"] else 0.0,
            "days": days,
        })
    return result


# ---------------------------------------------------------------------------
# Feature-stratified rolling window backtest
# ---------------------------------------------------------------------------

def _seal_bucket(first_time) -> str:
    try:
        t = int(first_time or "999999")
    except (ValueError, TypeError):
        return "盘中"
    if t < 93100:  return "竞价"
    if t < 100000: return "早盘"
    return "盘中"


def _consec_bucket(consecutive) -> str:
    try:
        c = int(consecutive or 1)
    except (ValueError, TypeError):
        c = 1
    if c == 1: return "首板"
    if c == 2: return "二板"
    return "三板+"


def _break_bucket(break_count) -> str:
    try:
        b = int(break_count or 0)
    except (ValueError, TypeError):
        b = 0
    if b == 0: return "0炸"
    if b == 1: return "1炸"
    return "2炸+"


async def compute_feature_backtest(anchor_date: str, window: int = 20) -> dict:
    """
    Rolling window feature-stratified backtest (ZT pool data only, no K-line).

    For each T0 day in the past `window` trading days, checks whether each
    涨停 stock re-appeared in the T+1 ZT pool (proxy for a profitable next-day
    trade).  Results are stratified by seal time, consecutive tier, and break
    count, plus a cross-dimension lookup table keyed by "seal|consec".
    """
    today_str = datetime.date.today().strftime("%Y%m%d")

    # Build (t0, t1) pairs where T+1 has already closed
    raw_dates = get_previous_n_trading_dates(anchor_date, window + 3)
    pairs: list[tuple[str, str]] = []
    for d in raw_dates:
        t1 = get_next_trading_date(d)
        if t1 and t1 <= today_str:
            pairs.append((d, t1))
    pairs = pairs[-window:]

    if not pairs:
        return {"window": window, "total": 0}

    needed: set[str] = set()
    for t0, t1 in pairs:
        needed.add(t0)
        needed.add(t1)

    sem = asyncio.Semaphore(MAX_PARALLEL_FETCHES)

    async def fetch_safe(d: str):
        async with sem:
            try:
                return d, await fetch_zt_data(d)
            except Exception:
                return d, []

    zt_results = await asyncio.gather(*[fetch_safe(d) for d in needed])
    zt_pools: dict[str, list] = dict(zt_results)

    # Build flat outcome records
    all_records: list[dict] = []
    for t0, t1 in pairs:
        t1_codes = {r["code"] for r in zt_pools.get(t1, []) if r.get("code")}
        for r in zt_pools.get(t0, []):
            code = r.get("code")
            if not code:
                continue
            all_records.append({
                "seal":   _seal_bucket(r.get("first_time")),
                "consec": _consec_bucket(r.get("consecutive")),
                "breaks": _break_bucket(r.get("break_count")),
                "t1_hit": code in t1_codes,
            })

    if not all_records:
        return {"window": window, "total": 0}

    def agg(recs: list) -> dict:
        n = len(recs)
        hits = sum(1 for r in recs if r["t1_hit"])
        return {"n": n, "t1_zt_rate": round(hits / n * 100, 1)}

    ORDER = {
        "seal":   ["竞价", "早盘", "盘中"],
        "consec": ["首板", "二板", "三板+"],
        "breaks": ["0炸", "1炸", "2炸+"],
    }

    def build_dim(key: str) -> list[dict]:
        groups: dict[str, list] = defaultdict(list)
        for r in all_records:
            groups[r[key]].append(r)
        return [
            {"label": k, **agg(groups[k])}
            for k in ORDER[key] if k in groups
        ]

    # Cross-dimension table "seal|consec" → stats (for per-stock lookup in UI)
    cross_groups: dict[str, list] = defaultdict(list)
    for r in all_records:
        cross_groups[f"{r['seal']}|{r['consec']}"].append(r)
    cross = {k: agg(v) for k, v in cross_groups.items()}

    return {
        "window":       window,
        "anchor_date":  anchor_date,
        "total":        len(all_records),
        "date_from":    pairs[0][0],
        "date_to":      pairs[-1][0],
        "by_seal":      build_dim("seal"),
        "by_consec":    build_dim("consec"),
        "by_breaks":    build_dim("breaks"),
        "cross":        cross,
    }
