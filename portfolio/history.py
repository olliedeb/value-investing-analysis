"""Reconstruct the strategy's history: point-in-time rankings for past dates.

The problem it solves: stock_stats only keeps each ticker's latest
evaluation, so there is no ranking history to backtest on. Replaying
today's ranking over past prices would be look-ahead bias. This module
rebuilds what the pipeline *would have computed* on each past rebalance
date, from the same raw material the live pipeline uses, and writes the
reconstructed rankings into ranking_snapshots (source='backfill') so the
backtest can run over years instead of weeks.

How a past date is reconstructed (for each ticker, for a rebalance date D):
  * SEC facts: the same 10-K facts load_us_gaap() fetches, but truncated
    to what was knowable on D -- only facts filed on or before D, with the
    anchor = the latest fiscal year-end whose 10-K was filed by D. A
    restatement filed after D is not used (the live pipeline uses the
    latest filing, so this is strictly more point-in-time than live).
  * DCF, debt/equity, ROE and reinvestment efficiency: the exact functions
    from repeatable.py (dcf, get_debt_to_equity, get_roe,
    retained_earnings_check) run on the truncated facts, so the numbers are
    the live numbers, just as of D instead of today.
  * P/E, P/B, P/DCF: from the price and market cap on D (Yahoo history +
    shares-outstanding history, cached under data/history/). P/E uses the
    latest annual EPS (net income / shares outstanding at the fiscal
    year-end) -- the live pipeline uses Yahoo's trailing (TTM) P/E, which
    has no historical equivalent in the 10-K-only data, so annual EPS is
    the documented approximation. P/B and P/DCF are exact given the data.
  * Screen: the Stage 1 screen is re-applied as of D (market cap > $2B,
    0 < P/E <= 20, P/B < 3, sector in the 9 screened sectors). A ticker
    that looked expensive on D is not in D's snapshot.
  * Ranking: the same percentile rules as recompute_rankings() (needs >= 4
    of 6 metrics, missing = 0.5, negative equity excluded, rank method
    'min'), applied to that date's screened set.

Efficiency: fundamentals change only when a new 10-K arrives, but prices
move daily. Per ticker there is ONE SEC download (cached in sec_cache/),
ONE price-history download and ONE shares download (cached under
data/history/), then cheap per-date arithmetic. A run over 2016-today for
~600 tickers is mostly downloads: expect tens of minutes the first time
(Yahoo rate limits), minutes on a re-run because everything is cached.

Resumable: per-ticker results are cached in data/history/rows_cache.json
and completed snapshot dates in data/history/backfill_progress.json, so a
stopped run picks up where it left off. --force redoes everything.

RUN IN VS CODE: edit the constants below, then press Run. No terminal
flags needed; the CLI flags in main() only default to the constants.

Known limits (read before trusting a backtest over this history):
  * Survivorship: only tickers in today's stock_stats can be
    reconstructed. Companies that died, were acquired, or were delisted
    are missing from early dates, which flatters the backtest.
  * Sector/industry/exchange are today's values (a ticker's sector rarely
    changes; its exchange is not stored, so the NYSE/Nasdaq-GS screen is
    not re-applied -- the market-cap/P/E/P/B screen is).
  * P/E uses annual EPS, not TTM EPS (see above).
  * Prices are Yahoo unadjusted closes; shares outstanding are Yahoo's
    sparse samples (nearest sample wins), the same approximation the live
    reinvestment-efficiency check uses. A split after D makes that date's
    market cap unknowable, so the ticker is skipped for that date.
"""

import json
import logging
import math
import os
import sys
import time
from datetime import date, timedelta
from pathlib import Path

# ---------------------------------------------------------------------------
# RUN IN VS CODE: edit these, then press Run.
# ---------------------------------------------------------------------------
START_YEAR = 2016  # first year to reconstruct
END_DATE = ""  # "" = today
REBALANCE = "quarterly"  # "monthly" | "quarterly" | "annual" | "2-year" | "3-year" | "5-year" | "Nm" (e.g. "18m")
MAX_TICKERS = 0  # 0 = every ticker in stock_stats; set small (e.g. 5) to trial it
MIN_MARKET_CAP = 2_000_000_000  # Stage 1 screen, as of the rebalance date
# No P/E or P/B gate here: the prescreen is only market cap + sector.
# P/E and P/B are ranking metrics (repeatable.py), not prescreen gates.
FORCE = False  # True to redo dates/tickers already backfilled
SELF_TEST = False  # True to verify the reconstruction on synthetic data

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

from dotenv import load_dotenv  # noqa: E402

try:
    load_dotenv(ROOT / "info.env")
except ImportError:
    pass

import db  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
import repeatable as rp  # noqa: E402
import requests as rq  # noqa: E402
import yfinance as yf  # noqa: E402
from portfolio.backtest import _rebalance_dates as _engine_rebalance_dates  # noqa: E402
from portfolio.backtest import _rebalance_months  # noqa: E402
from portfolio.snapshots import load_snapshots, write_snapshot  # noqa: E402
from sqlalchemy import text  # noqa: E402

log = logging.getLogger(__name__)

HISTORY_DIR = Path(__file__).resolve().parent / "data" / "history"
PRICES_DIR = HISTORY_DIR / "prices"
SHARES_DIR = HISTORY_DIR / "shares"
PROGRESS_PATH = HISTORY_DIR / "backfill_progress.json"
ROWS_CACHE_PATH = HISTORY_DIR / "rows_cache.json"

# The 9 sectors Stage 1 screens for (see initial_screen.py / README).
STAGE1_SECTORS = {
    "Real Estate", "Energy", "Financial Services", "Consumer Cyclical",
    "Consumer Defensive", "Basic Materials", "Industrials", "Utilities",
    "Communication Services",
}
YF_PAUSE = 0.25  # pause after a ticker that needed a fresh Yahoo download


# ---------------------------------------------------------------------------
# Yahoo history (cached)
# ---------------------------------------------------------------------------
def _tz_naive(index):
    index = pd.to_datetime(index)
    return index.tz_localize(None) if index.tz is not None else index


def load_price_history(ticker, start, end):
    """Daily closes + splits + dividends, cached under data/history/prices/.

    Close is split-adjusted but NOT dividend-adjusted (auto_adjust=False)
    on purpose: market cap is price x shares, and dividend-adjusting old
    prices downward would understate historical market caps for dividend
    payers. The backtest engine uses Adj Close for returns -- that column
    is cached here too, alongside Dividends, so a total-return backtest
    can be rebuilt from this cache without re-downloading. A cache file
    without a Dividends column is treated as stale and re-downloaded.
    """
    PRICES_DIR.mkdir(parents=True, exist_ok=True)
    path = PRICES_DIR / f"{ticker.replace('^', '').replace('/', '_')}.csv"
    start_ts = pd.Timestamp(start) - pd.Timedelta(days=30)
    end_ts = pd.Timestamp(end) + pd.Timedelta(days=1)
    if path.exists():
        try:
            df = pd.read_csv(path, parse_dates=["Date"], index_col="Date")
            if (len(df) and "Dividends" in df.columns
                    and df.index.min() <= start_ts and df.index.max() >= end_ts):
                return df
        except Exception:  # noqa: BLE001 -- corrupt cache: re-download below
            pass
    try:
        hist = yf.Ticker(ticker).history(
            start=start_ts.date().isoformat(), end=end_ts.date().isoformat(),
            auto_adjust=False)
    except Exception as exc:  # noqa: BLE001
        log.warning("%s: price history download failed: %r", ticker, exc)
        return None
    if hist is None or hist.empty:
        return None
    hist.index = _tz_naive(hist.index)
    cols = ["Close"]
    if "Adj Close" in hist.columns:
        cols.append("Adj Close")
    if "Stock Splits" in hist.columns:
        cols.append("Stock Splits")
    if "Dividends" in hist.columns:
        cols.append("Dividends")
    out = hist[cols].copy()
    if "Dividends" in out.columns:
        out["Dividends"] = out["Dividends"].fillna(0.0)
    out.index.name = "Date"
    out.to_csv(path)
    return out


def load_shares_history(ticker, start, end):
    """Shares outstanding over time (Yahoo's sparse samples), cached."""
    SHARES_DIR.mkdir(parents=True, exist_ok=True)
    path = SHARES_DIR / f"{ticker.replace('^', '').replace('/', '_')}.csv"
    start_ts, end_ts = pd.Timestamp(start), pd.Timestamp(end)
    if path.exists():
        try:
            s = pd.read_csv(path, parse_dates=["Date"], index_col="Date")["Shares"]
            if len(s) and s.index.min() <= start_ts and s.index.max() >= end_ts:
                return s
        except Exception:  # noqa: BLE001 -- corrupt cache: re-download below
            pass
    try:
        shares = yf.Ticker(ticker).get_shares_full(
            start=(start_ts - pd.Timedelta(days=400)).date().isoformat(),
            end=(end_ts + pd.Timedelta(days=30)).date().isoformat())
    except Exception as exc:  # noqa: BLE001
        log.warning("%s: shares history download failed: %r", ticker, exc)
        return None
    if shares is None or len(shares) == 0:
        return None
    shares = shares.dropna()
    if shares.empty:
        return None
    shares.index = _tz_naive(shares.index)
    shares.name = "Shares"
    shares.to_csv(path, header=True)
    return shares


def _close_on(hist, as_of):
    """Last close on or before `as_of` (within 14 days), else None."""
    if hist is None or hist.empty:
        return None
    idx = hist.index[hist.index <= pd.Timestamp(as_of)]
    if len(idx) == 0:
        return None
    if (pd.Timestamp(as_of) - idx[-1]).days > 14:
        return None
    # NB: the close AT idx[-1], not the last row of the whole series.
    return float(hist["Close"].iloc[len(idx) - 1])


def _shares_on(shares, as_of):
    """Nearest shares-outstanding sample to `as_of`, else None."""
    if shares is None or len(shares) == 0:
        return None
    pos = int(np.abs((shares.index - pd.Timestamp(as_of)).total_seconds()).argmin())
    return float(shares.iloc[pos])


def _market_cap_on(price_hist, shares_hist, as_of):
    """Market cap on `as_of`, or None when unknowable.

    Mirrors repeatable.get_market_caps: a split after the date makes
    price x shares unreliable (Yahoo's closes are split-adjusted, its
    share counts are not reliably adjusted the same way), so skip.
    """
    price = _close_on(price_hist, as_of)
    if price is None:
        return None
    if price_hist is not None and "Stock Splits" in price_hist.columns:
        splits = price_hist["Stock Splits"]
        if (splits[price_hist.index > pd.Timestamp(as_of)] != 0).any():
            return None
    shares = _shares_on(shares_hist, as_of)
    if shares is None:
        return None
    return price * shares


# ---------------------------------------------------------------------------
# Point-in-time SEC facts
# ---------------------------------------------------------------------------
def _anchor_as_of(gaap, as_of):
    """Latest fiscal year-end whose 10-K was filed on or before `as_of`."""
    ends = set()
    for tag in rp.OCF_TAGS + rp.NI_TAGS:
        for e in gaap.get(tag, {}).get("USD", []):
            if e.get("form") not in rp.ANNUAL_FORMS or not e.get("end"):
                continue
            if e.get("filed") and rp._to_date(e["filed"]) > as_of:
                continue  # not public yet on this date
            if e.get("start"):
                days = (rp._to_date(e["end"]) - rp._to_date(e["start"])).days
                if not 350 <= days <= 380:
                    continue
            ends.add(e["end"])
    return max(ends) if ends else None


def _gaap_as_of(gaap, anchor, as_of):
    """Facts as knowable on `as_of`: period ends up to `anchor`, filed by `as_of`."""
    anchor_d = rp._to_date(anchor)
    out = {}
    for tag, units in gaap.items():
        kept_units = {}
        for unit, entries in units.items():
            kept = [e for e in entries
                    if e.get("end") and rp._to_date(e["end"]) <= anchor_d
                    and (not e.get("filed") or rp._to_date(e["filed"]) <= as_of)]
            if kept:
                kept_units[unit] = kept
        if kept_units:
            out[tag] = kept_units
    return out


def _re_dates(gaap, anchor):
    """(start, end) fiscal dates the reinvestment check uses, or (None, None)."""
    re_series = rp.annual_series(gaap, rp.RE_TAGS, False)
    _, end_date = rp.value_near(re_series, anchor)
    start_target = (rp._to_date(anchor)
                    - timedelta(days=round(365.25 * rp.LOOKBACK_YEARS))).isoformat()
    _, start_date = rp.value_near(re_series, start_target, tolerance_days=120)
    if end_date is None or start_date is None or start_date == end_date:
        return None, None
    return rp._to_date(start_date), rp._to_date(end_date)


def evaluate_anchor(ticker, gaap, anchor, as_of, price_hist, shares_hist, sector):
    """Anchor-fixed fundamentals: everything that does not depend on the
    day's price. `as_of` is the rebalance date (for the filed-date cutoff)."""
    filtered = _gaap_as_of(gaap, anchor, as_of)
    flags = []
    equity, _ = rp.value_near(rp.annual_series(filtered, rp.EQUITY_TAGS, False), anchor)
    if equity is not None and equity <= 0:
        flags.append("neg_equity")
    ni, _ = rp.value_near(rp.annual_series(filtered, rp.NI_TAGS, True), anchor)
    pack = {
        "anchor": anchor,
        "equity": equity,
        "ni": ni,
        "shares_at_anchor": _shares_on(shares_hist, rp._to_date(anchor)),
        "dcf_value": None,
        "debt_to_equity": None,
        "roe": None,
        "re_eff": None,
        "flags": flags,
    }
    pack["roe"] = rp._guard("roe", lambda: rp.get_roe(filtered), flags)
    re_start, re_end = _re_dates(filtered, anchor)
    mc = None
    if re_start is not None and re_end is not None:
        mc = {re_start: _market_cap_on(price_hist, shares_hist, re_start),
              re_end: _market_cap_on(price_hist, shares_hist, re_end)}
    pack["re_eff"] = rp._guard(
        "re_check",
        lambda: rp.retained_earnings_check(ticker, filtered, anchor, flags, market_caps=mc),
        flags)
    if sector in rp.FINANCIAL_SECTORS:
        flags.append("fin_sector_skip")  # DCF and D/E not meaningful for these
    else:
        pack["debt_to_equity"] = rp._guard(
            "de", lambda: rp.get_debt_to_equity(filtered, anchor, flags), flags)
        pack["dcf_value"] = rp._guard("dcf", lambda: rp.dcf(filtered, flags), flags)
    return pack


def metrics_on_date(ticker, pack, price_hist, shares_hist, as_of, sector, industry):
    """The six metrics for one rebalance date, from an anchor pack + the day's price.

    Returns None when the ticker cannot be valued on that date (no price or
    no market cap -- e.g. a split after the date).
    """
    price = _close_on(price_hist, as_of)
    mc = _market_cap_on(price_hist, shares_hist, as_of)
    if price is None or mc is None:
        return None
    flags = list(pack["flags"])
    eps = None
    if pack["ni"] is not None and pack["shares_at_anchor"]:
        eps = pack["ni"] / pack["shares_at_anchor"]
    pe = (price / eps) if eps and eps > 0 else None
    pb = (mc / pack["equity"]) if pack["equity"] and pack["equity"] > 0 else None
    p_dcf = None
    if pack["dcf_value"] is not None:
        if pack["dcf_value"] <= 0:
            flags.append("dcf_nonpositive_equity")
        else:
            v = mc / pack["dcf_value"]
            if rp.PDCF_SANE_RANGE[0] <= v <= rp.PDCF_SANE_RANGE[1]:
                p_dcf = v
            else:
                flags.append("pdcf_out_of_range")
    return {
        "ticker": ticker,
        "sector": sector,
        "industry": industry,
        "date": as_of.isoformat(),
        "fiscal_year_end": pack["anchor"],
        "pe_ratio": rp._clean_float(pe),
        "pb_ratio": rp._clean_float(pb),
        "p_dcf": rp._clean_float(p_dcf),
        "debt_to_equity": rp._clean_float(pack["debt_to_equity"]),
        "reinvestment_efficiency": rp._clean_float(pack["re_eff"]),
        "roe": rp._clean_float(pack["roe"]),
        "data_flags": ",".join(sorted(set(flags)))[:255],
        "_market_cap": mc,
    }


def screen_row(row):
    """Stage 1 screen, re-applied as of the rebalance date.

    Only market cap (>$2B) and sector -- no P/E or P/B gate. A stock with
    a high P/E or P/B still enters the universe; the ranking (which uses
    P/E and P/B as metrics) sorts it out.
    """
    if row is None:
        return False
    mc = row.get("_market_cap")
    if mc is None or mc <= MIN_MARKET_CAP:
        return False
    if row.get("sector") not in STAGE1_SECTORS:
        return False
    return True


def rank_snapshot(rows):
    """Rank one date's screened rows. Mirrors repeatable.recompute_rankings()
    (global percentiles, missing metric = 0.5, >= 4 metrics, negative equity
    excluded, rank method 'min'), including the sector-percentile branch
    when RANK_WITHIN_SECTOR is on. Returns ranked row dicts."""
    if not rows:
        return []
    df = pd.DataFrame(rows)
    flags = df["data_flags"].fillna("")
    rankable = (df[rp.METRIC_COLS].notna().sum(axis=1) >= rp.MIN_METRICS_TO_RANK) \
        & ~flags.str.contains("neg_equity")
    ranked = df[rankable].copy()
    if ranked.empty:
        return []
    pct_cols = []
    for col in rp.METRIC_COLS:
        ascending = col in rp.LOWER_IS_BETTER
        pct = ranked[col].rank(pct=True, ascending=ascending)
        if rp.RANK_WITHIN_SECTOR:
            by_sector = ranked.groupby("sector")[col]
            sector_pct = by_sector.rank(pct=True, ascending=ascending)
            enough = by_sector.transform("count") >= rp.MIN_SECTOR_SAMPLE
            pct = sector_pct.where(enough, pct)
        ranked[f"{col}_pct"] = pct
        pct_cols.append(f"{col}_pct")
    ranked["composite_score"] = ranked[pct_cols].fillna(0.5).mean(axis=1)
    ranked["overall_rank"] = ranked["composite_score"].rank(method="min").astype(int)
    out = []
    for r in ranked.sort_values("overall_rank").itertuples():
        # NOTE: a DataFrame round-trip silently turns None into NaN in any
        # float column that has at least one real value (a column that is
        # all-None stays object dtype and keeps None). PyMySQL refuses NaN
        # ("nan can not be used with MySQL"), so a ranked row with a missing
        # metric used to crash the snapshot write on the first date that had
        # one. _clean_float turns NaN back into None (NULL in the database).
        row = {"ticker": r.ticker, "sector": r.sector, "industry": r.industry,
               "composite_score": rp._clean_float(r.composite_score),
               "overall_rank": int(r.overall_rank), "data_flags": r.data_flags}
        for c in rp.METRIC_COLS:
            row[c] = rp._clean_float(getattr(r, c))
        out.append(row)
    return out


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------
def load_universe(engine, max_tickers=0):
    sql = "SELECT ticker, sector, industry FROM stock_stats ORDER BY ticker"
    if max_tickers > 0:
        sql += " LIMIT :n"
    with engine.connect() as conn:
        rows = conn.execute(text(sql), {"n": max_tickers} if max_tickers > 0 else {}).mappings()
        return [(r["ticker"], r["sector"], r["industry"]) for r in rows]


def _load_json(path, default):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def _save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data))
    tmp.replace(path)


def rebalance_dates(start, end, frequency):
    """First trading day of each holding window, from the ^GSPC calendar
    (the same calendar -- and the same window rules -- the backtest engine
    uses, via portfolio.backtest._rebalance_dates, so the two can never
    drift apart). Falls back to business days if the benchmark history
    cannot be loaded. "monthly" / "quarterly" / "annual" / "2-year" /
    "3-year" / "5-year" (or any "Nm" window). "buy-and-hold" makes no
    sense here -- one snapshot is not a history -- so it raises, as does
    any unknown frequency (previously anything non-monthly silently
    behaved as quarterly)."""
    if frequency == "buy-and-hold":
        raise ValueError("Backfill needs at least two snapshot dates; "
                         "'buy-and-hold' records only one.")
    bench = load_price_history("^GSPC", start, end)
    if bench is not None and not bench.empty:
        calendar = bench.index[(bench.index >= pd.Timestamp(start))
                               & (bench.index <= pd.Timestamp(end))]
    else:
        calendar = pd.bdate_range(start, end)
    return [d.date() for d in _engine_rebalance_dates(calendar, frequency)]


def backfill(engine, start_year=START_YEAR, end_date=END_DATE, rebalance=REBALANCE,
             max_tickers=MAX_TICKERS, force=FORCE):
    """Reconstruct snapshots for every rebalance date in the window."""
    start = date(start_year, 1, 1)
    end = rp._to_date(end_date) if end_date else date.today()
    dates = rebalance_dates(start, end, rebalance)
    log.info("Backfill: %d rebalance dates (%s .. %s, %s)", len(dates),
             dates[0] if dates else None, dates[-1] if dates else None, rebalance)
    progress = {} if force else _load_json(PROGRESS_PATH, {})
    completed = set(progress.get("completed_dates", []))
    rows_cache = {} if force else _load_json(ROWS_CACHE_PATH, {})
    universe = load_universe(engine, max_tickers)
    log.info("%d tickers in the universe", len(universe))

    session = rq.Session()
    session.headers.update({"User-Agent": os.environ["SEC_USER_AGENT"]})
    book = rp.load_ticker_book(session)

    per_date = {d: [] for d in dates}
    n_failed = 0
    for i, (ticker, sector, industry) in enumerate(universe, start=1):
        cached = rows_cache.get(ticker)
        if isinstance(cached, dict) and "__error__" in cached:
            continue  # failed before; skip (clear the rows cache to retry)
        if cached is None:
            cached = {}
        needed = [d for d in dates if d.isoformat() not in cached]
        if needed:
            downloaded = False
            try:
                gaap = rp.load_us_gaap(ticker, book, session)
            except rp.NoGaapData:
                cached["__error__"] = "no_us_gaap"
                rows_cache[ticker] = cached
                _save_json(ROWS_CACHE_PATH, rows_cache)
                n_failed += 1
                continue
            except KeyError:
                cached["__error__"] = "no_sec_cik"
                rows_cache[ticker] = cached
                _save_json(ROWS_CACHE_PATH, rows_cache)
                n_failed += 1
                continue
            except Exception as exc:  # noqa: BLE001
                if rp._is_rate_limit(exc):
                    raise
                log.warning("%s: SEC fetch failed: %r", ticker, exc)
                cached["__error__"] = f"sec_fetch_failed: {exc}"
                rows_cache[ticker] = cached
                _save_json(ROWS_CACHE_PATH, rows_cache)
                n_failed += 1
                continue
            price_hist = load_price_history(ticker, start, end)
            shares_hist = load_shares_history(ticker, start, end)
            if price_hist is None or shares_hist is None:
                cached["__error__"] = "no_price_history"
                rows_cache[ticker] = cached
                _save_json(ROWS_CACHE_PATH, rows_cache)
                n_failed += 1
                continue
            downloaded = True
            for d in needed:
                anchor = _anchor_as_of(gaap, d)
                if anchor is None or (d - rp._to_date(anchor)).days > rp.MAX_FILING_AGE_DAYS:
                    cached[d.isoformat()] = None  # no usable 10-K yet / too stale
                    continue
                pack = evaluate_anchor(ticker, gaap, anchor, d,
                                       price_hist, shares_hist, sector)
                cached[d.isoformat()] = metrics_on_date(
                    ticker, pack, price_hist, shares_hist, d, sector, industry)
            rows_cache[ticker] = cached
            _save_json(ROWS_CACHE_PATH, rows_cache)
            if downloaded:
                time.sleep(YF_PAUSE)
        for d in dates:
            row = cached.get(d.isoformat())
            if row:
                per_date[d].append(row)
        if i % 25 == 0:
            log.info("Backfill progress: %d/%d tickers", i, len(universe))

    n_written = 0
    for d in dates:
        key = d.isoformat()
        if key in completed and not force:
            continue
        rows = per_date[d]
        screened = [r for r in rows if screen_row(r)]
        ranked = rank_snapshot(screened)
        n = write_snapshot(engine, key, ranked, source="backfill")
        n_written += n
        completed.add(key)
        _save_json(PROGRESS_PATH, {"completed_dates": sorted(completed)})
        log.info("%s: %d tickers evaluated, %d passed the screen, %d ranked, snapshot saved",
                 key, len(rows), len(screened), n)
    log.info("Backfill done: %d snapshot rows written across %d dates, %d tickers skipped (no SEC/price data)",
             n_written, len(completed), n_failed)
    return {"dates": len(dates), "rows_written": n_written, "tickers_failed": n_failed}


# ---------------------------------------------------------------------------
# Self-test (synthetic data, hand-computed answers)
# ---------------------------------------------------------------------------
def _fake_gaap():
    """A synthetic company, 2019-2024, in the trimmed-gaap shape.

    Scale: dollars. FCFF grows steadily; NI and equity grow; RE grows a lot.
    2024 facts were filed 2025-02-15, so they are not knowable before then.
    """
    M = 1_000_000
    years = {
        # year: (ocf, capex, interest, tax, cash, debt, equity, ni, re)
        2019: (70 * M, 20 * M, 10 * M, 0.20, 40 * M, 200 * M, 500 * M, 100 * M, 100 * M),
        2020: (80 * M, 20 * M, 10 * M, 0.20, 45 * M, 220 * M, 600 * M, 120 * M, 200 * M),
        2021: (90 * M, 20 * M, 10 * M, 0.20, 48 * M, 250 * M, 700 * M, 140 * M, 300 * M),
        2022: (100 * M, 20 * M, 10 * M, 0.20, 50 * M, 270 * M, 800 * M, 160 * M, 400 * M),
        2023: (110 * M, 20 * M, 10 * M, 0.20, 52 * M, 280 * M, 900 * M, 180 * M, 500 * M),
        2024: (120 * M, 20 * M, 10 * M, 0.20, 50 * M, 300 * M, 1000 * M, 200 * M, 1000 * M),
    }
    gaap = {}

    def add(tag, unit, year, val, filed):
        gaap.setdefault(tag, {}).setdefault(unit, []).append({
            "start": f"{year}-01-01", "end": f"{year}-12-31", "val": val,
            "filed": filed, "form": "10-K"})

    for y, (ocf, capex, interest, tax, cash, debt, equity, ni, re) in years.items():
        filed = "2025-02-15" if y == 2024 else f"{y + 1}-02-15"
        add("NetCashProvidedByUsedInOperatingActivities", "USD", y, ocf, filed)
        add("PaymentsToAcquirePropertyPlantAndEquipment", "USD", y, capex, filed)
        add("InterestExpense", "USD", y, interest, filed)
        add("EffectiveIncomeTaxRateContinuingOperations", "pure", y, tax, filed)
        add("CashAndCashEquivalentsAtCarryingValue", "USD", y, cash, filed)
        add("LongTermDebt", "USD", y, debt, filed)
        add("StockholdersEquity", "USD", y, equity, filed)
        add("NetIncomeLoss", "USD", y, ni, filed)
        add("RetainedEarningsAccumulatedDeficit", "USD", y, re, filed)
    return gaap


def _fake_market_data():
    """Price 10.0 until 2021-12-31, then 20.0; shares flat 110,000,000.

    Market cap is 1,100m at the 2019 fiscal end and 2,200m at the 2024
    fiscal end (above the $2B screen), so the screen passes.
    """
    days = pd.bdate_range("2019-01-01", "2025-03-01")
    price = pd.Series([10.0 if d <= pd.Timestamp("2021-12-31") else 20.0 for d in days], index=days)
    hist = pd.DataFrame({"Close": price, "Stock Splits": 0.0}, index=days)
    hist.index.name = "Date"
    shares = pd.Series([110_000_000.0] * len(days), index=days, name="Shares")
    return hist, shares


def run_self_test():
    """Verify the reconstruction on synthetic data. No network, no database."""
    gaap = _fake_gaap()
    price_hist, shares_hist = _fake_market_data()

    # Filed-date cutoff: the 2024 10-K was filed 2025-02-15.
    assert _anchor_as_of(gaap, date(2025, 2, 1)) == "2023-12-31"
    assert _anchor_as_of(gaap, date(2025, 3, 1)) == "2024-12-31"
    # A fact filed after the date must not leak into the truncated gaap.
    truncated = _gaap_as_of(gaap, "2024-12-31", date(2025, 2, 1))
    assert "2024-12-31" not in rp.annual_series(truncated, rp.NI_TAGS, True)

    as_of = date(2025, 3, 1)
    pack = evaluate_anchor("FAKE", gaap, "2024-12-31", as_of,
                           price_hist, shares_hist, "Industrials")
    # DCF, computed independently here from the same inputs:
    # FCFF = OCF - CapEx + interest*(1-tax) = 58, 68, 78, 88, 98, 108 (millions)
    fcff = [58e6, 68e6, 78e6, 88e6, 98e6, 108e6]
    base = sum(fcff[-3:]) / 3  # 98m
    growth = 0.10  # median YoY growth 0.1282, clamped to the 10% cap
    projected = [base * (1 + growth) ** i for i in range(1, 6)]
    pv = sum(f / (1.09) ** i for i, f in enumerate(projected, start=1))
    pv += (projected[-1] * 1.025 / (0.09 - 0.025)) / (1.09 ** 5)
    expected_dcf = pv + 50e6 - 300e6  # + cash 50m - debt 300m
    assert pack["dcf_value"] is not None
    assert abs(pack["dcf_value"] - expected_dcf) / expected_dcf < 1e-9, \
        (pack["dcf_value"], expected_dcf)
    assert abs(pack["debt_to_equity"] - 0.3) < 1e-12  # 300m / 1000m
    assert abs(pack["roe"] - 0.2) < 1e-12  # median of 0.2 each year
    # RE = (mc 2024 - mc 2019) / (RE 2024 - RE 2019) = (2,200m - 1,100m) / 900m
    assert abs(pack["re_eff"] - (1100e6 / 900e6)) < 1e-9, pack["re_eff"]

    row = metrics_on_date("FAKE", pack, price_hist, shares_hist, as_of,
                          "Industrials", "Machinery")
    assert row is not None
    assert abs(row["pe_ratio"] - 11.0) < 1e-9  # price 20 / EPS (200m / 110m shares)
    assert abs(row["pb_ratio"] - 2.2) < 1e-9  # mc 2,200m / equity 1,000m
    assert abs(row["p_dcf"] - (2200e6 / expected_dcf)) < 1e-9
    assert screen_row(row) is True  # mc 2,200m > 2,000m, sector Industrials

    # Ranking: 4 tickers, distinct values on every metric, hand-computed.
    rows = []
    for i, t in enumerate(["W", "X", "Y", "Z"]):
        rows.append({"ticker": t, "sector": "Industrials", "industry": "Machinery",
                     "pe_ratio": 10.0 + 10 * i, "pb_ratio": 1.0 + i,
                     "p_dcf": 0.5 + 0.5 * i, "debt_to_equity": 0.2 + 0.2 * i,
                     "reinvestment_efficiency": 4.0 - i, "roe": 0.4 - 0.1 * i,
                     "data_flags": ""})
    ranked = rank_snapshot(rows)
    # Lower-is-better percentiles for W..Z: 0.25, 0.5, 0.75, 1.0 on pe/pb/p_dcf/de.
    # Higher-is-better (re, roe): W best -> 0.25, Z worst -> 1.0.
    # Every ticker's six percentiles are identical, so composite = that value.
    assert [(r["ticker"], r["overall_rank"]) for r in ranked] == \
        [("W", 1), ("X", 2), ("Y", 3), ("Z", 4)], ranked
    assert abs(ranked[0]["composite_score"] - 0.25) < 1e-12
    assert abs(ranked[-1]["composite_score"] - 1.0) < 1e-12
    # NaN regression (real backfill failure, 2016-01-04): a ranked row with
    # a missing metric (p_dcf None: DCF nonpositive/insufficient) alongside
    # rows with a real p_dcf. A DataFrame round-trip turns that None into
    # NaN, which PyMySQL refuses ("nan can not be used with MySQL") and
    # crashes the snapshot write. The output must carry None, never NaN.
    nan_rows = [
        {"ticker": "MUR", "sector": "Energy", "industry": None,
         "pe_ratio": 4.36, "pb_ratio": 0.46, "p_dcf": 0.23, "debt_to_equity": 0.31,
         "reinvestment_efficiency": None, "roe": 0.11, "data_flags": "re_no_market_cap"},
        {"ticker": "CVX", "sector": "Energy", "industry": None,
         "pe_ratio": 8.69, "pb_ratio": 1.08, "p_dcf": None, "debt_to_equity": 0.10,
         "reinvestment_efficiency": None, "roe": 0.18,
         "data_flags": "dcf_nonpositive_equity,re_no_market_cap"},
        {"ticker": "GHC", "sector": "Industrials", "industry": None,
         "pe_ratio": 2.04, "pb_ratio": 0.84, "p_dcf": 0.58, "debt_to_equity": 0.14,
         "reinvestment_efficiency": None, "roe": 0.07, "data_flags": "re_no_market_cap"},
        {"ticker": "PSX", "sector": "Energy", "industry": None,
         "pe_ratio": 8.97, "pb_ratio": 1.96, "p_dcf": None, "debt_to_equity": 0.00,
         "reinvestment_efficiency": None, "roe": 0.20,
         "data_flags": "dcf_insufficient_fcf,re_missing_data"},
    ]
    nan_ranked = rank_snapshot(nan_rows)
    assert len(nan_ranked) == 4, nan_ranked
    for r in nan_ranked:
        for c in rp.METRIC_COLS:
            v = r[c]
            assert v is None or (isinstance(v, float) and math.isfinite(v)), (r["ticker"], c, v)
    by_ticker = {r["ticker"]: r for r in nan_ranked}
    assert by_ticker["CVX"]["p_dcf"] is None, by_ticker["CVX"]
    assert by_ticker["PSX"]["p_dcf"] is None, by_ticker["PSX"]
    assert abs(by_ticker["MUR"]["p_dcf"] - 0.23) < 1e-12

    # Snapshot dates: same window rules as the backtest engine, over
    # 2019-01-01 .. 2025-12-31 (7 years): monthly 84, quarterly 28,
    # annual 7, 2-year 4, 3-year 3, 5-year 3, "18m" 5. No network here,
    # so the benchmark download is stubbed out (business-day fallback).
    global load_price_history
    _real_load = load_price_history
    load_price_history = lambda *a: None  # noqa: E731
    try:
        assert len(rebalance_dates(date(2019, 1, 1), date(2025, 12, 31), "monthly")) == 84
        assert len(rebalance_dates(date(2019, 1, 1), date(2025, 12, 31), "quarterly")) == 28
        assert len(rebalance_dates(date(2019, 1, 1), date(2025, 12, 31), "annual")) == 7
        assert len(rebalance_dates(date(2019, 1, 1), date(2025, 12, 31), "2-year")) == 4
        assert len(rebalance_dates(date(2019, 1, 1), date(2025, 12, 31), "3-year")) == 3
        assert len(rebalance_dates(date(2019, 1, 1), date(2025, 12, 31), "5-year")) == 3
        assert len(rebalance_dates(date(2019, 1, 1), date(2025, 12, 31), "18m")) == 5
        try:
            rebalance_dates(date(2019, 1, 1), date(2025, 12, 31), "weekly")
            raise AssertionError("weekly should raise ValueError")
        except ValueError:
            pass
        try:
            rebalance_dates(date(2019, 1, 1), date(2025, 12, 31), "buy-and-hold")
            raise AssertionError("buy-and-hold should raise ValueError")
        except ValueError:
            pass
    finally:
        load_price_history = _real_load
    print("SELF-TEST PASSED: anchor/filed cutoff, DCF, ratios and ranking match hand-computed values.")
    print("  Snapshot windows over 2019-2025: monthly 84, quarterly 28, annual 7, "
          "2-year 4, 3-year 3, 5-year 3, 18m 5")
    print(f"  DCF equity value: {pack['dcf_value']:,.0f} (expected {expected_dcf:,.0f})")
    print(f"  P/E {row['pe_ratio']:.2f}, P/B {row['pb_ratio']:.2f}, "
          f"P/DCF {row['p_dcf']:.3f}, D/E {row['debt_to_equity']:.2f}, "
          f"RE {row['reinvestment_efficiency']:.3f}, ROE {row['roe']:.2f}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main():
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start-year", type=int, default=START_YEAR)
    parser.add_argument("--end", default=END_DATE)
    parser.add_argument("--rebalance", default=REBALANCE,
                        help="snapshot frequency: monthly | quarterly | annual | "
                             "2-year | 3-year | 5-year | Nm (e.g. 18m)")
    parser.add_argument("--max-tickers", type=int, default=MAX_TICKERS)
    parser.add_argument("--force", action="store_true", default=FORCE)
    parser.add_argument("--self-test", action="store_true", default=SELF_TEST)
    args = parser.parse_args()
    try:
        months = _rebalance_months(args.rebalance)
        if months is None:
            raise ValueError("Backfill needs at least two snapshot dates; "
                             "'buy-and-hold' records only one.")
    except ValueError as exc:
        parser.error(str(exc))
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    if args.self_test:
        run_self_test()
        return

    if not os.environ.get("SEC_USER_AGENT"):
        raise SystemExit('Set SEC_USER_AGENT in info.env (required by the SEC)')
    engine = db.get_engine()
    db.ensure_schema(engine)
    summary = backfill(engine, start_year=args.start_year, end_date=args.end,
                       rebalance=args.rebalance, max_tickers=args.max_tickers,
                       force=args.force)
    print(f"\nBackfill summary: {summary['rows_written']} snapshot rows across "
          f"{summary['dates']} dates; {summary['tickers_failed']} tickers skipped.")


if __name__ == "__main__":
    main()
