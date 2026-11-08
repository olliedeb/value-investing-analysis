"""Custom backtesting suite for the valuproj value strategy.

What it does: on each rebalance date it takes the top N from the ranking
*as it stood on that date* (from ranking_snapshots -- see snapshots.py),
weights them (same rules as portfolio.py), and holds them until the next
rebalance, tracking the equity curve against a buy-and-hold benchmark.

RUN IN VS CODE: edit the constants below, then press Run. No terminal
flags needed; the CLI flags in main() only default to the constants,
for automated testing. Set SELF_TEST = True and Run to verify the engine
on synthetic data with hand-computed answers.

Read this before trusting a result:
  * Point-in-time only. The backtest uses snapshots, never today's
    ranking replayed over past prices (that would be look-ahead bias).
    Snapshots are saved automatically each time Stage 2 runs, so the
    backtest only covers dates after this ships, and it needs at least
    two snapshot dates. Until then it refuses to run and says why.
  * Prices are Yahoo adjusted closes, cached under portfolio/data/prices/.
    Returns are TOTAL returns: the adjusted close already includes
    dividends (reinvested), and the dividends themselves are tracked
    separately -- see the "dividends" block in the results, which reports
    the cash dividends received, their share of the total gain, and the
    portfolio's dividend yield. That split is what makes a value strategy
    honest: much of its return can be income, not price appreciation.
  * Costs: COST_BPS is charged on one-way turnover at each rebalance
    (buys only, as a fraction of portfolio value). It is an estimate,
    not a quote.
  * Survivorship: a ticker that disappears from Yahoo history is carried
    at its last price and flagged in the warnings.
"""

import json
import sys
from datetime import date, timedelta
from pathlib import Path

# ---------------------------------------------------------------------------
# RUN IN VS CODE: edit these, then press Run.
# ---------------------------------------------------------------------------
TOP_N = 10
WEIGHTING = "score"  # "score" | "equal" | "rank" -- same rules as portfolio.py
REBALANCE = "monthly"  # "monthly" | "quarterly" | "annual" | "2-year" | "3-year" | "5-year" | "buy-and-hold" | "Nm" (e.g. "18m")
START_DATE = ""  # "" = earliest snapshot date
END_DATE = ""  # "" = today
INITIAL_CAPITAL = 100000.0
COST_BPS = 10.0  # round-trip-ish cost, charged on one-way turnover
BENCHMARK = "SPY"  # the investable S&P 500 (SPDR ETF), not ^GSPC: ^GSPC is a
# price-only index -- Yahoo publishes no dividends for it and its Adj Close
# equals its Close -- so it understates the benchmark by the dividend yield
# (about 11 points over the 5 years to Nov 2026). SPY's Adj Close is a true
# total return and it carries a Dividends column, so the benchmark gets the
# same dividend treatment as the portfolio. The 0.09%/yr expense ratio is a
# real (if small) cost of tracking the index.
REFRESH_PRICES = False  # True to re-download prices instead of using the cache
SELF_TEST = False  # True to verify the engine on synthetic data

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    from dotenv import load_dotenv  # noqa: E402

    load_dotenv(ROOT / "info.env")
except ImportError:
    pass  # dotenv is optional: env vars may already be set (see app.py)

import db  # noqa: E402
import pandas as pd  # noqa: E402
from portfolio.portfolio import compute_weights  # noqa: E402
from portfolio.snapshots import load_snapshots  # noqa: E402

try:
    from portfolio.analysis import analyze_results  # noqa: E402
except Exception:  # noqa: BLE001 -- analysis is a bonus; never break the backtest
    analyze_results = None

PRICE_CACHE_DIR = Path(__file__).resolve().parent / "data" / "prices"
RESULTS_PATH = Path(__file__).resolve().parent / "backtest_results.json"


# ---------------------------------------------------------------------------
# Prices (Yahoo, cached) -- total returns, with dividends tracked
# ---------------------------------------------------------------------------
# Yahoo's "Close" is split-adjusted but NOT dividend-adjusted; "Adj Close"
# is split- AND dividend-adjusted (the total-return price); "Dividends" is
# the cash dividend per share on each ex-date, in the same (split-adjusted)
# scale as Close. So the engine runs on Adj Close for returns, and the
# dividend accounting converts the engine's share counts back to real
# shares with Adj/Close at the rebalance date -- see _dividends_received.
# The cache stores all three columns; a cache file without Dividends is
# treated as stale and re-downloaded.
def load_price_series(ticker, start, end, cache_dir=PRICE_CACHE_DIR, refresh=False):
    """(Adj Close, Close, Dividends) Series for one ticker.

    From cache when it covers [start, end] and carries a Dividends column.
    Returns (None, None, None) when the history cannot be fetched.
    """
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = cache_dir / f"{ticker.replace('^', '').replace('/', '_')}.csv"
    start_ts, end_ts = pd.Timestamp(start), pd.Timestamp(end)
    if path.exists() and not refresh:
        try:
            df = pd.read_csv(path, parse_dates=["Date"], index_col="Date")
            if (len(df) and "Dividends" in df.columns
                    and df.index.min() <= start_ts and df.index.max() >= end_ts):
                adj = df["AdjClose"] if "AdjClose" in df.columns else df["Close"]
                return (adj, df["Close"], df["Dividends"].fillna(0.0))
        except Exception:  # noqa: BLE001 -- corrupt cache: fall through to download
            pass
    import yfinance as yf

    try:
        # actions=True is essential: without it yfinance omits the
        # Dividends (and Stock Splits) columns entirely, and the cache
        # would silently record zero dividends for every ticker.
        df = yf.download(ticker, start=start,
                         end=(end_ts + pd.Timedelta(days=1)).date().isoformat(),
                         auto_adjust=False, actions=True, progress=False)
    except Exception as exc:  # noqa: BLE001 -- no network etc: caller treats None as "skip"
        print(f"  Warning: could not download prices for {ticker}: {exc}")
        return None, None, None
    if df is None or df.empty:
        return None, None, None
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    if "Adj Close" not in df.columns or "Close" not in df.columns:
        return None, None, None
    out = pd.DataFrame({"AdjClose": df["Adj Close"],
                        "Close": df["Close"],
                        "Dividends": df["Dividends"].fillna(0.0)
                        if "Dividends" in df.columns else 0.0})
    out.index = pd.to_datetime(out.index)
    out.index.name = "Date"
    out.to_csv(path)
    return (out["AdjClose"], out["Close"], out["Dividends"])


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------
# Holding periods, in months. "buy-and-hold" is separate (buy once, never
# rebalance). A bare "Nm" (e.g. "18m") works for any other window, so the
# set is open-ended rather than a fixed list.
REBALANCE_MONTHS = {"monthly": 1, "quarterly": 3, "annual": 12,
                    "2-year": 24, "3-year": 36, "5-year": 60}


def _rebalance_months(frequency):
    """Months per holding window, or None for buy-and-hold."""
    if frequency == "buy-and-hold":
        return None
    if frequency in REBALANCE_MONTHS:
        return REBALANCE_MONTHS[frequency]
    if isinstance(frequency, str) and frequency.endswith("m") \
            and frequency[:-1].isdigit() and int(frequency[:-1]) >= 1:
        return int(frequency[:-1])
    raise ValueError(f"Unknown rebalance frequency: {frequency!r}")


def _rebalance_dates(calendar, frequency):
    """First trading day of each holding window.

    Windows are counted in whole months on the month index, so quarterly
    windows are Jan-Mar, Apr-Jun, ... and multi-year windows start in
    January of a year divisible by the window (2-year: 2024-25, 2026-27,
    ...; 3-year: 2022-24, 2025-27, ...). "buy-and-hold" is just the first
    trading day -- the strategy buys once and never rebalances. An unknown
    frequency raises instead of silently behaving like something else.
    """
    months = _rebalance_months(frequency)
    if months is None:
        return list(calendar[:1])
    dates = []
    seen = set()
    for d in calendar:
        key = (d.year * 12 + (d.month - 1)) // months
        if key not in seen:
            seen.add(key)
            dates.append(d)
    return dates


def _price_on(series, day):
    """Last available price on or before `day`, or None (also None when the
    series has no real price yet -- forward-fill leaves NaN before the first
    observation, and NaN must not be mistaken for a price)."""
    idx = series.index[series.index <= day]
    if len(idx) == 0:
        return None
    value = float(series.loc[idx[-1]])
    return None if pd.isna(value) else value


def _dividends_received(rebalance_log, div_data, end):
    """Cash dividends received, from the rebalance log.

    div_data: {ticker: (close_sa Series, dividends Series)} -- Close is
    the split-adjusted (not dividend-adjusted) price, dividends are
    $/share on each ex-date in the same scale. A holding bought on d1 and
    sold on d2 receives the dividends whose ex-date falls strictly after
    d1 and strictly before d2 (a dividend ex-dated on the buy date
    belongs to the seller; the price that day already reflects it).
    The log's share counts are in Adj Close units, so they are converted
    to real shares with Adj/Close at the buy date.
    Returns (total, by_ticker, by_year, per_rebalance).
    """
    total = 0.0
    by_ticker, by_year, per_rebalance = {}, {}, []
    n = len(rebalance_log)
    end = pd.Timestamp(end)
    for i, rb in enumerate(rebalance_log):
        d1 = pd.Timestamp(rb["date"])
        d2 = (pd.Timestamp(rebalance_log[i + 1]["date"]) if i + 1 < n else end)
        rb_total = 0.0
        for h in rb["holdings"]:
            t = h["ticker"]
            if t not in div_data:
                continue
            close_sa, div_s = div_data[t]
            close_price = _price_on(close_sa, d1)
            if not close_price:
                continue
            actual_shares = h["shares"] * h["price"] / close_price
            if i + 1 < n:
                events = div_s[(div_s.index > d1) & (div_s.index < d2)]
            else:
                events = div_s[(div_s.index > d1) & (div_s.index <= d2)]
            for ex_date, dps in events.items():
                if dps:
                    amount = actual_shares * float(dps)
                    rb_total += amount
                    by_ticker[t] = by_ticker.get(t, 0.0) + amount
                    by_year[int(ex_date.year)] = by_year.get(int(ex_date.year), 0.0) + amount
        total += rb_total
        per_rebalance.append(rb_total)
    return total, by_ticker, by_year, per_rebalance


def run_backtest(snapshots, prices, benchmark_prices, config,
                 div_data=None, benchmark_div_data=None):
    """Run the strategy over point-in-time snapshots.

    snapshots: {snapshot_date str: [rows with ticker, overall_rank, composite_score]}
    prices: {ticker: pd.Series of adjusted closes} -- total-return prices,
        so the equity curve already includes reinvested dividends.
    config: {top_n, weighting, rebalance, cost_bps, initial_capital, start, end}
    div_data: optional {ticker: (close_sa Series, dividends Series)} for the
        dividend accounting; benchmark_div_data the same pair for ^GSPC.
        Without them the results carry "dividends": None.
    Returns a results dict (equity curve, stats, rebalance log, warnings,
    dividends).
    """
    start = pd.Timestamp(config["start"])
    end = pd.Timestamp(config["end"])
    capital = float(config["initial_capital"])
    cost_bps = float(config["cost_bps"])
    top_n = int(config["top_n"])
    weighting = config["weighting"]

    calendar = benchmark_prices[(benchmark_prices.index >= start)
                                & (benchmark_prices.index <= end)].index
    if len(calendar) == 0:
        raise ValueError("No benchmark trading days in the backtest window.")
    # Forward-fill every price series onto the calendar.
    aligned = {}
    for t, s in prices.items():
        aligned[t] = s.reindex(calendar, method="ffill")
    bench = benchmark_prices.reindex(calendar, method="ffill")

    snap_dates = sorted(pd.Timestamp(d) for d in snapshots)
    reb_dates = [d for d in _rebalance_dates(calendar, config["rebalance"])
                 if start <= d <= end]

    shares = {}  # ticker -> shares held
    cash = capital
    equity_curve, bench_curve = [], []
    rebalance_log, warnings = [], []
    bench_shares = capital / float(bench.iloc[0])

    def value_on(day):
        total = cash
        for t, sh in shares.items():
            p = _price_on(aligned[t], day) if t in aligned else None
            if p is None:
                continue
            total += sh * p
        return total

    for day in calendar:
        if day in reb_dates:
            snap_date = max([d for d in snap_dates if d <= day], default=None)
            if snap_date is None:
                warnings.append(f"{day.date()}: no snapshot yet -- staying in cash.")
            else:
                rows = sorted(snapshots[snap_date.strftime("%Y-%m-%d")],
                              key=lambda r: (r["overall_rank"] is None, r["overall_rank"]))
                target_rows = []
                for r in rows[:top_n]:
                    if r["ticker"] in aligned and _price_on(aligned[r["ticker"]], day) is not None:
                        target_rows.append(r)
                    else:
                        warnings.append(f"{day.date()}: {r['ticker']} has no price data -- skipped.")
                value = value_on(day)
                old_weights = {}
                if value > 0:
                    for t, sh in shares.items():
                        p = _price_on(aligned[t], day)
                        old_weights[t] = (sh * p / value) if p else 0.0
                weights = compute_weights(target_rows, weighting) if target_rows else {}
                turnover = sum(max(weights.get(t, 0.0) - old_weights.get(t, 0.0), 0.0)
                               for t in set(weights) | set(old_weights))
                cost = value * turnover * cost_bps / 10000.0
                investable = value - cost
                new_shares = {}
                for t, w in weights.items():
                    p = _price_on(aligned[t], day)
                    new_shares[t] = (w * investable / p) if p else 0.0
                shares = {t: s for t, s in new_shares.items() if s > 0}
                cash = investable - sum(shares[t] * _price_on(aligned[t], day)
                                        for t in shares)
                rebalance_log.append({
                    "date": day.strftime("%Y-%m-%d"),
                    "snapshot_date": snap_date.strftime("%Y-%m-%d"),
                    "holdings": [{"ticker": t, "weight": weights[t],
                                  "shares": shares[t],
                                  "price": _price_on(aligned[t], day)}
                                 for t in shares],
                    "turnover": turnover, "cost": cost,
                })
        equity_curve.append({"date": day.strftime("%Y-%m-%d"),
                             "portfolio": value_on(day),
                             "benchmark": bench_shares * float(bench.loc[day])})
    bench_curve = [e["benchmark"] for e in equity_curve]
    port_curve = [e["portfolio"] for e in equity_curve]

    def stats(curve):
        s = pd.Series(curve, index=calendar)
        total_return = float(s.iloc[-1] / s.iloc[0] - 1) if s.iloc[0] else 0.0
        days = max((calendar[-1] - calendar[0]).days, 1)
        cagr = float((s.iloc[-1] / s.iloc[0]) ** (365.25 / days) - 1) if s.iloc[0] else 0.0
        daily = s.pct_change().dropna()
        vol = float(daily.std() * (252 ** 0.5)) if len(daily) > 1 else 0.0
        sharpe = float(daily.mean() / daily.std() * (252 ** 0.5)) if len(daily) > 1 and daily.std() else 0.0
        running_max = s.cummax()
        max_dd = float(((s - running_max) / running_max).min()) if len(s) else 0.0
        return {"total_return": total_return, "cagr": cagr, "ann_vol": vol,
                "sharpe_rf0": sharpe, "max_drawdown": max_dd,
                "start_value": float(s.iloc[0]), "end_value": float(s.iloc[-1])}

    p_stats = stats(port_curve)
    b_stats = stats(bench_curve)

    # Dividends. The equity curve already includes them (Adj Close is a
    # total-return price), so this block only splits the gain into income
    # vs price appreciation -- it is not added to the equity a second time.
    # Note the split is approximate: dividends received are reported as
    # cash, but the reinvested part of them also earns a return inside the
    # Adj Close series, so income + price appreciation slightly exceeds
    # the reported gain by that compounding.
    div_block = None
    if div_data:
        total_div, by_ticker, by_year, _per_rb = _dividends_received(
            rebalance_log, div_data, end)
        gain = p_stats["end_value"] - capital
        years = max((end - start).days / 365.25, 1e-9)
        avg_equity = float(pd.Series(port_curve).mean()) if port_curve else capital
        bench_div = None
        bench_div_pct = None
        if benchmark_div_data is not None:
            bench_close_sa, bench_div_s = benchmark_div_data
            bench_close0 = _price_on(bench_close_sa, start)
            if bench_close0:
                bench_shares = capital / bench_close0
                bench_div = float(bench_shares
                                  * bench_div_s[(bench_div_s.index > start)
                                                & (bench_div_s.index <= end)].sum())
                bench_gain = b_stats["end_value"] - capital
                bench_div_pct = bench_div / bench_gain if bench_gain > 0 else None
        div_block = {
            "total_received": total_div,
            "pct_of_total_return": (total_div / gain) if gain > 0 else None,
            "annual_yield": (total_div / avg_equity) / years if avg_equity else None,
            "by_ticker": {t: round(v, 2) for t, v in
                          sorted(by_ticker.items(), key=lambda kv: -kv[1])},
            "by_year": {str(y): round(v, 2) for y, v in sorted(by_year.items())},
            "benchmark_total_received": bench_div,
            "benchmark_pct_of_total_return": bench_div_pct,
        }
    return {
        "config": {**config, "start": start.strftime("%Y-%m-%d"),
                   "end": end.strftime("%Y-%m-%d")},
        "stats": {"portfolio": p_stats, "benchmark": b_stats,
                  "excess_return": p_stats["total_return"] - b_stats["total_return"]},
        "equity_curve": equity_curve,
        "rebalances": rebalance_log,
        "warnings": sorted(set(warnings)),
        "dividends": div_block,
    }


# ---------------------------------------------------------------------------
# Self-test (synthetic data, hand-computed answers)
# ---------------------------------------------------------------------------
def run_self_test():
    """Verify the engine on synthetic data. No network, no database."""
    days = pd.bdate_range("2025-01-02", "2025-02-28")
    a = pd.Series([100.0 if d <= pd.Timestamp("2025-01-31") else
                   (121.0 if d == pd.Timestamp("2025-02-28") else 110.0)
                   for d in days], index=days)
    b = pd.Series([50.0] * len(days), index=days)
    bench = pd.Series([100.0] * len(days), index=days)
    snaps = {"2025-01-02": [{"ticker": "A", "overall_rank": 1, "composite_score": 0.2},
                            {"ticker": "B", "overall_rank": 2, "composite_score": 0.4}],
             "2025-02-03": [{"ticker": "A", "overall_rank": 1, "composite_score": 0.2},
                            {"ticker": "B", "overall_rank": 2, "composite_score": 0.4}]}
    cfg = {"top_n": 10, "weighting": "score", "rebalance": "monthly",
           "cost_bps": 0.0, "initial_capital": 100000.0,
           "start": "2025-01-02", "end": "2025-02-28"}
    res = run_backtest(snaps, {"A": a, "B": b}, bench, cfg)
    curve = {e["date"]: e["portfolio"] for e in res["equity_curve"]}
    # Jan 31: prices unchanged (A still 100, B 50), so equity is still 100,000.
    assert abs(curve["2025-01-31"] - 100000.0) < 0.01, curve["2025-01-31"]
    # Feb 3: A steps 100 -> 110, so A 571.4286 sh x 110 + B 857.1429 sh x 50
    # = 105,714.29 (the rebalance that day keeps the same weights, no cost).
    assert abs(curve["2025-02-03"] - 105714.2857) < 0.01, curve["2025-02-03"]
    # Feb 28: after the Feb 3 rebalance (same weights, no cost),
    # A 549.1651 sh x 121 + B 906.1224 sh x 50 = 111,755.10
    assert abs(curve["2025-02-28"] - 111755.1020) < 0.01, curve["2025-02-28"]
    assert abs(res["stats"]["benchmark"]["total_return"]) < 1e-9
    assert len(res["rebalances"]) == 2
    # Costs: one rebalance from cash at 10 bps on $100,000 costs $100.
    cfg_cost = {**cfg, "cost_bps": 10.0, "end": "2025-01-02"}
    res_cost = run_backtest(snaps, {"A": a, "B": b}, bench, cfg_cost)
    curve_cost = {e["date"]: e["portfolio"] for e in res_cost["equity_curve"]}
    assert abs(curve_cost["2025-01-02"] - 99900.0) < 0.01, curve_cost["2025-01-02"]
    assert abs(res_cost["rebalances"][0]["cost"] - 100.0) < 0.01
    # Annual: one rebalance (Jan 2) over the two-month window; the Feb 3
    # price step happens but nothing is rebalanced, so the Jan 2 share
    # counts carry through: A 571.4286 x 121 + B 857.1429 x 50 = 112,000.
    cfg_ann = {**cfg, "rebalance": "annual"}
    res_ann = run_backtest(snaps, {"A": a, "B": b}, bench, cfg_ann)
    curve_ann = {e["date"]: e["portfolio"] for e in res_ann["equity_curve"]}
    assert len(res_ann["rebalances"]) == 1
    assert abs(curve_ann["2025-02-28"] - 112000.0) < 0.01, curve_ann["2025-02-28"]
    # Buy-and-hold: also exactly one rebalance, same path as annual here.
    cfg_bh = {**cfg, "rebalance": "buy-and-hold"}
    res_bh = run_backtest(snaps, {"A": a, "B": b}, bench, cfg_bh)
    assert len(res_bh["rebalances"]) == 1
    assert abs(res_bh["stats"]["portfolio"]["end_value"] - 112000.0) < 0.01
    # Date logic: every holding window gives its first trading day.
    # 2019-01-02 .. 2025-12-31 is 7 years: monthly 84, quarterly 28,
    # annual 7, 2-year 4 ([18-19],[20-21],[22-23],[24-25]), 3-year 3
    # ([19-21],[22-24],[25-27]), 5-year 3 ([15-19],[20-24],[25-29]),
    # "18m" 5, buy-and-hold 1.
    long_cal = pd.bdate_range("2019-01-02", "2025-12-31")
    assert len(_rebalance_dates(long_cal, "monthly")) == 84
    assert len(_rebalance_dates(long_cal, "quarterly")) == 28
    assert len(_rebalance_dates(long_cal, "annual")) == 7
    assert len(_rebalance_dates(long_cal, "2-year")) == 4
    assert len(_rebalance_dates(long_cal, "3-year")) == 3
    assert len(_rebalance_dates(long_cal, "5-year")) == 3
    assert len(_rebalance_dates(long_cal, "18m")) == 5
    assert len(_rebalance_dates(long_cal, "buy-and-hold")) == 1
    # The old short-window checks still hold (annual 2, quarterly 6).
    short_cal = pd.bdate_range("2024-01-02", "2025-06-30")
    assert len(_rebalance_dates(short_cal, "annual")) == 2
    assert len(_rebalance_dates(short_cal, "buy-and-hold")) == 1
    assert len(_rebalance_dates(short_cal, "quarterly")) == 6
    try:
        _rebalance_dates(short_cal, "weekly")
        raise AssertionError("weekly should raise ValueError")
    except ValueError:
        pass
    # POINT-IN-TIME SELECTION (the whole point of the snapshots): the
    # ranking changes between the two snapshot dates, and the holdings
    # must follow the ranking *as it stood on each date* -- never today's
    # ranking replayed over the past. Jan 2 ranks A then B; Feb 3 ranks B
    # then C (A dropped out, C is new). All prices are flat, so any
    # holdings change comes from the ranking change alone, and equity
    # stays exactly 100,000 throughout.
    # Flat prices here (A's main-test series steps up in February, which
    # would muddy the point): with flat prices, any holdings change comes
    # from the ranking change alone.
    a_flat = pd.Series([100.0] * len(days), index=days)
    c = pd.Series([20.0] * len(days), index=days)
    snaps_flip = {"2025-01-02": [{"ticker": "A", "overall_rank": 1, "composite_score": 0.2},
                                  {"ticker": "B", "overall_rank": 2, "composite_score": 0.4}],
                  "2025-02-03": [{"ticker": "B", "overall_rank": 1, "composite_score": 0.2},
                                  {"ticker": "C", "overall_rank": 2, "composite_score": 0.4}]}
    cfg_flip = {**cfg, "top_n": 2}
    res_flip = run_backtest(snaps_flip, {"A": a_flat, "B": b, "C": c}, bench, cfg_flip)
    assert len(res_flip["rebalances"]) == 2
    first, second = res_flip["rebalances"]
    assert first["snapshot_date"] == "2025-01-02"
    assert {h["ticker"] for h in first["holdings"]} == {"A", "B"}, first["holdings"]
    assert second["snapshot_date"] == "2025-02-03"
    assert {h["ticker"] for h in second["holdings"]} == {"B", "C"}, second["holdings"]
    # C (only ranked from Feb 3) is never held in January; A (dropped out
    # by Feb 3) is never held after. That is the anti-look-ahead guarantee.
    assert "C" not in {h["ticker"] for h in first["holdings"]}
    assert "A" not in {h["ticker"] for h in second["holdings"]}
    assert abs(res_flip["stats"]["portfolio"]["end_value"] - 100000.0) < 0.01
    assert abs(second["turnover"] - 0.5714285714) < 1e-6, second["turnover"]
    # DIVIDENDS: ticker D pays a $2 dividend, ex-date 2025-01-15. Its
    # split-adjusted Close is flat 100, so its Adj Close is 98 before the
    # ex-date and 100 on/after it (100 x (1 - 2/100)). Buy $100,000 on
    # Jan 2 at Adj 98 -> 1,020.4081 adjusted shares, i.e. 1,000 real
    # shares (1,020.4081 x 98/100). Dividends received = 1,000 x $2 =
    # $2,000. End equity = 1,020.4081 x 100 = $102,040.82, a $2,040.82
    # gain, so dividends are $2,000 / $2,040.82 = 98.0% of the gain
    # (the other 2% is the return on the reinvested dividend).
    d_close = pd.Series([100.0] * len(days), index=days)
    d_div = pd.Series([0.0] * len(days), index=days)
    d_div.loc[pd.Timestamp("2025-01-15")] = 2.0
    d_adj = pd.Series([98.0 if d < pd.Timestamp("2025-01-15") else 100.0
                       for d in days], index=days)
    snaps_div = {"2025-01-02": [{"ticker": "D", "overall_rank": 1,
                                 "composite_score": 0.2}]}
    cfg_div = {**cfg, "top_n": 1}
    res_div = run_backtest(snaps_div, {"D": d_adj}, bench, cfg_div,
                           div_data={"D": (d_close, d_div)})
    assert abs(res_div["stats"]["portfolio"]["end_value"] - 102040.82) < 0.01, \
        res_div["stats"]["portfolio"]["end_value"]
    dv = res_div["dividends"]
    assert dv is not None
    assert abs(dv["total_received"] - 2000.0) < 0.01, dv["total_received"]
    assert abs(dv["pct_of_total_return"] - 2000.0 / 2040.82) < 1e-4, \
        dv["pct_of_total_return"]
    assert dv["by_ticker"] == {"D": 2000.0}, dv["by_ticker"]
    assert dv["by_year"] == {"2025": 2000.0}, dv["by_year"]
    # Without div_data the results carry no dividend block, but the
    # equity (which uses Adj Close) is unchanged.
    res_nodiv = run_backtest(snaps_div, {"D": d_adj}, bench, cfg_div)
    assert res_nodiv["dividends"] is None
    assert abs(res_nodiv["stats"]["portfolio"]["end_value"] - 102040.82) < 0.01
    print("SELF-TEST PASSED: equity path and costs match hand-computed values.")
    print(f"  Feb 3 equity: {curve['2025-02-03']:,.2f} (expected 105,714.29)")
    print(f"  Feb 28 equity: {curve['2025-02-28']:,.2f} (expected 111,755.10)")
    print(f"  Cost case: equity after first buy {curve_cost['2025-01-02']:,.2f} (expected 99,900.00)")
    print(f"  Annual / buy-and-hold Feb 28 equity: {curve_ann['2025-02-28']:,.2f} (expected 112,000.00)")
    print(f"  Holding windows over 2019-2025: monthly 84, quarterly 28, annual 7, "
          f"2-year 4, 3-year 3, 5-year 3, 18m 5, buy-and-hold 1")
    print(f"  Point-in-time: Jan holdings A+B, Feb holdings B+C (A out, C in), equity flat 100,000.00")
    print(f"  Dividends: $2,000.00 received on $100,000 (98.0% of the $2,040.82 gain); "
          f"no-div-data run still returns the same $102,040.82 equity")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main():
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--top-n", type=int, default=TOP_N)
    parser.add_argument("--weighting", choices=["score", "equal", "rank"], default=WEIGHTING)
    parser.add_argument("--rebalance", default=REBALANCE,
                        help="holding period: monthly | quarterly | annual | "
                             "2-year | 3-year | 5-year | buy-and-hold | Nm (e.g. 18m)")
    parser.add_argument("--start", default=START_DATE)
    parser.add_argument("--end", default=END_DATE)
    parser.add_argument("--capital", type=float, default=INITIAL_CAPITAL)
    parser.add_argument("--cost-bps", type=float, default=COST_BPS)
    parser.add_argument("--refresh-prices", action="store_true", default=REFRESH_PRICES)
    parser.add_argument("--self-test", action="store_true", default=SELF_TEST)
    args = parser.parse_args()
    try:
        _rebalance_months(args.rebalance)
    except ValueError as exc:
        parser.error(str(exc))

    if args.self_test:
        run_self_test()
        return

    engine = db.get_engine()
    snapshots, err = load_snapshots(engine)
    if err:
        print(f"Cannot backtest yet: {err}")
        return
    dates = sorted(snapshots)
    if len(dates) < 2:
        print(f"Cannot backtest yet: only {len(dates)} snapshot date(s) "
              f"({', '.join(dates)}). Need at least two, on different dates -- "
              "snapshots are saved each time Stage 2 runs.")
        return
    start = args.start or dates[0]
    end = args.end or date.today().isoformat()
    tickers = sorted({r["ticker"] for rows in snapshots.values() for r in rows})
    print(f"Snapshots: {len(dates)} dates ({dates[0]} .. {dates[-1]}), "
          f"{len(tickers)} tickers. Downloading prices...")
    prices, div_data = {}, {}
    for t in tickers:
        adj, close_sa, div_s = load_price_series(
            t, start, end, refresh=args.refresh_prices)
        if adj is None:
            print(f"  Warning: no price history for {t} -- it will be skipped.")
        else:
            prices[t] = adj
            div_data[t] = (close_sa, div_s)
    bench_adj, bench_close, bench_div = load_price_series(
        BENCHMARK, start, end, refresh=args.refresh_prices)
    if bench_adj is None:
        print(f"Cannot backtest: no price history for benchmark {BENCHMARK}.")
        return
    cfg = {"top_n": args.top_n, "weighting": args.weighting,
           "rebalance": args.rebalance, "cost_bps": args.cost_bps,
           "initial_capital": args.capital, "start": start, "end": end}
    res = run_backtest(snapshots, prices, bench_adj, cfg,
                       div_data=div_data,
                       benchmark_div_data=(bench_close, bench_div))
    if analyze_results is not None:
        try:
            res["analysis"] = analyze_results(res)
        except Exception as exc:  # noqa: BLE001
            print(f"  Warning: analysis failed: {exc}")
            res["analysis"] = None
    else:
        res["analysis"] = None
    p, b = res["stats"]["portfolio"], res["stats"]["benchmark"]
    print(f"\nBacktest {start} .. {end} -- top {args.top_n}, {args.weighting}-weighted, "
          f"{args.rebalance} rebalancing, {args.cost_bps} bps costs")
    print(f"{'':<14}{'Portfolio':>12}{'Benchmark':>12}")
    print(f"{'Total return':<14}{p['total_return']:>11.1%}{b['total_return']:>11.1%}")
    print(f"{'CAGR':<14}{p['cagr']:>11.1%}{b['cagr']:>11.1%}")
    print(f"{'Ann. vol':<14}{p['ann_vol']:>11.1%}{b['ann_vol']:>11.1%}")
    print(f"{'Sharpe (rf=0)':<14}{p['sharpe_rf0']:>12.2f}{b['sharpe_rf0']:>12.2f}")
    print(f"{'Max drawdown':<14}{p['max_drawdown']:>11.1%}{b['max_drawdown']:>11.1%}")
    print(f"Excess return vs benchmark: {res['stats']['excess_return']:+.1%}")
    print(f"Rebalances: {len(res['rebalances'])}")
    d = res.get("dividends")
    if d:
        print(f"Dividends received: ${d['total_received']:,.0f}"
              + (f" ({d['pct_of_total_return']:.0%} of the total gain)" if d['pct_of_total_return'] is not None else "")
              + (f" | yield {d['annual_yield']:.1%}/yr" if d['annual_yield'] is not None else ""))
        if d.get("benchmark_total_received") is not None:
            print(f"Benchmark dividends: ${d['benchmark_total_received']:,.0f}"
                  + (f" ({d['benchmark_pct_of_total_return']:.0%} of its gain)" if d['benchmark_pct_of_total_return'] is not None else ""))
    a = res.get("analysis") or {}
    if a:
        print(f"Beta {a['beta']:.2f} | Alpha (ann.) {a['alpha_annualized']:+.1%} | "
              f"Corr {a['correlation']:.2f} | Tracking err {a['tracking_error']:.1%} | "
              f"Info ratio {a['information_ratio']:.2f}")
        print(f"Sortino {a['sortino']:.2f} | Calmar {a['calmar']:.2f} | "
              f"Avg turnover {a['avg_turnover']:.1%} | Total costs ${a['total_cost']:,.0f}")
        if a.get("best_month"):
            print(f"Best month {a['best_month']['month']} {a['best_month']['portfolio']:+.1%} | "
                  f"Worst month {a['worst_month']['month']} {a['worst_month']['portfolio']:+.1%} | "
                  f"Positive months {a['positive_months_pct']:.0%}")
        for dd in (a.get("drawdowns") or [])[:3]:
            print(f"Drawdown {dd['depth']:.1%} from {dd['start']} (trough {dd['trough']}, "
                  f"recovery {dd['recovery'] or 'not yet'})")
    for w in res["warnings"]:
        print(f"  Warning: {w}")
    RESULTS_PATH.write_text(json.dumps(res, indent=2))
    print(f"Saved to {RESULTS_PATH}")


if __name__ == "__main__":
    main()
