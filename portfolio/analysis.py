"""Comprehensive performance analysis for valuproj backtest results.

Takes the results dict produced by portfolio/backtest.py (see its
docstring for the exact shape) and returns a plain-dict analysis.

Pure function: no I/O, no network, no database. stdlib + pandas only.

Design notes / edge-case decisions:
  * Monthly returns are month-end to month-end. The first month has no
    previous month-end, so it starts at the first curve value.
  * best_month / worst_month only consider *full* months: a month counts
    as full when the curve's first entry in it falls within the first 3
    calendar days of the month and its last entry falls within the last 3
    calendar days (allowing for weekends/holidays at month boundaries).
    A curve shorter than one full month gives best/worst = None.
  * A drawdown episode runs from a running peak through its lowest
    trough to the first date equity regains the peak (recovery = None
    when it never does). A flat stretch at the peak counts the latest
    peak date as the episode start. Episodes are ranked by depth and
    the worst 5 are returned.
  * Sortino's downside deviation is sqrt(mean(min(r, 0)^2)) over all
    daily returns -- positive days contribute 0, only negative days
    contribute. No negative days means downside deviation 0, so
    sortino is 0.0.
  * Beta/correlation with a flat benchmark (zero variance) are 0.0;
    alpha then reduces to the portfolio's annualised mean daily return.
  * Nothing is rounded; every float is sanitised with math.isfinite so
    the output never contains NaN or inf.
"""

import calendar
import math
from datetime import date

import pandas as pd


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _finite(value, default=0.0):
    """Coerce to a plain finite float, or `default` when not finite."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return default
    return v if math.isfinite(v) else default


def _ret(start_value, end_value):
    """Simple return end/start - 1, guarded against a zero/None start."""
    if start_value in (None, 0) or end_value is None:
        return 0.0
    return _finite(end_value / start_value - 1.0)


def _parse_curve(equity_curve):
    """Equity curve -> (dates [date], portfolio [float], benchmark [float]).

    Skips rows with an unparseable date; non-numeric values become None
    and are dropped together with their row.
    """
    dates, port, bench = [], [], []
    for row in equity_curve or []:
        try:
            d = date.fromisoformat(str(row.get("date")))
            p = float(row.get("portfolio"))
            b = float(row.get("benchmark"))
        except (TypeError, ValueError, AttributeError):
            continue
        if not (math.isfinite(p) and math.isfinite(b)):
            continue
        dates.append(d)
        port.append(p)
        bench.append(b)
    return dates, port, bench


def _period_returns(dates, values):
    """Group values by (year, month) -> list of (key, start, end) in order.

    A period's start is the previous period's end value, except the first
    period, which starts at its first value.
    """
    groups = {}
    order = []
    for d, v in zip(dates, values):
        key = (d.year, d.month)
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(v)
    out = []
    prev_end = None
    for key in order:
        vals = groups[key]
        start = vals[0] if prev_end is None else prev_end
        end = vals[-1]
        out.append((key, start, end))
        prev_end = end
    return out


def _is_full_month(dates, year, month):
    """True when the curve covers the month from near its start to near
    its end (first entry within the first 3 days, last entry within the
    last 3 days of the month)."""
    month_dates = [d for d in dates if d.year == year and d.month == month]
    if not month_dates:
        return False
    last_day = calendar.monthrange(year, month)[1]
    return month_dates[0].day <= 3 and month_dates[-1].day >= last_day - 2


# ---------------------------------------------------------------------------
# Main analysis
# ---------------------------------------------------------------------------
def analyze_results(results):
    """Compute comprehensive performance analysis from a backtest results dict."""
    results = results or {}
    equity_curve = results.get("equity_curve") or []
    rebalances = results.get("rebalances") or []
    stats = results.get("stats") or {}
    port_stats = stats.get("portfolio") or {}

    dates, port, bench = _parse_curve(equity_curve)

    # --- Monthly / annual returns -------------------------------------------
    # Benchmark monthly returns share the portfolio's period boundaries:
    # each month's start is the previous month's end value.
    bench_periods = dict(((y, m), (s, e)) for (y, m), s, e in _period_returns(dates, bench))
    monthly = []
    for (year, month), start, end in _period_returns(dates, port):
        b_start, b_end = bench_periods.get((year, month), (None, None))
        monthly.append({
            "month": f"{year:04d}-{month:02d}",
            "portfolio": _ret(start, end),
            "benchmark": _ret(b_start, b_end),
        })

    annual = _annual_returns(dates, port, bench)

    full_months = [m for m in monthly
                   if _is_full_month(dates, int(m["month"][:4]), int(m["month"][5:7]))]
    if full_months:
        best = max(full_months, key=lambda m: m["portfolio"])
        worst = min(full_months, key=lambda m: m["portfolio"])
        best_month = {"month": best["month"], "portfolio": best["portfolio"]}
        worst_month = {"month": worst["month"], "portfolio": worst["portfolio"]}
    else:
        best_month = None
        worst_month = None
    positive_months_pct = (
        sum(1 for m in monthly if m["portfolio"] > 0) / len(monthly)
        if monthly else None
    )

    # --- Drawdown episodes --------------------------------------------------
    drawdowns = _drawdown_episodes(dates, port)

    # --- Daily-return statistics --------------------------------------------
    if len(port) >= 2:
        index = pd.DatetimeIndex(dates)
        port_ret = pd.Series(port, index=index).pct_change().dropna()
        bench_ret = pd.Series(bench, index=index).pct_change().dropna()
        port_ret = port_ret.replace([float("inf"), float("-inf")], float("nan")).dropna()
        bench_ret = bench_ret.replace([float("inf"), float("-inf")], float("nan")).dropna()
        # Align (both series share the index, but be safe after the drops).
        port_ret, bench_ret = port_ret.align(bench_ret, join="inner")
    else:
        port_ret = pd.Series(dtype=float)
        bench_ret = pd.Series(dtype=float)

    if len(port_ret) >= 2 and len(bench_ret) >= 2:
        bench_var = float(bench_ret.var())
        bench_std = float(bench_ret.std())
        port_std = float(port_ret.std())
        if bench_var and math.isfinite(bench_var):
            beta = _finite(port_ret.cov(bench_ret) / bench_var)
        else:
            beta = 0.0  # flat benchmark: no slope is defined
        alpha_daily = port_ret - beta * bench_ret
        alpha_annualized = _finite(alpha_daily.mean() * 252)
        correlation = _finite(port_ret.corr(bench_ret)) if bench_std and port_std else 0.0
        active = port_ret - bench_ret
        active_std = float(active.std())
        tracking_error = _finite(active_std * math.sqrt(252))
        if active_std:
            information_ratio = _finite(active.mean() / active_std * math.sqrt(252))
        else:
            information_ratio = 0.0
        # Sortino: downside deviation from negative daily returns only.
        downside = math.sqrt(float((port_ret.clip(upper=0) ** 2).mean()))
        mean_daily = float(port_ret.mean())
        if downside and math.isfinite(downside):
            sortino = _finite(mean_daily * 252 / (downside * math.sqrt(252)))
        else:
            sortino = 0.0
    else:
        beta = alpha_annualized = correlation = 0.0
        tracking_error = information_ratio = sortino = 0.0

    # --- Calmar ---------------------------------------------------------------
    cagr = _finite(port_stats.get("cagr"), 0.0)
    max_dd = _finite(port_stats.get("max_drawdown"), 0.0)
    calmar = _finite(cagr / abs(max_dd)) if max_dd != 0 else 0.0

    # --- Turnover / costs / holdings ------------------------------------------
    if rebalances:
        turnovers = [_finite(r.get("turnover"), 0.0) for r in rebalances]
        avg_turnover = _finite(sum(turnovers) / len(turnovers))
        total_cost = _finite(sum(_finite(r.get("cost"), 0.0) for r in rebalances))
        counts = [len(r.get("holdings") or []) for r in rebalances]
        avg_holdings = _finite(sum(counts) / len(counts))
        min_holdings = int(min(counts))
        max_holdings = int(max(counts))
    else:
        avg_turnover = 0.0
        total_cost = 0.0
        avg_holdings = min_holdings = max_holdings = None

    return {
        "monthly_returns": monthly,
        "annual_returns": annual,
        "best_month": best_month,
        "worst_month": worst_month,
        "positive_months_pct": positive_months_pct,
        "drawdowns": drawdowns,
        "beta": beta,
        "alpha_annualized": alpha_annualized,
        "correlation": correlation,
        "tracking_error": tracking_error,
        "information_ratio": information_ratio,
        "sortino": sortino,
        "calmar": calmar,
        "avg_turnover": avg_turnover,
        "total_cost": total_cost,
        "avg_holdings": avg_holdings,
        "min_holdings": min_holdings,
        "max_holdings": max_holdings,
    }


def _annual_returns(dates, port, bench):
    """Year-end to year-end returns; the first year starts at the first
    curve value, later years start at the previous year's end value."""
    out = []
    port_years = {}
    bench_years = {}
    for d, v in zip(dates, port):
        port_years.setdefault(d.year, []).append(v)
    for d, v in zip(dates, bench):
        bench_years.setdefault(d.year, []).append(v)
    prev_port_end = prev_bench_end = None
    for year in sorted(port_years):
        vals = port_years[year]
        b_vals = bench_years.get(year, [])
        p_start = vals[0] if prev_port_end is None else prev_port_end
        b_start = b_vals[0] if prev_bench_end is None else prev_bench_end
        out.append({
            "year": int(year),
            "portfolio": _ret(p_start, vals[-1]),
            "benchmark": _ret(b_start, b_vals[-1]) if b_vals else 0.0,
        })
        prev_port_end = vals[-1]
        prev_bench_end = b_vals[-1] if b_vals else prev_bench_end
    return out


def _drawdown_episodes(dates, values):
    """Worst 5 drawdown episodes, each from a running peak through its
    trough to the first date equity regains the peak (None if never)."""
    episodes = []
    if len(values) < 2:
        return episodes
    peak_val = values[0]
    peak_date = dates[0]
    in_dd = False
    dd_start = dd_trough_val = dd_trough_date = None
    for d, v in zip(dates, values):
        if v >= peak_val:
            if in_dd:
                episodes.append({
                    "start": dd_start.isoformat(),
                    "trough": dd_trough_date.isoformat(),
                    "recovery": d.isoformat(),
                    "depth": _finite(dd_trough_val / peak_val - 1.0),
                    "duration_days": (dd_trough_date - dd_start).days,
                })
                in_dd = False
            peak_val = v
            peak_date = d
        elif v < peak_val:
            if not in_dd:
                in_dd = True
                dd_start = peak_date
                dd_trough_val = v
                dd_trough_date = d
            elif v < dd_trough_val:
                dd_trough_val = v
                dd_trough_date = d
    if in_dd:
        episodes.append({
            "start": dd_start.isoformat(),
            "trough": dd_trough_date.isoformat(),
            "recovery": None,
            "depth": _finite(dd_trough_val / peak_val - 1.0),
            "duration_days": (dd_trough_date - dd_start).days,
        })
    episodes.sort(key=lambda e: e["depth"])
    return episodes[:5]


# ---------------------------------------------------------------------------
# Self-test (synthetic curves, hand-computed answers)
# ---------------------------------------------------------------------------
def _make_results(port_values, bench_values, dates, stats_port=None, rebalances=None):
    curve = [{"date": d.isoformat(), "portfolio": p, "benchmark": b}
             for d, p, b in zip(dates, port_values, bench_values)]
    return {
        "config": {"top_n": 10, "weighting": "score", "rebalance": "monthly",
                   "cost_bps": 0.0, "initial_capital": 100000.0,
                   "start": dates[0].isoformat(), "end": dates[-1].isoformat()},
        "stats": {
            "portfolio": {"total_return": 0.0, "cagr": 0.0, "ann_vol": 0.0,
                          "sharpe_rf0": 0.0, "max_drawdown": 0.0,
                          "start_value": port_values[0], "end_value": port_values[-1],
                          **(stats_port or {})},
            "benchmark": {"total_return": 0.0, "cagr": 0.0, "ann_vol": 0.0,
                          "sharpe_rf0": 0.0, "max_drawdown": 0.0,
                          "start_value": bench_values[0], "end_value": bench_values[-1]},
            "excess_return": 0.0,
        },
        "equity_curve": curve,
        "rebalances": rebalances or [],
        "warnings": [],
    }


def run_self_test():
    """Verify analyze_results on synthetic curves with hand-computed answers."""
    # (1) Portfolio doubles in one month, then is flat.
    days = pd.bdate_range("2025-01-02", "2025-02-28")
    port = [100.0 if d <= pd.Timestamp("2025-01-15") else 200.0 for d in days]
    bench = [100.0] * len(days)
    res = _make_results(port, bench, list(days.date))
    out = analyze_results(res)
    by_month = {m["month"]: m for m in out["monthly_returns"]}
    assert abs(by_month["2025-01"]["portfolio"] - 1.0) < 1e-12, by_month["2025-01"]
    assert abs(by_month["2025-02"]["portfolio"] - 0.0) < 1e-12, by_month["2025-02"]
    assert out["annual_returns"] == [{"year": 2025, "portfolio": 1.0, "benchmark": 0.0}], out["annual_returns"]
    assert out["best_month"] == {"month": "2025-01", "portfolio": 1.0}, out["best_month"]
    assert out["worst_month"] == {"month": "2025-02", "portfolio": 0.0}, out["worst_month"]
    print(f"PASS (1) double-in-a-month: Jan {by_month['2025-01']['portfolio']:+.0%}, "
          f"Feb {by_month['2025-02']['portfolio']:+.0%}, annual 2025 "
          f"{out['annual_returns'][0]['portfolio']:+.0%}")

    # (2) Known drawdown: peak 100 -> trough 90 -> back to 100.
    days2 = pd.bdate_range("2025-01-02", "2025-01-07")
    res2 = _make_results([100.0, 100.0, 90.0, 100.0], [100.0] * 4, list(days2.date))
    out2 = analyze_results(res2)
    assert len(out2["drawdowns"]) == 1, out2["drawdowns"]
    dd = out2["drawdowns"][0]
    assert (dd["start"], dd["trough"], dd["recovery"], dd["duration_days"]) == \
        ("2025-01-03", "2025-01-06", "2025-01-07", 3), dd
    assert abs(dd["depth"] + 0.10) < 1e-12, dd  # 90/100 - 1 (float, unrounded)
    assert out2["best_month"] is None and out2["worst_month"] is None  # no full month
    print(f"PASS (2) drawdown: depth {dd['depth']:+.0%}, start {dd['start']}, "
          f"trough {dd['trough']}, recovery {dd['recovery']}, duration {dd['duration_days']}d")

    # (2b) Unrecovered drawdown: peak 100 -> 90, never regains it.
    days2b = pd.bdate_range("2025-01-02", "2025-01-06")
    res2b = _make_results([100.0, 95.0, 90.0], [100.0] * 3, list(days2b.date))
    out2b = analyze_results(res2b)
    dd2 = out2b["drawdowns"][0]
    assert dd2["recovery"] is None and abs(dd2["depth"] + 0.10) < 1e-12, dd2
    print(f"PASS (2b) unrecovered drawdown: depth {dd2['depth']:+.0%}, recovery {dd2['recovery']}")

    # (3) Portfolio == benchmark exactly.
    days3 = pd.bdate_range("2025-01-02", "2025-01-08")
    vals3 = [100.0, 101.0, 102.0, 103.0, 104.0]
    res3 = _make_results(vals3, vals3, list(days3.date))
    out3 = analyze_results(res3)
    assert abs(out3["beta"] - 1.0) < 1e-9, out3["beta"]
    assert abs(out3["alpha_annualized"] - 0.0) < 1e-9, out3["alpha_annualized"]
    assert abs(out3["correlation"] - 1.0) < 1e-9, out3["correlation"]
    assert abs(out3["tracking_error"] - 0.0) < 1e-9, out3["tracking_error"]
    assert abs(out3["information_ratio"] - 0.0) < 1e-9, out3["information_ratio"]
    print(f"PASS (3) portfolio == benchmark: beta {out3['beta']:.2f}, "
          f"alpha {out3['alpha_annualized']:.2f}, corr {out3['correlation']:.2f}, "
          f"TE {out3['tracking_error']:.4f}, IR {out3['information_ratio']:.2f}")

    # (4) Flat portfolio: sortino 0.0 and calmar 0.0.
    days4 = pd.bdate_range("2025-01-02", "2025-01-15")
    res4 = _make_results([100.0] * len(days4), [100.0] * len(days4), list(days4.date))
    out4 = analyze_results(res4)
    assert out4["sortino"] == 0.0, out4["sortino"]
    assert out4["calmar"] == 0.0, out4["calmar"]
    assert out4["beta"] == 0.0 and out4["correlation"] == 0.0  # flat benchmark
    print(f"PASS (4) flat portfolio: sortino {out4['sortino']:.2f}, "
          f"calmar {out4['calmar']:.2f}, beta {out4['beta']:.2f}")

    # (5) Empty curve: everything degrades gracefully, no NaN/inf.
    out5 = analyze_results({"config": {}, "stats": {}, "equity_curve": [],
                            "rebalances": [], "warnings": []})
    assert out5["monthly_returns"] == [] and out5["annual_returns"] == []
    assert out5["best_month"] is None and out5["worst_month"] is None
    assert out5["positive_months_pct"] is None and out5["drawdowns"] == []
    assert out5["avg_turnover"] == 0.0 and out5["total_cost"] == 0.0
    assert out5["avg_holdings"] is None and out5["min_holdings"] is None
    assert all(math.isfinite(v) for v in [out5["beta"], out5["alpha_annualized"],
                                           out5["correlation"], out5["tracking_error"],
                                           out5["information_ratio"], out5["sortino"],
                                           out5["calmar"]])
    print("PASS (5) empty curve: all stats degrade to 0.0/None, no NaN/inf")

    # (6) Turnover / cost / holdings from the rebalance log.
    reb = [{"date": "2025-01-02", "snapshot_date": "2025-01-02",
            "holdings": [{"ticker": "A", "weight": 0.6, "shares": 1, "price": 1},
                         {"ticker": "B", "weight": 0.4, "shares": 1, "price": 1}],
            "turnover": 1.0, "cost": 100.0},
           {"date": "2025-02-03", "snapshot_date": "2025-02-03",
            "holdings": [{"ticker": "A", "weight": 1.0, "shares": 1, "price": 1}],
            "turnover": 0.2, "cost": 20.0}]
    res6 = _make_results(vals3, vals3, list(days3.date), rebalances=reb)
    out6 = analyze_results(res6)
    assert abs(out6["avg_turnover"] - 0.6) < 1e-12, out6["avg_turnover"]
    assert abs(out6["total_cost"] - 120.0) < 1e-12, out6["total_cost"]
    assert abs(out6["avg_holdings"] - 1.5) < 1e-12, out6["avg_holdings"]
    assert out6["min_holdings"] == 1 and out6["max_holdings"] == 2
    print(f"PASS (6) rebalances: avg turnover {out6['avg_turnover']:.2f}, "
          f"total cost {out6['total_cost']:.0f}, holdings avg/min/max "
          f"{out6['avg_holdings']}/{out6['min_holdings']}/{out6['max_holdings']}")
    print("ALL ANALYSIS SELF-TESTS PASSED")


if __name__ == "__main__":
    run_self_test()
