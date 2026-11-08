"""Top-N portfolio construction for the valuproj value strategy.

Takes the pipeline's ranked shortlist (stock_stats, as written by
repeatable.py) and turns the top N into a portfolio.

RUN IN VS CODE: edit the constants below, then press Run. No terminal
flags needed; the CLI flags in main() only default to the constants,
for automated testing.

Weighting (WEIGHTING):
  "score" -- weight ∝ (1 - composite_score), so the best-ranked stocks
             get the most capital. This is the default: the portfolio is
             built "based on a weighted average of their scores".
  "equal" -- 1/N each.
  "rank"  -- weight ∝ (N + 1 - rank), a linear rank weight.

Only fresh, ranked rows are eligible (evaluated in the last 45 days with
a composite score, exactly the set repeatable.py ranks). The portfolio
score reported is the weight-weighted average composite score.

This module is stdlib + the pipeline's db.py only, so the dashboard can
import it without pulling in yfinance/pandas. The backtest lives in
backtest.py.
"""

import json
import sys
from datetime import date, timedelta
from pathlib import Path

# ---------------------------------------------------------------------------
# RUN IN VS CODE: edit these, then press Run.
# ---------------------------------------------------------------------------
TOP_N = 10
WEIGHTING = "score"  # "score" | "equal" | "rank"
STALE_DAYS = 45  # matches MAX_STATS_AGE_DAYS in repeatable.py

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    from dotenv import load_dotenv  # noqa: E402

    load_dotenv(ROOT / "info.env")
except ImportError:
    pass  # dotenv is optional: env vars may already be set (see app.py)

import db  # noqa: E402
from sqlalchemy import text  # noqa: E402


def load_ranked(engine, top_n=TOP_N):
    """Top-N ranked tickers from stock_stats, fresh only. Returns rows."""
    cutoff = (date.today() - timedelta(days=STALE_DAYS)).isoformat()
    sql = text("""
        SELECT ticker, `date`, sector, industry, overall_rank, composite_score,
               pe_ratio, pb_ratio, p_dcf, debt_to_equity,
               reinvestment_efficiency, roe, data_flags
        FROM stock_stats
        WHERE composite_score IS NOT NULL AND overall_rank IS NOT NULL
          AND `date` >= :cutoff
        ORDER BY overall_rank ASC
        LIMIT :n
    """)
    with engine.connect() as conn:
        rows = [dict(r) for r in conn.execute(sql, {"cutoff": cutoff, "n": top_n}).mappings()]
    return rows


def compute_weights(holdings, weighting=WEIGHTING):
    """Weights for the holdings, normalised to sum to 1. Returns {ticker: weight}."""
    n = len(holdings)
    if n == 0:
        return {}
    if weighting == "equal":
        raw = {h["ticker"]: 1.0 for h in holdings}
    elif weighting == "rank":
        raw = {h["ticker"]: float(n + 1 - (h["overall_rank"] or n)) for h in holdings}
    else:  # "score": weight ∝ (1 - composite_score); best score -> most capital
        raw = {h["ticker"]: max(1.0 - float(h["composite_score"] or 1.0), 0.0) for h in holdings}
    total = sum(raw.values())
    if total <= 0:  # every score was 1.0 (or worse) -- fall back to equal weight
        raw = {h["ticker"]: 1.0 for h in holdings}
        total = float(n)
    return {t: w / total for t, w in raw.items()}


def build_portfolio(engine=None, top_n=TOP_N, weighting=WEIGHTING):
    """Build the target portfolio from the current ranked shortlist."""
    if engine is None:
        engine = db.get_engine()
    holdings = load_ranked(engine, top_n=top_n)
    weights = compute_weights(holdings, weighting)
    for h in holdings:
        h["weight"] = weights.get(h["ticker"], 0.0)
    portfolio_score = (sum(h["weight"] * float(h["composite_score"]) for h in holdings)
                       if holdings else None)
    warning = None
    if not holdings:
        warning = "No ranked, fresh rows in stock_stats -- run Stage 2 first."
    elif len(holdings) < top_n:
        warning = (f"Only {len(holdings)} ranked tickers available "
                   f"(asked for top {top_n}).")
    return {
        "as_of": date.today().isoformat(),
        "top_n": top_n,
        "weighting": weighting,
        "count": len(holdings),
        "portfolio_score": portfolio_score,
        "holdings": holdings,
        "warning": warning,
    }


def main():
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--top-n", type=int, default=TOP_N)
    parser.add_argument("--weighting", choices=["score", "equal", "rank"], default=WEIGHTING)
    args = parser.parse_args()
    pf = build_portfolio(top_n=args.top_n, weighting=args.weighting)
    if pf["warning"]:
        print(f"Warning: {pf['warning']}")
    if not pf["holdings"]:
        return
    print(f"\nTop {pf['count']} portfolio ({pf['weighting']}-weighted, as of {pf['as_of']})")
    print(f"{'Rank':>4}  {'Ticker':<8} {'Score':>6}  {'Weight':>7}  Sector")
    for h in pf["holdings"]:
        print(f"{h['overall_rank']:>4}  {h['ticker']:<8} {h['composite_score']:>6.3f}  "
              f"{100 * h['weight']:>6.2f}%  {h.get('sector') or '-'}")
    print(f"\nPortfolio score (weight-weighted average composite): "
          f"{pf['portfolio_score']:.3f} (lower is better)")
    out = Path(__file__).resolve().parent / "portfolio_latest.json"
    out.write_text(json.dumps(pf, indent=2, default=str))
    print(f"Saved to {out}")


if __name__ == "__main__":
    main()
