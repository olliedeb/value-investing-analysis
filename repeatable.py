"""Stage 2: evaluate queued tickers on fundamentals and rank them.

For each ticker waiting in the `queue` table this computes, from SEC XBRL
filings (10-K only) plus Yahoo Finance market data:

    pe_ratio, pb_ratio          Yahoo trailing P/E and P/B (must be > 0)
    p_dcf                       market cap / DCF equity value   (< 1 = cheap)
    debt_to_equity              total debt / shareholders' equity
    reinvestment_efficiency     change in market cap / change in retained earnings
    roe                         median annual ROE over the lookback window

and then stores a composite percentile rank across everything evaluated.

    python repeatable.py                 # process 25 queued tickers
    python repeatable.py --limit 0       # process the entire queue
    python repeatable.py --rank-only     # just recompute rankings

Required env vars (info.env): DB_USERNAME, DB_PASSWORD, DB_NAME, [DB_HOST],
SEC_USER_AGENT (the SEC requires "Your Name your@email.com").
"""
import argparse
import json
import logging
import math
import os
import statistics
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import requests as rq
import yfinance as yf
from sqlalchemy import text

from db import ensure_schema, get_engine

log = logging.getLogger("repeatable")

# --- tunable assumptions -------------------------------------------------------
DISCOUNT_RATE = 0.09
TERMINAL_GROWTH_RATE = 0.025
HORIZON_YEARS = 5
LOOKBACK_YEARS = 5

# Growth is the MEDIAN of historical YoY FCF growth (robust to one wild year),
# clamped to this band. A value tool should not extrapolate +30%/yr for five years.
GROWTH_CAP = 0.10
GROWTH_FLOOR = -0.10
BASE_FCF_YEARS = 3        # starting FCF = mean of last N years (normalises one-offs); 1 = last year only
MIN_FCF_YEARS = 4         # need at least this many years of FCF to run a DCF
DEFAULT_TAX_RATE = 0.21   # used when a filer doesn't tag an effective tax rate
PDCF_SANE_RANGE = (0.01, 100)

# Retained-earnings change must be at least this fraction of starting market cap,
# otherwise the ratio's denominator is ~0 and the result is meaningless.
MIN_RE_DELTA_PCT_OF_MCAP = 0.05

MIN_METRICS_TO_RANK = 4   # of 6 metrics; fewer than this -> not ranked
RANK_WITHIN_SECTOR = False
MIN_SECTOR_SAMPLE = 8     # sectors smaller than this fall back to global percentiles
MAX_STATS_AGE_DAYS = 45   # ignore stock_stats rows older than this when ranking
MAX_FILING_AGE_DAYS = 600  # latest 10-K older than this = stale/delisted

# Free-cash-flow DCF and debt/equity are not meaningful for banks, insurers,
# REITs (deposits / repo funding / acquisition capex dominate the cash flows).
FINANCIAL_SECTORS = {"Financial Services", "Real Estate"}

SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SEC_FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"
SEC_MIN_INTERVAL = 0.15   # SEC fair-access limit is 10 requests/second
YF_PAUSE = 0.5            # pause between tickers to stay under Yahoo's rate limit
TICKER_FILE = Path("company_tickers.json")
CACHE_DIR = Path("sec_cache")
CACHE_TTL_SECONDS = 7 * 24 * 3600
CACHE_VERSION = 1
ANNUAL_FORMS = ("10-K", "10-K/A", "10-KT")

# --- XBRL tag candidates (earlier = preferred; later ones only fill gaps) ---------
OCF_TAGS = [
    "NetCashProvidedByUsedInOperatingActivities",
    "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations",
]
CAPEX_TAGS = [
    "PaymentsToAcquirePropertyPlantAndEquipment",
    "PaymentsForCapitalImprovements",
    "PaymentsForProceedsFromProductiveAssets",
    "PaymentsToAcquireProductiveAssets",
    "PaymentsToAcquireRealEstate",
]
CASH_TAGS = [
    "CashAndCashEquivalentsAtCarryingValue",
    "CashAndCashEquivalentsAtCarryingValueIncludingDiscontinuedOperations",
    "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",
]
INTEREST_TAGS = ["InterestExpense", "InterestExpenseDebt"]
TAX_TAGS = ["EffectiveIncomeTaxRateContinuingOperations"]
RE_TAGS = [
    "RetainedEarningsAccumulatedDeficit",
    "PartnersCapital",   # LPs don't report retained earnings; total capital is an approximation
    "MembersEquity",
]
EQUITY_TAGS = [
    "StockholdersEquity",
    "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
]
NI_TAGS = ["NetIncomeLoss", "ProfitLoss"]
TOTAL_DEBT_TAGS = ["DebtLongtermAndShorttermCombinedAmount", "LongTermDebt"]  # already include current portion
NONCURRENT_DEBT_TAGS = ["LongTermDebtNoncurrent"]
DEBT_CURRENT_TAGS = ["DebtCurrent"]
LTD_CURRENT_TAGS = ["LongTermDebtCurrent"]
ST_BORROWINGS_TAGS = ["ShortTermBorrowings"]

NEEDED_TAGS = sorted(set(
    OCF_TAGS + CAPEX_TAGS + CASH_TAGS + INTEREST_TAGS + TAX_TAGS + RE_TAGS + EQUITY_TAGS
    + NI_TAGS + TOTAL_DEBT_TAGS + NONCURRENT_DEBT_TAGS + DEBT_CURRENT_TAGS
    + LTD_CURRENT_TAGS + ST_BORROWINGS_TAGS
))

METRIC_COLS = ["pe_ratio", "pb_ratio", "p_dcf", "debt_to_equity", "reinvestment_efficiency", "roe"]
LOWER_IS_BETTER = ["pe_ratio", "pb_ratio", "p_dcf", "debt_to_equity"]
HIGHER_IS_BETTER = ["reinvestment_efficiency", "roe"]


# ==================================================================================
# Small helpers
# ==================================================================================
class RateLimited(Exception):
    """Raised when Yahoo or the SEC tells us to back off; the batch stops cleanly."""


class NoGaapData(Exception):
    """Company has no us-gaap facts (e.g. foreign private issuers filing IFRS 20-Fs)."""


def _is_rate_limit(exc):
    return (
        isinstance(exc, RateLimited)
        or "ratelimit" in type(exc).__name__.lower()
        or "too many requests" in str(exc).lower()
    )


def _guard(name, fn, flags):
    """Run fn(); on failure record a flag and return None so other metrics still compute."""
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001 - deliberately broad, see below
        if _is_rate_limit(exc):
            raise  # never swallow a rate limit - the caller needs to stop the batch
        log.warning("%s failed: %r", name, exc)
        flags.append(f"{name}_failed")
        return None


def _clean_float(x):
    """float(x) or None. Rejects NaN/inf (Yahoo sometimes returns the string 'Infinity')."""
    try:
        f = float(x)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _to_date(s):
    return datetime.strptime(s, "%Y-%m-%d").date()


# ==================================================================================
# SEC data layer: ONE download per ticker, trimmed + cached on disk
# ==================================================================================
_last_sec_call = 0.0


def _sec_get(session, url):
    """Rate-limited GET against sec.gov that fails loudly instead of parsing error pages as JSON."""
    global _last_sec_call
    wait = SEC_MIN_INTERVAL - (time.monotonic() - _last_sec_call)
    if wait > 0:
        time.sleep(wait)
    resp = session.get(url, timeout=(5, 60))
    _last_sec_call = time.monotonic()
    if resp.status_code in (403, 429):
        raise RateLimited(f"SEC returned {resp.status_code} - check SEC_USER_AGENT / slow down")
    resp.raise_for_status()
    return resp


def flatten(raw_book):
    """SEC's {"0": {"cik_str": 320193, "ticker": "AAPL", ...}} -> {"AAPL": 320193}"""
    return {v["ticker"].upper(): int(v["cik_str"]) for v in raw_book.values()}


def load_ticker_book(session):
    """Load SEC's ticker->CIK map, downloading/refreshing it weekly."""
    stale = (not TICKER_FILE.exists()
             or time.time() - TICKER_FILE.stat().st_mtime > CACHE_TTL_SECONDS)
    if stale:
        log.info("Downloading %s", SEC_TICKERS_URL)
        TICKER_FILE.write_text(_sec_get(session, SEC_TICKERS_URL).text)
    return flatten(json.loads(TICKER_FILE.read_text()))


def _trim_gaap(gaap):
    """Keep only the tags/units/forms/fields we use. Full companyfacts files are 1-15 MB each."""
    out = {}
    for tag in NEEDED_TAGS:
        node = gaap.get(tag)
        if not node:
            continue
        units = {}
        for unit, entries in node.get("units", {}).items():
            if unit not in ("USD", "pure"):
                continue
            kept = [
                {k: e[k] for k in ("start", "end", "val", "filed", "form") if k in e}
                for e in entries if e.get("form") in ANNUAL_FORMS
            ]
            if kept:
                units[unit] = kept
        if units:
            out[tag] = units
    return out


def load_us_gaap(ticker, book, session):
    """Return the trimmed us-gaap facts for a ticker (one HTTP call, cached for a week)."""
    cik = book.get(ticker.upper())
    if cik is None:
        raise KeyError(f"{ticker} not found in SEC ticker map")

    path = CACHE_DIR / f"CIK{cik:010d}.json"
    if path.exists() and time.time() - path.stat().st_mtime < CACHE_TTL_SECONDS:
        cached = json.loads(path.read_text())
        if cached.get("v") == CACHE_VERSION:
            if cached["gaap"] is None:
                raise NoGaapData(ticker)
            return cached["gaap"]

    data = _sec_get(session, SEC_FACTS_URL.format(cik=cik)).json()
    raw = data.get("facts", {}).get("us-gaap")
    trimmed = _trim_gaap(raw) if raw else None
    CACHE_DIR.mkdir(exist_ok=True)
    path.write_text(json.dumps({"v": CACHE_VERSION, "gaap": trimmed}))
    if trimmed is None:
        raise NoGaapData(ticker)
    return trimmed


# ==================================================================================
# Series extraction. Everything is keyed by the period END DATE ("YYYY-MM-DD").
#
# The original keyed on XBRL `frame` labels. Frames are assigned per-concept to
# only ONE filing's fact per period, so the same fiscal year can carry a frame
# in one tag and none in another (and non-calendar fiscal years get labelled
# inconsistently between statements). Period end dates are identical across
# every statement in a filing, so they align reliably.
# ==================================================================================
def annual_series(gaap, tags, duration, unit="USD"):
    """{period_end: value} for annual (10-K) facts.

    duration=True  -> flow items (cash flow / income statement): only ~1-year periods kept
    duration=False -> point-in-time items (balance sheet)
    For each period the latest-filed value wins (captures restatements). Tags are
    tried in priority order; a later tag only fills periods earlier tags lack,
    so a company that switched XBRL tags mid-history still gets a full series.
    """
    merged = {}
    for tag in tags:
        latest = {}
        for e in gaap.get(tag, {}).get(unit, []):
            if e.get("form") not in ANNUAL_FORMS or not e.get("end"):
                continue
            if duration:
                if not e.get("start"):
                    continue
                days = (_to_date(e["end"]) - _to_date(e["start"])).days
                if not 350 <= days <= 380:
                    continue
            if e["end"] not in latest or e["filed"] > latest[e["end"]]["filed"]:
                latest[e["end"]] = e
        for end, e in latest.items():
            merged.setdefault(end, e["val"])
    return merged


def value_near(series, target, tolerance_days=45):
    """Value whose period end is closest to `target` (within tolerance) -> (value, end) or (None, None)."""
    best = None
    for end, val in series.items():
        gap = abs((_to_date(end) - _to_date(target)).days)
        if gap <= tolerance_days and (best is None or gap < best[0]):
            best = (gap, end, val)
    return (best[2], best[1]) if best else (None, None)


def total_debt_series(gaap):
    """Total debt per period, including the current portion and short-term borrowings.

    The original used LongTermDebtNoncurrent alone, which excludes debt maturing
    within a year and commercial paper, understating leverage and net debt.
    """
    out = annual_series(gaap, TOTAL_DEBT_TAGS, False)  # already include the current portion
    noncurrent = annual_series(gaap, NONCURRENT_DEBT_TAGS, False)
    debt_current = annual_series(gaap, DEBT_CURRENT_TAGS, False)
    ltd_current = annual_series(gaap, LTD_CURRENT_TAGS, False)
    short_term = annual_series(gaap, ST_BORROWINGS_TAGS, False)
    for end, nc in noncurrent.items():
        if end in out:
            continue
        if end in debt_current:  # DebtCurrent already covers current LTD + short-term borrowings
            current = debt_current[end]
        else:
            current = ltd_current.get(end, 0.0) + short_term.get(end, 0.0)
        out[end] = nc + current
    return out


def debt_at(gaap, anchor):
    """Total debt near `anchor`, or None if unknowable.

    A company with no debt tag AND no interest expense is treated as debt-free.
    A company that pays interest but has no debt tag is a data gap -> None
    (never silently reported as zero leverage, and never swapped for Total
    Liabilities, which would count payables/deferred revenue as debt).
    """
    debt, _ = value_near(total_debt_series(gaap), anchor)
    if debt is not None:
        return debt
    interest = annual_series(gaap, INTEREST_TAGS, True)
    if not any(v > 0 for v in interest.values()):
        return 0.0
    return None


def latest_fiscal_end(gaap):
    ends = set(annual_series(gaap, OCF_TAGS, True)) | set(annual_series(gaap, NI_TAGS, True))
    return max(ends) if ends else None


# ==================================================================================
# DCF
# ==================================================================================
def compute_fcff(gaap):
    """Unlevered free cash flow per fiscal year: CFO - CapEx + Interest * (1 - tax).

    Same formula as the original (CFO - CapEx - Interest*t + Interest), computed
    directly per year. Differences:
      * a year with missing CapEx is skipped - the original treated missing
        CapEx as zero, silently turning FCF into operating cash flow
      * missing interest = 0; missing tax rate = DEFAULT_TAX_RATE (the original's
        helper treated a missing tax series as a 100% rate)
    """
    ocf = annual_series(gaap, OCF_TAGS, True)
    capex = annual_series(gaap, CAPEX_TAGS, True)
    interest = annual_series(gaap, INTEREST_TAGS, True)
    tax = annual_series(gaap, TAX_TAGS, True, unit="pure")

    fcff = {}
    for end, cfo in ocf.items():
        if end not in capex:
            continue
        t = tax.get(end, DEFAULT_TAX_RATE)
        if t > 1:           # occasionally mis-scaled as 21 instead of 0.21
            t /= 100
        t = min(max(t, 0.0), 0.5)
        fcff[end] = cfo - capex[end] + interest.get(end, 0.0) * (1 - t)
    return fcff


def estimate_growth(values):
    """Median year-over-year growth (chronological values), clamped to [GROWTH_FLOOR, GROWTH_CAP].

    Only growth measured from a positive base counts; growth from a negative or
    zero base is meaningless. Median instead of mean so one COVID-style swing
    can't dominate.
    """
    rates = [(cur - prev) / prev for prev, cur in zip(values[:-1], values[1:]) if prev > 0]
    if not rates:
        return 0.0
    return min(max(statistics.median(rates), GROWTH_FLOOR), GROWTH_CAP)


def terminal_value(final_year_fcf, discount_rate, terminal_growth_rate, horizon_years):
    """Gordon growth terminal value, discounted to today."""
    if discount_rate <= terminal_growth_rate:
        raise ValueError("discount_rate must be greater than terminal_growth_rate")
    tv = final_year_fcf * (1 + terminal_growth_rate) / (discount_rate - terminal_growth_rate)
    return tv / ((1 + discount_rate) ** horizon_years)


def dcf(gaap, flags, discount_rate=DISCOUNT_RATE, terminal_growth_rate=TERMINAL_GROWTH_RATE,
        horizon_years=HORIZON_YEARS, lookback_years=LOOKBACK_YEARS):
    """DCF equity value (EV from FCFF, then + cash - debt), or None when it can't be trusted."""
    fcff = compute_fcff(gaap)
    ends = sorted(fcff)[-(lookback_years + 1):]
    if len(ends) < MIN_FCF_YEARS:
        flags.append("dcf_insufficient_fcf")
        return None

    values = [fcff[e] for e in ends]
    base = statistics.mean(values[-BASE_FCF_YEARS:])
    if base <= 0:
        flags.append("dcf_nonpositive_fcf")
        return None

    growth = estimate_growth(values)
    projected = [base * (1 + growth) ** i for i in range(1, horizon_years + 1)]
    pv = sum(f / (1 + discount_rate) ** i for i, f in enumerate(projected, start=1))
    pv += terminal_value(projected[-1], discount_rate, terminal_growth_rate, horizon_years)

    anchor = ends[-1]
    debt = debt_at(gaap, anchor)
    if debt is None:
        flags.append("dcf_no_debt_data")
        return None
    cash, _ = value_near(annual_series(gaap, CASH_TAGS, False), anchor)
    return pv + (cash or 0.0) - debt


# ==================================================================================
# Market cap + reinvestment efficiency
# ==================================================================================
def get_market_caps(ticker, as_of_dates):
    """Approximate market cap at each date -> {date: float | None}. Two Yahoo calls total.

    Fixes vs the original:
      * uses the last close ON/BEFORE the date (the original took .iloc[0], the
        EARLIEST day of the window - up to 6 weeks early)
      * auto_adjust=False: the default dividend-adjusts old prices downward,
        understating historical market caps for dividend payers
      * returns None if a split happened after the date: Yahoo's Close is
        split-adjusted, but its historical share counts are not reliably
        adjusted the same way, so price x shares could be off by the split
        ratio. Skipping is safer than guessing (value stocks rarely split)
    """
    result = {d: None for d in as_of_dates}
    tkr = yf.Ticker(ticker)
    first, last = min(as_of_dates), max(as_of_dates)

    hist = tkr.history(
        start=(first - timedelta(days=14)).isoformat(),
        end=(date.today() + timedelta(days=1)).isoformat(),
        auto_adjust=False,
    )
    if hist is None or hist.empty:
        return result
    idx = hist.index.tz_localize(None) if hist.index.tz is not None else hist.index
    closes = hist["Close"].to_numpy()
    splits = np.nan_to_num(hist["Stock Splits"].to_numpy()) if "Stock Splits" in hist.columns else None

    # Yahoo samples shares outstanding sparsely, so search a wide window for the nearest sample.
    shares = tkr.get_shares_full(
        start=(first - timedelta(days=200)).isoformat(),
        end=(last + timedelta(days=200)).isoformat(),
    )
    if shares is None or len(shares) == 0:
        return result
    shares = shares.dropna()
    if shares.empty:
        return result
    s_idx = shares.index.tz_localize(None) if shares.index.tz is not None else shares.index

    for d in as_of_dates:
        ts = pd.Timestamp(d)
        before = np.flatnonzero(np.asarray(idx <= ts))
        if before.size == 0:
            continue
        pos = before[-1]
        if (ts - idx[pos]).days > 14:      # no recent trading data around that date
            continue
        if splits is not None and (splits[np.asarray(idx > ts)] != 0).any():
            log.info("%s: split after %s - skipping market-cap comparison", ticker, d)
            continue
        nearest = int(np.abs(np.asarray(s_idx - ts)).argmin())
        result[d] = float(closes[pos]) * float(shares.iloc[nearest])
    return result


def retained_earnings_check(ticker, gaap, anchor, flags, lookback_years=LOOKBACK_YEARS,
                            market_caps=None):
    """Reinvestment efficiency = change in market cap / change in retained earnings.

    Buffett's "one-dollar premise": each $1 of earnings retained should create >= $1
    of market value. Fixes vs the original:
      * uses the actual period-end dates of the matched filings, not Dec 31 and
        not the *requested* years when a fallback year was used
      * requires retained earnings to have INCREASED. With a negative change, a
        market cap that also fell produced a positive ratio that ranked as "good"
      * requires a material change (see MIN_RE_DELTA_PCT_OF_MCAP) so a near-zero
        denominator can't produce an absurd score
    Caveat: buybacks retired against retained earnings and share issuance both
    distort this ratio; treat it as a screen, not a verdict.

    `market_caps` (optional) is a {date: market cap} dict for the two fiscal
    period ends. The backfill (portfolio/history.py) passes precomputed
    values so it doesn't re-download Yahoo data once per anchor year;
    the live path leaves it as None and downloads as before.
    """
    re_series = annual_series(gaap, RE_TAGS, False)
    end_val, end_date = value_near(re_series, anchor)
    start_target = (_to_date(anchor) - timedelta(days=round(365.25 * lookback_years))).isoformat()
    start_val, start_date = value_near(re_series, start_target, tolerance_days=120)
    if end_val is None or start_val is None or start_date == end_date:
        flags.append("re_missing_data")
        return None

    delta_re = end_val - start_val
    if delta_re <= 0:
        flags.append("re_not_increasing")
        return None

    d_start, d_end = _to_date(start_date), _to_date(end_date)
    caps = market_caps if market_caps is not None else get_market_caps(ticker, [d_start, d_end])
    mc_start, mc_end = caps[d_start], caps[d_end]
    if mc_start is None or mc_end is None:
        flags.append("re_no_market_cap")
        return None
    if delta_re < MIN_RE_DELTA_PCT_OF_MCAP * mc_start:
        flags.append("re_change_immaterial")
        return None
    return (mc_end - mc_start) / delta_re


# ==================================================================================
# Other fundamentals
# ==================================================================================
def get_debt_to_equity(gaap, anchor, flags):
    equity, _ = value_near(annual_series(gaap, EQUITY_TAGS, False), anchor)
    if equity is None or equity <= 0:    # negative equity is flagged by the caller
        return None
    debt = debt_at(gaap, anchor)
    if debt is None:
        flags.append("de_no_debt_data")
        return None
    return debt / equity


def get_roe(gaap, lookback_years=LOOKBACK_YEARS):
    """Median annual ROE (net income / year-end equity) over the last N fiscal years; needs >= 3 years."""
    ni = annual_series(gaap, NI_TAGS, True)
    equity = annual_series(gaap, EQUITY_TAGS, False)
    roes = []
    for end in sorted(ni)[-lookback_years:]:
        eq, _ = value_near(equity, end, tolerance_days=10)
        if eq is not None and eq > 0:
            roes.append(ni[end] / eq)
    return statistics.median(roes) if len(roes) >= 3 else None


# ==================================================================================
# Per-ticker evaluation + persistence
# ==================================================================================
def evaluate_ticker(ticker, book, session):
    """Compute all metrics for one ticker -> (stats dict, flags list, status)."""
    flags = []
    stats = {"ticker": ticker, "date": date.today().isoformat(), "sector": None,
             "industry": None,
             "fiscal_year_end": None, **{c: None for c in METRIC_COLS}}

    # ---- Yahoo ----------------------------------------------------------------
    info = _guard("yf_info", lambda: yf.Ticker(ticker).info, flags) or {}
    market_cap = _clean_float(info.get("marketCap"))
    sector = info.get("sector")
    stats["sector"] = sector
    # Industry is the narrower peer grouping (e.g. Semiconductors within
    # Technology). It comes free with the info call above, and the dashboard
    # uses it for peer-relative z-scores when enough industry peers exist.
    stats["industry"] = info.get("industry")
    pe, pb = _clean_float(info.get("trailingPE")), _clean_float(info.get("priceToBook"))
    stats["pe_ratio"] = pe if pe is not None and pe > 0 else None
    if pb is not None and pb <= 0:
        flags.append("neg_equity")
    stats["pb_ratio"] = pb if pb is not None and pb > 0 else None

    # ---- SEC ------------------------------------------------------------------
    try:
        gaap = load_us_gaap(ticker, book, session)
    except NoGaapData:
        flags.append("no_us_gaap")
        gaap = None
    except KeyError:
        flags.append("no_sec_cik")
        gaap = None
    except RateLimited:
        raise
    except Exception as exc:  # noqa: BLE001
        log.warning("%s: SEC fetch failed: %r", ticker, exc)
        flags.append("sec_fetch_failed")
        gaap = None

    anchor = latest_fiscal_end(gaap) if gaap else None
    if gaap and (anchor is None or (date.today() - _to_date(anchor)).days > MAX_FILING_AGE_DAYS):
        flags.append("no_recent_10k")
        anchor = None

    if anchor:
        stats["fiscal_year_end"] = anchor
        equity, _ = value_near(annual_series(gaap, EQUITY_TAGS, False), anchor)
        if equity is not None and equity <= 0:
            flags.append("neg_equity")

        stats["roe"] = _guard("roe", lambda: get_roe(gaap), flags)
        stats["reinvestment_efficiency"] = _guard(
            "re_check", lambda: retained_earnings_check(ticker, gaap, anchor, flags), flags)

        if sector in FINANCIAL_SECTORS:
            flags.append("fin_sector_skip")  # DCF and D/E not meaningful for these
        else:
            stats["debt_to_equity"] = _guard("de", lambda: get_debt_to_equity(gaap, anchor, flags), flags)
            intrinsic = _guard("dcf", lambda: dcf(gaap, flags), flags)
            if intrinsic is not None and market_cap:
                if intrinsic <= 0:
                    flags.append("dcf_nonpositive_equity")
                else:
                    p_dcf = market_cap / intrinsic
                    # A ratio outside this band means an upstream input broke, not a real signal.
                    if PDCF_SANE_RANGE[0] <= p_dcf <= PDCF_SANE_RANGE[1]:
                        stats["p_dcf"] = p_dcf
                    else:
                        flags.append("pdcf_out_of_range")
            elif intrinsic is not None:
                flags.append("no_market_cap")

    # Normalise to plain floats (pymysql can't bind NaN/inf or numpy scalars reliably).
    for col in METRIC_COLS:
        stats[col] = _clean_float(stats[col])

    n_metrics = sum(stats[c] is not None for c in METRIC_COLS)
    status = "failed" if n_metrics == 0 else ("success" if n_metrics >= MIN_METRICS_TO_RANK else "partial")
    return stats, flags, status


UPSERT_SQL = text("""
    INSERT INTO stock_stats
        (ticker, `date`, sector, industry, fiscal_year_end, pe_ratio, pb_ratio, p_dcf,
         debt_to_equity, reinvestment_efficiency, roe, data_flags)
    VALUES
        (:ticker, :date, :sector, :industry, :fiscal_year_end, :pe_ratio, :pb_ratio, :p_dcf,
         :debt_to_equity, :reinvestment_efficiency, :roe, :data_flags)
    ON DUPLICATE KEY UPDATE
        `date` = VALUES(`date`), sector = VALUES(sector), industry = VALUES(industry),
        fiscal_year_end = VALUES(fiscal_year_end),
        pe_ratio = VALUES(pe_ratio), pb_ratio = VALUES(pb_ratio), p_dcf = VALUES(p_dcf),
        debt_to_equity = VALUES(debt_to_equity),
        reinvestment_efficiency = VALUES(reinvestment_efficiency),
        roe = VALUES(roe), data_flags = VALUES(data_flags)
""")


def save_result(engine, stats, flags, status):
    """Write stats + queue status in ONE transaction.

    Fully failed tickers are NOT written to stock_stats: an all-NULL row would
    be percentile-ranked at a neutral 0.5 and land mid-pack in the final ranking.
    """
    with engine.begin() as conn:
        if status != "failed":
            conn.execute(UPSERT_SQL, {**stats, "data_flags": ",".join(sorted(set(flags)))[:255]})
        conn.execute(text("UPDATE queue SET Status = :s WHERE Ticker = :t"),
                     {"s": status, "t": stats["ticker"]})


def mark_failed(engine, ticker):
    with engine.begin() as conn:
        conn.execute(text("UPDATE queue SET Status = 'failed' WHERE Ticker = :t"), {"t": ticker})


# ==================================================================================
# Ranking
# ==================================================================================
def recompute_rankings(engine):
    """Composite percentile rank (1 = best). Returns the ranked DataFrame."""
    cutoff = (date.today() - timedelta(days=MAX_STATS_AGE_DAYS)).isoformat()
    df = pd.read_sql(
        text("SELECT ticker, sector, industry, " + ", ".join(METRIC_COLS) + ", data_flags "
             "FROM stock_stats WHERE `date` >= :cutoff"),
        con=engine, params={"cutoff": cutoff},
    )

    # Only rank rows with enough data, and never rank negative-equity companies:
    # their book-based ratios are meaningless (a negative D/E would otherwise sort as "best").
    flags = df["data_flags"].fillna("")
    rankable = (df[METRIC_COLS].notna().sum(axis=1) >= MIN_METRICS_TO_RANK) & ~flags.str.contains("neg_equity")
    ranked = df[rankable].copy()

    updates = []
    if not ranked.empty:
        pct_cols = []
        for col in METRIC_COLS:
            ascending = col in LOWER_IS_BETTER      # lowest value -> lowest (best) percentile
            pct = ranked[col].rank(pct=True, ascending=ascending)
            if RANK_WITHIN_SECTOR:
                by_sector = ranked.groupby("sector")[col]
                sector_pct = by_sector.rank(pct=True, ascending=ascending)
                enough = by_sector.transform("count") >= MIN_SECTOR_SAMPLE
                pct = sector_pct.where(enough, pct)
            ranked[f"{col}_pct"] = pct
            pct_cols.append(f"{col}_pct")

        # Missing metric -> neutral 0.5 (rows must still have >= MIN_METRICS_TO_RANK real metrics).
        ranked["composite_score"] = ranked[pct_cols].fillna(0.5).mean(axis=1)
        ranked["overall_rank"] = ranked["composite_score"].rank(method="min").astype(int)
        updates = [
            {"score": float(r.composite_score), "rank": int(r.overall_rank), "ticker": r.ticker}
            for r in ranked.itertuples()
        ]

    with engine.begin() as conn:
        # Clear first so rows that dropped out of the rankable set don't keep stale ranks.
        conn.execute(text("UPDATE stock_stats SET composite_score = NULL, overall_rank = NULL"))
        if updates:
            conn.execute(
                text("UPDATE stock_stats SET composite_score = :score, overall_rank = :rank "
                     "WHERE ticker = :ticker"),
                updates,
            )
    return ranked.sort_values("overall_rank") if not ranked.empty else ranked


# ==================================================================================
# CLI
# ==================================================================================
def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    parser.add_argument("--limit", type=int, default=25,
                        help="max queued tickers to process this run (0 = all, default 25)")
    parser.add_argument("--rank-only", action="store_true", help="skip evaluation, recompute rankings only")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    engine = get_engine()
    ensure_schema(engine)

    if not args.rank_only:
        if not os.environ.get("SEC_USER_AGENT"):
            raise SystemExit('Set SEC_USER_AGENT in info.env, e.g. "Jane Doe jane@example.com" (required by the SEC)')
        session = rq.Session()
        session.headers.update({"User-Agent": os.environ["SEC_USER_AGENT"]})
        book = load_ticker_book(session)

        sql = "SELECT Ticker FROM queue WHERE Status = 'queued' ORDER BY Ticker"
        params = {}
        if args.limit > 0:
            sql += " LIMIT :n"
            params["n"] = args.limit
        tickers = pd.read_sql(text(sql), con=engine, params=params)["Ticker"].tolist()
        log.info("%d tickers to process", len(tickers))

        for ticker in tickers:
            try:
                stats, flags, status = evaluate_ticker(ticker, book, session)
                save_result(engine, stats, flags, status)
                log.info("%s: %s %s", ticker, status, sorted(set(flags)) or "")
            except Exception as exc:  # noqa: BLE001
                if _is_rate_limit(exc):
                    log.error("Rate limited (%s). Stopping; remaining tickers stay queued - re-run later.", exc)
                    break
                log.exception("%s: unexpected error", ticker)
                try:
                    mark_failed(engine, ticker)
                except Exception:  # noqa: BLE001
                    log.exception("%s: could not mark failed", ticker)
            time.sleep(YF_PAUSE)

    ranked = recompute_rankings(engine)
    # Save a point-in-time snapshot of today's ranked set so the strategy
    # can be backtested later (see portfolio/snapshots.py). A snapshot
    # failure must never break the pipeline run, so it only warns.
    try:
        from portfolio.snapshots import snapshot_rankings
        n_snap = snapshot_rankings(engine, ranked)
        if n_snap:
            log.info("Saved %d ranking snapshot rows for %s", n_snap, date.today().isoformat())
    except Exception as exc:  # noqa: BLE001
        log.warning("Could not save ranking snapshot: %r", exc)
    if not ranked.empty:
        print("\nTop 15 by composite score (1 = best):")
        print(ranked[["overall_rank", "ticker", "sector", "composite_score"]].head(15).to_string(index=False))


if __name__ == "__main__":
    main()
