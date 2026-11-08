"""Point-in-time ranking snapshots: the raw material for honest backtests.

A backtest is only valid if it uses the ranking *as it stood on the
rebalance date* -- not today's ranking replayed over past prices (that is
look-ahead bias: it trades on information that did not exist yet).

stock_stats only keeps the latest evaluation per ticker, so history would
otherwise be lost every time Stage 2 runs. This module saves each run's
ranked set into ranking_snapshots (one row per ticker per run date).
repeatable.py calls snapshot_rankings() after every recompute_rankings(),
so history accumulates automatically from the day this ships. A backtest
needs at least two snapshot dates before it can run -- see backtest.py.

Stdlib + the pipeline's db.py only (no pandas), so repeatable.py and the
dashboard can both use it.
"""

import math
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    from dotenv import load_dotenv  # noqa: E402

    load_dotenv(ROOT / "info.env")
except ImportError:
    pass  # dotenv is optional: env vars may already be set (see app.py)

from sqlalchemy import text  # noqa: E402

METRIC_COLS = ["pe_ratio", "pb_ratio", "p_dcf", "debt_to_equity",
               "reinvestment_efficiency", "roe"]


SNAPSHOT_FIELDS = ["snapshot_date", "ticker", "composite_score", "overall_rank",
                   "sector", "industry", "pe_ratio", "pb_ratio", "p_dcf",
                   "debt_to_equity", "reinvestment_efficiency", "roe",
                   "data_flags", "source"]


def write_snapshot(engine, snapshot_date, rows, source="live"):
    """Write one snapshot (a list of row dicts) to ranking_snapshots.

    `rows` carry ticker, composite_score, overall_rank, sector, industry,
    the six metrics and data_flags. Writing the same date again overwrites
    it instead of duplicating it. `source` is 'live' (saved by a Stage 2
    run) or 'backfill' (reconstructed history, portfolio/history.py).
    Returns the number of rows written.
    """
    if not rows:
        return 0
    out = []
    for r in rows:
        # Defense in depth: a NaN in any float (e.g. a missing metric that
        # survived a pandas round-trip as NaN instead of None) makes PyMySQL
        # refuse the whole INSERT ("nan can not be used with MySQL"). A
        # non-finite float is a missing value here, so store NULL.
        row = {f: (None if isinstance(r.get(f), float) and not math.isfinite(r.get(f))
                   else r.get(f)) for f in SNAPSHOT_FIELDS}
        row["snapshot_date"] = snapshot_date
        row["source"] = source
        row["data_flags"] = row["data_flags"] or ""
        out.append(row)
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM ranking_snapshots WHERE snapshot_date = :d"),
                     {"d": snapshot_date})
        conn.execute(
            text(f"INSERT INTO ranking_snapshots ({', '.join(SNAPSHOT_FIELDS)}) "
                 f"VALUES ({', '.join(':' + c for c in SNAPSHOT_FIELDS)})"),
            out,
        )
    return len(out)


def snapshot_rankings(engine, ranked):
    """Save one snapshot (today's ranked set) to ranking_snapshots.

    `ranked` is the DataFrame returned by repeatable.py's recompute_rankings()
    (or any object with .empty / .itertuples() carrying ticker, sector,
    industry, the six metrics, data_flags, composite_score, overall_rank).
    Returns the number of rows written.
    """
    if ranked is None or (hasattr(ranked, "empty") and ranked.empty):
        return 0
    rows = []
    for r in ranked.itertuples():
        get = lambda name, default=None: getattr(r, name, default)  # noqa: E731
        rows.append({
            "ticker": get("ticker"),
            "composite_score": float(get("composite_score")) if get("composite_score") is not None else None,
            "overall_rank": int(get("overall_rank")) if get("overall_rank") is not None else None,
            "sector": get("sector"),
            "industry": get("industry"),
            "pe_ratio": get("pe_ratio"),
            "pb_ratio": get("pb_ratio"),
            "p_dcf": get("p_dcf"),
            "debt_to_equity": get("debt_to_equity"),
            "reinvestment_efficiency": get("reinvestment_efficiency"),
            "roe": get("roe"),
            "data_flags": get("data_flags") or "",
        })
    return write_snapshot(engine, date.today().isoformat(), rows, source="live")


def load_snapshots(engine):
    """All snapshots as {snapshot_date (str): [rows by overall_rank]}.

    Returns ({}, error) -- error is a message string when the table does
    not exist yet (no snapshots have ever been saved), else None.
    """
    from sqlalchemy import inspect

    if "ranking_snapshots" not in inspect(engine).get_table_names():
        return {}, "No ranking snapshots yet: ranking_snapshots does not exist."
    cols = [c["name"] for c in inspect(engine).get_columns("ranking_snapshots")]
    # 'source' only exists on tables upgraded after the backfill shipped;
    # older tables are all live snapshots.
    fields = ["snapshot_date", "ticker", "composite_score", "overall_rank",
              "sector", "industry", "pe_ratio", "pb_ratio", "p_dcf",
              "debt_to_equity", "reinvestment_efficiency", "roe", "data_flags"]
    if "source" in cols:
        fields.append("source")
    sql = text(f"SELECT {', '.join(fields)} FROM ranking_snapshots "
               "ORDER BY snapshot_date, overall_rank")
    with engine.connect() as conn:
        rows = [dict(r) for r in conn.execute(sql).mappings()]
    for r in rows:
        r.setdefault("source", "live")
    out = {}
    for r in rows:
        out.setdefault(str(r["snapshot_date"]), []).append(r)
    if not out:
        return {}, ("ranking_snapshots is empty. Snapshots are saved automatically "
                    "each time Stage 2 runs -- run it, then run it again on a later "
                    "date, before backtesting.")
    return out, None
