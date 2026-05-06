import sys
import types
import json
import os
import asyncio
import datetime
import logging
import random

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


async def _fetch_with_retry(fn, *args, retries: int = 3, timeout: float = 25) -> pd.DataFrame:
    """Run a sync AKShare fetch with exponential backoff on transient network errors.

    Delays: 2s, 4s (+ up to 1s jitter each). Timeout errors are not retried.
    """
    for attempt in range(retries):
        try:
            return await asyncio.wait_for(_run_sync(fn, *args), timeout=timeout)
        except asyncio.TimeoutError:
            raise RuntimeError("请求超时，请稍后重试")
        except Exception as e:
            if attempt == retries - 1:
                raise
            wait = 2.0 * (2 ** attempt) + random.uniform(0, 1.0)
            logger.warning(
                "AKShare fetch attempt %d/%d failed (%.1fs retry): %s",
                attempt + 1, retries, wait, e,
            )
            await asyncio.sleep(wait)
    raise RuntimeError("unreachable")


async def warm_zt_cache(days: int = 35) -> None:
    """
    Background task: pre-fetch ZT pool data for the past `days` trading days.
    Historical dates are permanently cached, so already-cached dates are skipped
    instantly. Only uncached dates trigger an AKShare call.
    """
    today = datetime.date.today().strftime("%Y%m%d")
    dates = get_previous_n_trading_dates(today, days)
    sem = asyncio.Semaphore(3)   # gentler than MAX_PARALLEL_FETCHES for warmup

    async def fetch_one(d: str) -> None:
        # Skip if already cached — _read_cache returns non-None for valid cache
        path = os.path.join(CACHE_DIR, f"zt_{d}.json")
        if os.path.exists(path):
            return
        async with sem:
            try:
                await fetch_zt_data(d)
                logger.info("Cache warmed: %s", d)
            except Exception as e:
                logger.warning("Cache warm failed for %s: %s", d, e)

    await asyncio.gather(*[fetch_one(d) for d in dates])
    logger.info("ZT cache warmup done (%d dates checked)", len(dates))


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
            await asyncio.sleep(random.uniform(0.0, 0.2))
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
            await asyncio.sleep(random.uniform(0.0, 0.2))
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
            await asyncio.sleep(random.uniform(0.0, 0.2))
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
    # Beijing Stock Exchange stocks (8xxxxx, 43xxxx) use a different API
    if code.startswith("8") or code.startswith("43"):
        return ak.stock_bj_a_hist(
            symbol=code, period="daily",
            start_date=start_date, end_date=end_date,
            adjust="qfq",
        )
    return ak.stock_zh_a_hist(
        symbol=code,
        period="daily",
        start_date=start_date,
        end_date=end_date,
        adjust="qfq",
    )


def _fetch_opens_sync(code: str, start_date: str, end_date: str) -> pd.DataFrame:
    """Raw (unadjusted) OHLCV — used so open prices are comparable to ZT pool limit prices."""
    if code.startswith("8") or code.startswith("43"):
        return ak.stock_bj_a_hist(
            symbol=code, period="daily",
            start_date=start_date, end_date=end_date,
            adjust="",
        )
    return ak.stock_zh_a_hist(
        symbol=code, period="daily",
        start_date=start_date, end_date=end_date,
        adjust="",
    )


async def fetch_stock_kline(code: str, anchor_date: str, days: int = 40) -> list[dict]:
    """Fetch daily OHLCV for a stock covering the last `days` trading days."""
    date_list = get_previous_n_trading_dates(anchor_date, days)
    if not date_list:
        return []
    start_date = date_list[0]

    try:
        df = await _fetch_with_retry(_fetch_kline_sync, code, start_date, anchor_date)
    except RuntimeError:
        raise
    except Exception as e:
        logger.error("Failed to fetch kline for %s: %s", code, e)
        raise RuntimeError(f"数据获取失败：{e}")

    if df is None or df.empty:
        return []

    records = []
    skipped = 0
    for _, row in df.iterrows():
        try:
            pct_raw = row.get("涨跌幅")
            records.append({
                "date":   str(row["日期"]).replace("-", ""),
                "open":   float(row["开盘"]),
                "high":   float(row["最高"]),
                "low":    float(row["最低"]),
                "close":  float(row["收盘"]),
                "volume": int(row["成交量"]),
                "pct":    float(pct_raw) if pct_raw is not None else 0.0,
            })
        except Exception:
            skipped += 1
    if skipped:
        logger.warning("kline %s: skipped %d malformed rows", code, skipped)
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
            await asyncio.sleep(random.uniform(0.0, 0.2))
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


def _opens_path(date_str: str) -> str:
    return os.path.join(CACHE_DIR, f"opens_{date_str}.json")

def _load_opens(date_str: str) -> dict[str, float]:
    path = _opens_path(date_str)
    if not os.path.exists(path):
        return {}
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return {}

def _save_opens(date_str: str, opens: dict[str, float]) -> None:
    try:
        with open(_opens_path(date_str), "w") as f:
            json.dump(opens, f)
    except Exception as e:
        logger.warning("Failed to save opens for %s: %s", date_str, e)


async def compute_feature_backtest(anchor_date: str, window: int = 20) -> dict:
    """
    Full three-day rolling backtest:
      T0 → observe 涨停 (features + limit-up price)
      T+1 → buy at open  (T1 open premium vs T0 limit price)
      T+2 → sell at open (T2 open return vs T1 open = actual P&L proxy)

    Also tracks T+1 re-limit rate as a secondary signal.
    Open prices are cached per-date in cache/opens_YYYYMMDD.json.
    """
    today_str = datetime.date.today().strftime("%Y%m%d")

    # Build (T0, T1, T2) triples where T+2 has already closed
    raw_dates = get_previous_n_trading_dates(anchor_date, window + 4)
    triples: list[tuple[str, str, str]] = []
    for d in raw_dates:
        t1 = get_next_trading_date(d)
        t2 = get_next_trading_date(t1) if t1 else None
        if t2 and t2 <= today_str:
            triples.append((d, t1, t2))
    triples = triples[-window:]

    if not triples:
        return {"window": window, "total": 0}

    t0_dates = [t[0] for t in triples]
    t1_dates = list(dict.fromkeys(t[1] for t in triples))
    t2_dates = list(dict.fromkeys(t[2] for t in triples))
    needed_t1t2 = set(t1_dates) | set(t2_dates)

    # ── Fetch ZT pools for T0 and T1 ────────────────────────────────
    sem = asyncio.Semaphore(MAX_PARALLEL_FETCHES)

    async def fetch_zt_safe(d: str):
        async with sem:
            try:
                return d, await fetch_zt_data(d)
            except Exception:
                return d, []

    needed_zt = set(t0_dates) | set(t1_dates)
    zt_results = await asyncio.gather(*[fetch_zt_safe(d) for d in needed_zt])
    zt_pools: dict[str, list] = dict(zt_results)

    # ── Collect unique codes from all T0 pools ───────────────────────
    all_t0_codes: set[str] = set()
    for t0, _, _ in triples:
        for r in zt_pools.get(t0, []):
            if r.get("code"):
                all_t0_codes.add(r["code"])

    # ── Load existing per-date opens caches ──────────────────────────
    opens_by_date: dict[str, dict[str, float]] = {
        d: _load_opens(d) for d in needed_t1t2
    }

    # Determine which codes are missing for any T1/T2 date
    missing_codes: set[str] = {
        c for c in all_t0_codes
        for d in needed_t1t2
        if c not in opens_by_date[d]
    }

    # ── Batch-fetch missing: one K-line call per unique code ─────────
    if missing_codes:
        fetch_start = min(t1_dates + t2_dates)
        fetch_end   = max(t1_dates + t2_dates)

        async def fetch_code_opens(code: str):
            async with sem:
                await asyncio.sleep(random.uniform(0.05, 0.4))
                try:
                    df = await _fetch_with_retry(_fetch_opens_sync, code, fetch_start, fetch_end)
                    if df is None or df.empty:
                        return code, {}
                    return code, {
                        str(row["日期"]).replace("-", ""): float(row["开盘"])
                        for _, row in df.iterrows()
                    }
                except Exception as e:
                    logger.warning("Opens fetch failed %s: %s", code, e)
                    return code, {}

        kline_results = await asyncio.gather(
            *[fetch_code_opens(c) for c in missing_codes]
        )

        # Merge into per-date opens and persist cache
        new_by_date: dict[str, dict[str, float]] = defaultdict(dict)
        for code, date_map in kline_results:
            for date, open_price in date_map.items():
                if date in needed_t1t2:
                    new_by_date[date][code] = open_price
                    opens_by_date[date][code] = open_price

        for date, new_opens in new_by_date.items():
            merged = opens_by_date.get(date, {})
            merged.update(new_opens)
            _save_opens(date, merged)

    # ── Build flat outcome records ────────────────────────────────────
    all_records: list[dict] = []
    for t0, t1, t2 in triples:
        t1_zt_codes = {r["code"] for r in zt_pools.get(t1, []) if r.get("code")}
        t1_opens    = opens_by_date.get(t1, {})
        t2_opens    = opens_by_date.get(t2, {})

        for r in zt_pools.get(t0, []):
            code = r.get("code")
            if not code:
                continue
            record: dict = {
                "seal":   _seal_bucket(r.get("first_time")),
                "consec": _consec_bucket(r.get("consecutive")),
                "breaks": _break_bucket(r.get("break_count")),
                "t1_hit": code in t1_zt_codes,
            }
            t0_price = r.get("price")
            t1_open  = t1_opens.get(code)
            t2_open  = t2_opens.get(code)

            if t0_price and t1_open:
                t0_p = float(t0_price)
                record["t1_prem"] = round((t1_open - t0_p) / t0_p * 100, 2)
                if t2_open:
                    record["t2_ret"] = round((t2_open - t1_open) / t1_open * 100, 2)

            all_records.append(record)

    if not all_records:
        return {"window": window, "total": 0}

    # ── Aggregate helper ─────────────────────────────────────────────
    def agg(recs: list) -> dict:
        n = len(recs)
        hits = sum(1 for r in recs if r.get("t1_hit"))
        out: dict = {"n": n, "t1_zt_rate": round(hits / n * 100, 1)}

        prems = [r["t1_prem"] for r in recs if "t1_prem" in r]
        rets  = [r["t2_ret"]  for r in recs if "t2_ret"  in r]
        if prems:
            out["t1_prem_mean"] = round(sum(prems) / len(prems), 2)
        if rets:
            wins = sum(1 for x in rets if x > 0)
            out["t2_win_rate"] = round(wins / len(rets) * 100, 1)
            out["t2_ret_mean"] = round(sum(rets) / len(rets), 2)
            out["t2_n"]        = len(rets)
        return out

    ORDER = {
        "seal":   ["竞价", "早盘", "盘中"],
        "consec": ["首板", "二板", "三板+"],
        "breaks": ["0炸", "1炸", "2炸+"],
    }

    def build_dim(key: str) -> list[dict]:
        groups: dict[str, list] = defaultdict(list)
        for r in all_records:
            groups[r[key]].append(r)
        return [{"label": k, **agg(groups[k])} for k in ORDER[key] if k in groups]

    cross_groups: dict[str, list] = defaultdict(list)
    for r in all_records:
        cross_groups[f"{r['seal']}|{r['consec']}"].append(r)
    cross = {k: agg(v) for k, v in cross_groups.items()}

    return {
        "window":      window,
        "anchor_date": anchor_date,
        "total":       len(all_records),
        "date_from":   triples[0][0],
        "date_to":     triples[-1][0],
        "by_seal":     build_dim("seal"),
        "by_consec":   build_dim("consec"),
        "by_breaks":   build_dim("breaks"),
        "cross":       cross,
    }


# ---------------------------------------------------------------------------
# ZT pool trend scoring (medium-short term, independent dimension)
# ---------------------------------------------------------------------------

async def compute_zt_trend(date: str) -> list[dict]:
    """Compute trend scores for every stock in the ZT pool on `date`.

    Score (0-100):
      MA alignment (MA5>MA10>MA20)  → up to +35
      Price above MA20              → up to +20
      Higher-High structure (10d)   → up to +25
      Volume expansion (5d vs 20d)  → up to +20

    Labels: 上升 (≥70) / 震荡 (40-69) / 下降 (<40)
    """
    cache_path = _cache_path(date, prefix="trend")
    if os.path.exists(cache_path):
        try:
            with open(cache_path) as f:
                cached = json.load(f)
            if _is_cache_valid(cached, date):
                return cached.get("records", [])
        except Exception:
            pass

    records = await fetch_zt_data(date)
    if not records:
        return []

    date_list = get_previous_n_trading_dates(date, 30)
    if not date_list:
        return []
    start_date = date_list[0]

    sem = asyncio.Semaphore(MAX_PARALLEL_FETCHES)

    async def fetch_one(code: str):
        async with sem:
            await asyncio.sleep(random.uniform(0.05, 0.4))
            try:
                df = await _fetch_with_retry(_fetch_kline_sync, code, start_date, date)
                return code, df
            except Exception as e:
                logger.warning("Trend kline failed %s: %s", code, e)
                return code, None

    results = await asyncio.gather(*[fetch_one(r["code"]) for r in records])

    trend_records = []
    for code, df in results:
        if df is None or df.empty or len(df) < 10:
            trend_records.append({"code": code, "trend_score": 50, "trend_label": "震荡"})
            continue

        closes  = [float(v) for v in df["收盘"].tolist()]
        volumes = [float(v) for v in df["成交量"].tolist()]

        def ma(n: int) -> float | None:
            return sum(closes[-n:]) / n if len(closes) >= n else None

        ma5, ma10, ma20 = ma(5), ma(10), ma(20)
        score = 0

        # MA alignment
        if ma5 and ma10 and ma20:
            if ma5 > ma10 > ma20:
                score += 35
            elif ma5 > ma20:
                score += 15
        elif ma5 and ma10 and ma5 > ma10:
            score += 20

        # Price vs MA20
        if ma20 and closes[-1] > ma20:
            score += 20
        elif ma5 and closes[-1] > ma5:
            score += 10

        # Higher-High: avg of last 3 closes vs avg of closes[−13:−8]
        if len(closes) >= 13:
            recent  = sum(closes[-3:]) / 3
            earlier = sum(closes[-13:-8]) / 5
            if recent > earlier * 1.01:
                score += 25
            elif recent > earlier:
                score += 12

        # Volume expansion
        if len(volumes) >= 20:
            vol5  = sum(volumes[-5:]) / 5
            vol20 = sum(volumes[-20:]) / 20
            if vol5 > vol20 * 1.1:
                score += 20
            elif vol5 > vol20:
                score += 10

        label = "上升" if score >= 70 else "震荡" if score >= 40 else "下降"
        trend_records.append({"code": code, "trend_score": score, "trend_label": label})

    try:
        with open(cache_path, "w") as f:
            json.dump({
                "date": date,
                "fetched_at": datetime.datetime.now().isoformat(),
                "records": trend_records,
            }, f)
    except Exception as e:
        logger.warning("Failed to save trend cache for %s: %s", date, e)

    return trend_records
