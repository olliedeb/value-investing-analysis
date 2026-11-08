"""Local front-end for the valuproj value-investing pipeline.

RUN IN VS CODE: open this file and press Run. No terminal flags.
It serves a dashboard at http://127.0.0.1:8123 (or the PORT below)
that reads the pipeline's own databases and shows the ranked shortlist,
every metric, every data flag in plain English, and the qualitative
(Agent_Wardrobe) scores -- plus a place to jot down anything that looks
wrong, per ticker.

It never writes rows to the pipeline databases: the only thing it writes
is review notes, to frontend/review_notes.json next to this file. The one
exception is schema: on start it runs the pipeline's own db.ensure_schema(),
which creates the tables if they are missing and adds any columns an older
stock_stats table lacks (sector, fiscal_year_end, roe, data_flags,
composite_score, overall_rank) -- exactly what initial_screen.py and
repeatable.py already do on every run. Without that, an older table makes
the dashboard fail with "Unknown column 's.sector'".

The Pipeline panel can also *run* the pipeline: it launches the real
initial_screen.py / repeatable.py scripts as subprocesses (same Python,
same info.env, same working directory as running them in VS Code), one
run at a time, and streams their log back to the page while the table
refreshes live from the database as tickers finish.

The Rank and Score columns are recomputed live in this server from the
ranking modes the user picks in the dashboard (POST /api/ranking-modes,
saved to ranking_modes.json): P/DCF and reinvestment efficiency always
use absolute values, the other four metrics can use absolute values or
their peer z-scores. The computation mirrors repeatable.py's
recompute_rankings() -- same freshness and minimum-metrics rules, same
percentile method -- with the one difference that a metric in "relative"
mode is percentiled over its z-scores instead of its absolute values.
The stored Stage 2 score/rank are still shown for comparison.

Data sources (same ones the pipeline already uses):
  * MySQL/MariaDB via ../db.py + info.env  -> queue + stock_stats
  * ../Agent_Wardrobe/qualitative_agent.db  -> qualitative_scores, macro_scores
If a source is missing or unreachable, the dashboard says so on screen
instead of crashing -- check the banner at the top of the page.
"""

# --- RUN IN VS CODE (edit these constants, then press Run) -------------------
PORT = 8123
HOST = "127.0.0.1"          # localhost only; nothing leaves this machine
OPEN_BROWSER = True         # open the dashboard in your browser on start
# -----------------------------------------------------------------------------

import json
import math
import os
import re
import sqlite3
import statistics
import subprocess
import sys
import threading
import time
import uuid
import webbrowser
from collections import deque
from datetime import date, datetime
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

ROOT = Path(__file__).resolve().parent.parent          # /Users/.../valuproj
FRONTEND_DIR = Path(__file__).resolve().parent
STATIC_DIR = FRONTEND_DIR / "static"
NOTES_PATH = FRONTEND_DIR / "review_notes.json"
QUAL_DB_PATH = ROOT / "Agent_Wardrobe" / "qualitative_agent.db"

sys.path.insert(0, str(ROOT))
try:
    from dotenv import load_dotenv

    load_dotenv(ROOT / "info.env")
except Exception:
    pass

DB_IMPORT_ERROR = None
try:
    from db import ensure_schema, get_engine  # noqa: E402  (needs the path + env setup above)
except Exception as exc:  # noqa: BLE001 - surface it in the UI, don't crash
    ensure_schema = None
    get_engine = None
    DB_IMPORT_ERROR = f"{type(exc).__name__}: {exc}"

PORTFOLIO_IMPORT_ERROR = None
try:
    from portfolio import portfolio as portfolio_mod  # noqa: E402
except Exception as exc:  # noqa: BLE001
    portfolio_mod = None
    PORTFOLIO_IMPORT_ERROR = f"{type(exc).__name__}: {exc}"

METRIC_COLS = ["pe_ratio", "pb_ratio", "p_dcf", "debt_to_equity",
               "reinvestment_efficiency", "roe"]
LOWER_IS_BETTER = ["pe_ratio", "pb_ratio", "p_dcf", "debt_to_equity"]
MIN_PEER_N = 5  # need at least this many peers in a group for a z-score against it

# Ranking modes: which value goes into the composite score for each metric.
# P/DCF and reinvestment efficiency are always absolute -- only the absolute
# number matters for those. The other four can use absolute values or their
# peer z-score. Modes are chosen in the dashboard and saved to
# ranking_modes.json next to this file.
RANK_SWITCHABLE = ["pe_ratio", "pb_ratio", "debt_to_equity", "roe"]
RANK_LOCKED_ABSOLUTE = ["p_dcf", "reinvestment_efficiency"]
MODES_PATH = FRONTEND_DIR / "ranking_modes.json"
MIN_METRICS_TO_RANK = 4  # mirrors repeatable.py
METRIC_LABELS = {
    "pe_ratio": "P/E", "pb_ratio": "P/B", "p_dcf": "P/DCF",
    "debt_to_equity": "Debt/Equity",
    "reinvestment_efficiency": "Reinvestment efficiency", "roe": "ROE",
}

# Plain-English meanings for every flag repeatable.py can write, from README.md.
FLAG_MEANINGS = {
    "neg_equity": "Negative book equity -- book-based ratios are meaningless, excluded from ranking.",
    "no_us_gaap": "No US-GAAP facts (typically a foreign filer on IFRS) -- most metrics missing.",
    "no_sec_cik": "Ticker not found in the SEC ticker map.",
    "no_recent_10k": "Latest annual filing is more than ~600 days old -- stale or delisted.",
    "sec_fetch_failed": "SEC request errored -- try re-running stage 2 for this ticker.",
    "fin_sector_skip": "Financial / Real Estate: DCF and debt/equity skipped on purpose.",
    "dcf_insufficient_fcf": "Fewer than 4 years of usable free cash flow for a DCF.",
    "dcf_nonpositive_fcf": "Normalised free cash flow is zero or negative.",
    "dcf_no_debt_data": "Company pays interest but reports no debt tag.",
    "dcf_nonpositive_equity": "DCF equity value is zero or negative.",
    "pdcf_out_of_range": "P/DCF outside 0.01-100 -- discarded as a likely data error. Worth checking.",
    "no_market_cap": "Yahoo returned no market cap.",
    "re_missing_data": "Retained earnings unavailable for the start or end of the window.",
    "re_not_increasing": "Retained earnings did not grow over the window.",
    "re_no_market_cap": "Historical market cap unavailable (including skipped due to a stock split).",
    "re_change_immaterial": "Retained-earnings change too small for a meaningful ratio.",
    "de_no_debt_data": "Debt/equity not computable.",
    "yf_info_failed": "Yahoo info lookup failed for this ticker.",
    "roe_failed": "ROE calculation errored.",
    "re_check_failed": "Reinvestment-efficiency calculation errored.",
    "de_failed": "Debt/equity calculation errored.",
    "dcf_failed": "DCF calculation errored.",
}

STALE_DAYS = 45  # matches MAX_STATS_AGE_DAYS in repeatable.py


# ---------------------------------------------------------------------------
# Data access
# ---------------------------------------------------------------------------
def _jsonable(value):
    if value is None:
        return None
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        # PyMySQL returns Decimal for MySQL numeric columns (e.g. the
        # stock_stats metric columns). json.dumps cannot serialise Decimal,
        # and one such value used to kill /api/overview with
        # "TypeError: Object of type Decimal is not JSON serializable",
        # leaving the dashboard blank with no error shown.
        f = float(value)
        return f if math.isfinite(f) else None
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return value


def _json_default(obj):
    """Fallback for json.dumps so one unexpected value can never kill an
    API response the way a stray Decimal did on 2026-11-06."""
    return _jsonable(obj) if isinstance(obj, (Decimal, datetime, date, bytes)) else str(obj)


def _short_error(exc):
    """One-line database error, without the full SQL statement attached.

    SQLAlchemy's str(exc) dumps the whole query into the message, which is
    what filled the dashboard banner in the first version of this file.
    """
    orig = getattr(exc, "orig", None)
    text = str(orig or exc).splitlines()
    first = text[0].strip() if text else type(exc).__name__
    return first[:300]


def compute_zscores(rows):
    """Peer-relative z-scores, stored on each row as
    row["zscores"][metric] = {z, scope, n, peer_mean, peer_std}, plus
    row["peer_z_avg"] (mean of the ticker's available z-scores).

    P/DCF and reinvestment efficiency never get a z-score: only their
    absolute value matters, so they are not shown peer-relative anywhere
    and do not count toward peer_z_avg.

    Scope, narrowest first: the ticker's *industry* when at least MIN_PEER_N
    peers in that industry have the metric, else its *sector*, else all
    stocks. Positive z always means better than peers: for lower-is-better
    metrics (P/E, P/B, P/DCF, debt/equity) z = (peer mean - value) / peer
    std, so a cheaper-than-peers stock scores positive; for higher-is-better
    metrics (reinvestment efficiency, ROE) z = (value - peer mean) / peer
    std. The ticker itself is included in its peer group, as usual for a
    z-score against a peer set.
    Peer stats prefer rows evaluated in the last STALE_DAYS days (the same
    freshness the ranking uses); if too few fresh rows have the metric at
    all, all rows with the metric are used instead.
    """
    for row in rows:
        row["zscores"] = {}
        row["peer_z_avg"] = None
    for col in METRIC_COLS:
        if col in RANK_LOCKED_ABSOLUTE:
            continue  # absolute only -- no z-score for this metric
        candidates = [r for r in rows if r.get(col) is not None]
        if not candidates:
            continue
        fresh = [r for r in candidates
                 if r.get("age_days") is not None and r["age_days"] <= STALE_DAYS]
        pool = fresh if len(fresh) >= MIN_PEER_N else candidates
        if len(pool) < 2:
            continue
        by_industry, by_sector = {}, {}
        for r in pool:
            if r.get("industry"):
                by_industry.setdefault(r["industry"], []).append(r[col])
            if r.get("sector"):
                by_sector.setdefault(r["sector"], []).append(r[col])
        all_vals = [r[col] for r in pool]

        def group_stats(vals):
            if len(vals) < 2:
                return None
            mean = statistics.mean(vals)
            std = statistics.stdev(vals)
            return mean, std

        global_stats = group_stats(all_vals)
        for r in candidates:
            chosen = None  # (scope, mean, std, n)
            ind = by_industry.get(r.get("industry") or "")
            if ind and len(ind) >= MIN_PEER_N:
                s = group_stats(ind)
                if s:
                    chosen = ("industry", s[0], s[1], len(ind))
            if chosen is None:
                sec = by_sector.get(r.get("sector") or "")
                if sec and len(sec) >= MIN_PEER_N:
                    s = group_stats(sec)
                    if s:
                        chosen = ("sector", s[0], s[1], len(sec))
            if chosen is None and global_stats:
                chosen = ("all", global_stats[0], global_stats[1], len(all_vals))
            if chosen is None:
                continue
            scope, mean, std, n = chosen
            if std == 0:
                z = 0.0
            elif col in LOWER_IS_BETTER:
                z = (mean - r[col]) / std
            else:
                z = (r[col] - mean) / std
            r["zscores"][col] = {"z": z, "scope": scope, "n": n,
                                  "peer_mean": mean, "peer_std": std}
    for row in rows:
        zs = [v["z"] for v in row["zscores"].values()]
        row["peer_z_avg"] = sum(zs) / len(zs) if zs else None


def load_modes():
    """Per-metric ranking modes, defaulting to absolute for everything."""
    modes = {c: "absolute" for c in METRIC_COLS}
    try:
        saved = json.loads(MODES_PATH.read_text())
    except (OSError, ValueError):
        return modes
    if isinstance(saved, dict):
        for col in RANK_SWITCHABLE:
            if saved.get(col) in ("absolute", "relative"):
                modes[col] = saved[col]
    # P/DCF and reinvestment efficiency are always absolute, whatever is saved.
    return modes


def save_modes(modes):
    """Persist modes, coercing locked metrics (and anything invalid) to absolute."""
    clean = {c: ("relative" if modes.get(c) == "relative" and c in RANK_SWITCHABLE
                 else "absolute") for c in METRIC_COLS}
    tmp = MODES_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(clean, indent=2))
    os.replace(tmp, MODES_PATH)
    return clean


def _percentile_ranks(items, ascending=True):
    """Pandas rank(pct=True) semantics for a list of (key, value) pairs.

    Returns {key: percentile}, where a smaller percentile is always better:
    rank 1 (best) -> 1/N. Ties share the average rank, exactly as pandas
    does. ascending=True means the smallest value is best (lower-is-better
    metrics); ascending=False means the largest value is best (a z-score,
    where a higher z is better).
    """
    ordered = sorted(items, key=lambda kv: kv[1], reverse=not ascending)
    n = len(ordered)
    out = {}
    i = 0
    while i < n:
        j = i
        while j + 1 < n and ordered[j + 1][1] == ordered[i][1]:
            j += 1
        avg_rank = (i + 1 + j + 1) / 2.0
        for k in range(i, j + 1):
            out[ordered[k][0]] = avg_rank / n
        i = j + 1
    return out


def recompute_live_rankings(rows, modes):
    """Recompute composite score + overall rank from the chosen modes.

    Mirrors repeatable.py's recompute_rankings() (RANK_WITHIN_SECTOR is off
    there, so absolute mode is plain global percentiles): only fresh rows
    (<= STALE_DAYS) with >= MIN_METRICS_TO_RANK usable metrics and no
    neg_equity flag are ranked; missing metrics count as neutral 0.5; the
    composite is the mean of the six per-metric percentiles and rank 1 is
    best (ties share the lowest rank, pandas rank(method="min")).
    The one difference is per-metric: in "relative" mode the percentile is
    taken over the tickers' peer z-scores instead of their absolute values.
    The stored Stage 2 score/rank are kept as stored_composite_score /
    stored_overall_rank for comparison; composite_score / overall_rank /
    is_ranked on each row become the live values.
    """
    for row in rows:
        row["stored_composite_score"] = row.get("composite_score")
        row["stored_overall_rank"] = row.get("overall_rank")
        row["composite_score"] = None
        row["overall_rank"] = None
        row["is_ranked"] = False

    def usable(row, col):
        if modes.get(col) == "relative":
            return row["zscores"].get(col) is not None
        return row.get(col) is not None

    rankable = [r for r in rows
                if r.get("age_days") is not None and r["age_days"] <= STALE_DAYS
                and sum(usable(r, c) for c in METRIC_COLS) >= MIN_METRICS_TO_RANK
                and "neg_equity" not in r["flags"]]
    if not rankable:
        return

    pct = {}
    for col in METRIC_COLS:
        if modes.get(col) == "relative":
            items = [(r["ticker"], r["zscores"][col]["z"]) for r in rankable
                     if r["zscores"].get(col)]
            pct[col] = _percentile_ranks(items, ascending=False)
        else:
            items = [(r["ticker"], r[col]) for r in rankable if r.get(col) is not None]
            pct[col] = _percentile_ranks(items, ascending=col in LOWER_IS_BETTER)

    scored = []
    for r in rankable:
        composite = sum(pct[c].get(r["ticker"], 0.5) for c in METRIC_COLS) / len(METRIC_COLS)
        r["composite_score"] = composite
        r["is_ranked"] = True
        scored.append((composite, r["ticker"]))
    # pandas rank(method="min"): tied composites share the lowest rank.
    scored.sort()
    ranks = {}
    i = 0
    while i < len(scored):
        j = i
        while j + 1 < len(scored) and scored[j + 1][0] == scored[i][0]:
            j += 1
        for k in range(i, j + 1):
            ranks[scored[k][1]] = i + 1
        i = j + 1
    for r in rankable:
        r["overall_rank"] = ranks[r["ticker"]]


def load_quantitative():
    """All queue tickers, joined to stock_stats. Returns (rows, error)."""
    if get_engine is None:
        return [], ("Could not load the pipeline database module (db.py): "
                    f"{DB_IMPORT_ERROR}. Check that SQLAlchemy, PyMySQL and "
                    "python-dotenv are installed in the Python you run this with.")
    try:
        engine = get_engine()
    except Exception as exc:  # noqa: BLE001
        return [], (f"Could not connect to MySQL: {_short_error(exc)}. "
                    "Check MySQL/MariaDB is running and info.env has "
                    "DB_USERNAME / DB_PASSWORD / DB_NAME.")
    if ensure_schema is not None:
        try:
            ensure_schema(engine)
        except Exception as exc:  # noqa: BLE001
            return [], (f"Could not bring the tables up to date: {_short_error(exc)}. "
                        "The dashboard adds the newer stock_stats columns "
                        "(sector, fiscal_year_end, roe, data_flags, "
                        "composite_score, overall_rank) the same way "
                        "repeatable.py does.")
    sql = """
        SELECT q.Ticker AS ticker, q.Status AS queue_status,
               s.`date` AS `date`, s.sector, s.industry, s.fiscal_year_end,
               s.pe_ratio, s.pb_ratio, s.p_dcf, s.debt_to_equity,
               s.reinvestment_efficiency, s.roe, s.data_flags,
               s.composite_score, s.overall_rank
        FROM queue q LEFT JOIN stock_stats s ON s.ticker = q.Ticker
        UNION ALL
        SELECT s.ticker AS ticker, 'not_in_queue' AS queue_status,
               s.`date` AS `date`, s.sector, s.industry, s.fiscal_year_end,
               s.pe_ratio, s.pb_ratio, s.p_dcf, s.debt_to_equity,
               s.reinvestment_efficiency, s.roe, s.data_flags,
               s.composite_score, s.overall_rank
        FROM stock_stats s
        WHERE s.ticker NOT IN (SELECT Ticker FROM queue)
    """
    try:
        from sqlalchemy import text

        with engine.connect() as conn:
            result = conn.execute(text(sql))
            rows = [dict(r._mapping) for r in result]
    except Exception as exc:  # noqa: BLE001
        return [], (f"Could not read queue / stock_stats: {_short_error(exc)}. "
                    "The tables are created on the first pipeline run -- run "
                    "initial_screen.py / repeatable.py once if this is a fresh database.")
    today = date.today()
    for row in rows:
        flags = [f for f in (row.get("data_flags") or "").split(",") if f]
        row["flags"] = flags
        row["flag_meanings"] = {f: FLAG_MEANINGS.get(f, "Unknown flag -- see repeatable.py.")
                                for f in flags}
        row["metrics_present"] = sum(row.get(c) is not None for c in METRIC_COLS)
        row["missing_metrics"] = [c for c in METRIC_COLS if row.get(c) is None]
        row["is_ranked"] = row.get("overall_rank") is not None
        d = row.get("date")
        if isinstance(d, str):
            try:
                d = datetime.strptime(d, "%Y-%m-%d").date()
            except ValueError:
                d = None
        row["age_days"] = (today - d).days if isinstance(d, date) else None
        row["is_stale"] = row["age_days"] is not None and row["age_days"] > STALE_DAYS
        for key, value in list(row.items()):
            row[key] = _jsonable(value)
    compute_zscores(rows)
    recompute_live_rankings(rows, load_modes())
    return rows, None


def load_qualitative():
    """Latest management + macro score per ticker. Returns (by_ticker, error)."""
    if not QUAL_DB_PATH.exists():
        return {}, ("No qualitative scores yet: "
                    f"{QUAL_DB_PATH.name} does not exist. Run Agent_Wardrobe "
                    "(agent_loop.py) for a ticker first.")
    try:
        conn = sqlite3.connect(f"file:{QUAL_DB_PATH}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            qual = conn.execute(
                "SELECT * FROM qualitative_scores ORDER BY as_of DESC").fetchall()
            macro = conn.execute(
                "SELECT * FROM macro_scores ORDER BY as_of DESC").fetchall()
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001
        return {}, f"Could not read {QUAL_DB_PATH.name}: {type(exc).__name__}: {exc}"
    by_ticker = {}
    for r in qual:
        by_ticker.setdefault(r["ticker"], {})["management"] = dict(r)
    for r in macro:
        d = dict(r)
        for key in ("tailwinds", "headwinds"):
            try:
                d[key] = json.loads(d[key]) if isinstance(d[key], str) else d[key]
            except (TypeError, ValueError):
                pass
        by_ticker.setdefault(r["ticker"], {})["macro"] = d
    for entry in by_ticker.values():
        for score in entry.values():
            for key, value in list(score.items()):
                score[key] = _jsonable(value)
    return by_ticker, None


def load_notes():
    if not NOTES_PATH.exists():
        return {}
    try:
        data = json.loads(NOTES_PATH.read_text())
        return data if isinstance(data, dict) else {}
    except (ValueError, OSError):
        return {}


def save_notes(notes):
    tmp = NOTES_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(notes, indent=2))
    os.replace(tmp, NOTES_PATH)


# ---------------------------------------------------------------------------
# Pipeline runner: launch the real stage scripts as subprocesses, one at a
# time, and stream their log back to the page. The dashboard never re-implements
# the pipeline -- it runs initial_screen.py / repeatable.py exactly as you
# would from VS Code, with the same interpreter, working directory and
# environment (info.env is already loaded into os.environ above, so the
# child inherits DB credentials and SEC_USER_AGENT).
# ---------------------------------------------------------------------------
RUN_LOG_MAX_LINES = 500
RUN_STATE = {
    "state": "idle", "job": None, "label": None, "command": [],
    "started_at": None, "finished_at": None, "exit_code": None,
    "total": None, "processed": 0, "current_ticker": None,
    "error": None, "stop_requested": False,
}
RUN_LOG = deque(maxlen=RUN_LOG_MAX_LINES)
RUN_LOCK = threading.Lock()
RUN_PROC = {"proc": None}

_JOB_SCRIPTS = {
    "stage2": ("repeatable.py", "Stage 2 — evaluate queued tickers"),
    "rank-only": ("repeatable.py", "Recompute rankings only"),
    "stage1": ("initial_screen.py", "Stage 1 — screen for new candidates"),
}
_TICKER_LINE = re.compile(r"^([A-Z][A-Z0-9.\-]{0,19}):\s+(success|partial|failed)\b")
_TOTAL_LINE = re.compile(r"(\d+)\s+tickers to process")
_SCREEN_LINE = re.compile(r"Screen matched (\d+) tickers")


def run_status():
    with RUN_LOCK:
        status = dict(RUN_STATE)
    status["log_tail"] = list(RUN_LOG)[-100:]
    return status


def _run_reader(proc):
    """Drain the child process's output, tracking progress for the UI."""
    try:
        for raw in proc.stdout:
            line = raw.rstrip("\n")
            with RUN_LOCK:
                RUN_LOG.append(line)
                m = _TICKER_LINE.match(line)
                if m:
                    RUN_STATE["processed"] += 1
                    RUN_STATE["current_ticker"] = m.group(1)
                m = _TOTAL_LINE.search(line)
                if m:
                    RUN_STATE["total"] = int(m.group(1))
                m = _SCREEN_LINE.search(line)
                if m:
                    RUN_STATE["total"] = int(m.group(1))
    finally:
        try:
            rc = proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            rc = proc.wait()
        with RUN_LOCK:
            RUN_STATE["exit_code"] = rc
            RUN_STATE["finished_at"] = datetime.now().isoformat(timespec="seconds")
            RUN_STATE["current_ticker"] = None
            if RUN_STATE["stop_requested"]:
                RUN_STATE["state"] = "stopped"
            elif rc == 0:
                RUN_STATE["state"] = "done"
            else:
                RUN_STATE["state"] = "error"
                RUN_STATE["error"] = (f"Exited with code {rc}. "
                                      "See the log below for the last lines.")
            RUN_PROC["proc"] = None


def start_run(job, limit=25, requeue="none"):
    """Start a pipeline run. Returns (ok, message)."""
    if job not in _JOB_SCRIPTS:
        return False, f"Unknown job: {job!r}."
    script, label = _JOB_SCRIPTS[job]
    script_path = ROOT / script
    if not script_path.exists():
        return False, f"{script} not found in {ROOT}."
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        return False, "Limit must be a whole number."
    if limit < 0 or limit > 5000:
        return False, "Limit must be between 0 (all) and 5000."
    if requeue not in ("none", "failed", "all"):
        return False, f"Unknown requeue option: {requeue!r}."
    argv = [sys.executable or "python3", str(script_path)]
    if job == "stage2":
        argv += ["--limit", str(limit)]
    elif job == "rank-only":
        argv += ["--rank-only"]
    elif job == "stage1":
        argv += ["--requeue", requeue]
    with RUN_LOCK:
        if RUN_STATE["state"] == "running":
            return False, "A pipeline run is already in progress."
        RUN_STATE.update({
            "state": "running", "job": job, "label": label,
            "command": argv[1:], "started_at": datetime.now().isoformat(timespec="seconds"),
            "finished_at": None, "exit_code": None, "total": None,
            "processed": 0, "current_ticker": None, "error": None,
            "stop_requested": False,
        })
        RUN_LOG.clear()
        RUN_LOG.append("$ " + " ".join(argv[1:]))
    try:
        proc = subprocess.Popen(
            argv, cwd=str(ROOT), stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, bufsize=1,
            env={**os.environ},
        )
    except Exception as exc:  # noqa: BLE001
        with RUN_LOCK:
            RUN_STATE.update({"state": "error", "error": f"Could not start: {exc}"})
        return False, f"Could not start {script}: {exc}"
    with RUN_LOCK:
        RUN_PROC["proc"] = proc
    threading.Thread(target=_run_reader, args=(proc,), daemon=True).start()
    return True, f"Started: {label}."


def stop_run():
    """Ask the running pipeline process to stop. Returns True if one was running."""
    with RUN_LOCK:
        proc = RUN_PROC.get("proc")
        running = RUN_STATE["state"] == "running"
        if running:
            RUN_STATE["stop_requested"] = True
    if proc is not None and proc.poll() is None:
        proc.terminate()
        return True
    return running


# ---------------------------------------------------------------------------
# Backtest runner: runs portfolio/backtest.py as a subprocess (same pattern
# as the pipeline runner, separate state so the two never interfere).
# The backtest writes its results to portfolio/backtest_results.json; the
# dashboard reads that file to show the stats and equity curve.
# ---------------------------------------------------------------------------
BACKTEST_RESULTS_PATH = ROOT / "portfolio" / "backtest_results.json"
BACKTEST_STATE = {"state": "idle", "started_at": None, "finished_at": None,
                  "exit_code": None, "error": None}
BACKTEST_LOG = deque(maxlen=300)
BACKTEST_LOCK = threading.Lock()
BACKTEST_PROC = {"proc": None}


def backtest_status():
    with BACKTEST_LOCK:
        status = dict(BACKTEST_STATE)
    status["log_tail"] = list(BACKTEST_LOG)[-100:]
    results = None
    if BACKTEST_RESULTS_PATH.exists():
        try:
            results = json.loads(BACKTEST_RESULTS_PATH.read_text())
        except (OSError, ValueError):
            results = None
    status["results"] = results
    status["has_results"] = results is not None
    return status


# ---------------------------------------------------------------------------
# History backfill runner: runs portfolio/history.py as a subprocess (same
# pattern as the backtest runner). The backfill writes reconstructed
# snapshots into ranking_snapshots (source='backfill') and tracks its own
# progress in portfolio/data/history/backfill_progress.json.
# ---------------------------------------------------------------------------
BACKFILL_PROGRESS_PATH = ROOT / "portfolio" / "data" / "history" / "backfill_progress.json"
BACKFILL_STATE = {"state": "idle", "started_at": None, "finished_at": None,
                  "exit_code": None, "error": None}
BACKFILL_LOG = deque(maxlen=300)
BACKFILL_LOCK = threading.Lock()
BACKFILL_PROC = {"proc": None}


def snapshot_coverage():
    """{dates: [snapshot_date...], backfilled: n, live: n} from the DB, or {}."""
    if get_engine is None:
        return {}
    try:
        from sqlalchemy import text as _text
        engine = get_engine()
        with engine.connect() as conn:
            rows = conn.execute(_text(
                "SELECT snapshot_date, source, COUNT(*) AS n FROM ranking_snapshots "
                "GROUP BY snapshot_date, source ORDER BY snapshot_date")).mappings()
            dates, backfilled, live = set(), 0, 0
            for r in rows:
                dates.add(str(r["snapshot_date"]))
                if (r["source"] or "live") == "backfill":
                    backfilled += 1
                else:
                    live += 1
        return {"dates": sorted(dates), "backfilled_dates": backfilled, "live_dates": live}
    except Exception:  # noqa: BLE001 -- coverage is informational, never fatal
        return {}


def backfill_status():
    with BACKFILL_LOCK:
        status = dict(BACKFILL_STATE)
    status["log_tail"] = list(BACKFILL_LOG)[-100:]
    status["coverage"] = snapshot_coverage()
    return status


def _backfill_reader(proc):
    try:
        for raw in proc.stdout:
            with BACKFILL_LOCK:
                BACKFILL_LOG.append(raw.rstrip("\n"))
    finally:
        try:
            rc = proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            rc = proc.wait()
        with BACKFILL_LOCK:
            BACKFILL_STATE["exit_code"] = rc
            BACKFILL_STATE["finished_at"] = datetime.now().isoformat(timespec="seconds")
            BACKFILL_STATE["state"] = "done" if rc == 0 else "error"
            if rc != 0:
                BACKFILL_STATE["error"] = (f"Backfill exited with code {rc}. "
                                           "See the log below.")
            BACKFILL_PROC["proc"] = None


def start_backfill(start_year=2016, rebalance="quarterly", max_tickers=0, force=False):
    """Start a history backfill run. Returns (ok, message)."""
    script = ROOT / "portfolio" / "history.py"
    if not script.exists():
        return False, "portfolio/history.py not found."
    try:
        start_year = int(start_year)
        max_tickers = int(max_tickers)
    except (TypeError, ValueError):
        return False, "Start year and max tickers must be numbers."
    if start_year < 2000 or start_year > date.today().year:
        return False, f"Start year must be between 2000 and {date.today().year}."
    if rebalance not in ("monthly", "quarterly", "annual", "2-year",
                         "3-year", "5-year"):
        return False, f"Unknown rebalance frequency: {rebalance!r}."
    if max_tickers < 0:
        return False, "Max tickers must be 0 (all) or more."
    argv = [sys.executable or "python3", str(script),
            "--start-year", str(start_year), "--rebalance", rebalance,
            "--max-tickers", str(max_tickers)]
    if force:
        argv.append("--force")
    with BACKFILL_LOCK:
        if BACKFILL_STATE["state"] == "running":
            return False, "A backfill is already running."
        BACKFILL_STATE.update({
            "state": "running",
            "started_at": datetime.now().isoformat(timespec="seconds"),
            "finished_at": None, "exit_code": None, "error": None,
        })
        BACKFILL_LOG.clear()
        BACKFILL_LOG.append("$ " + " ".join(argv[1:]))
    try:
        proc = subprocess.Popen(
            argv, cwd=str(ROOT), stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, bufsize=1,
            env={**os.environ},
        )
    except Exception as exc:  # noqa: BLE001
        with BACKFILL_LOCK:
            BACKFILL_STATE.update({"state": "error", "error": f"Could not start: {exc}"})
        return False, f"Could not start backfill: {exc}"
    with BACKFILL_LOCK:
        BACKFILL_PROC["proc"] = proc
    threading.Thread(target=_backfill_reader, args=(proc,), daemon=True).start()
    return True, "Backfill started."


def _backtest_reader(proc):
    try:
        for raw in proc.stdout:
            with BACKTEST_LOCK:
                BACKTEST_LOG.append(raw.rstrip("\n"))
    finally:
        try:
            rc = proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            rc = proc.wait()
        with BACKTEST_LOCK:
            BACKTEST_STATE["exit_code"] = rc
            BACKTEST_STATE["finished_at"] = datetime.now().isoformat(timespec="seconds")
            BACKTEST_STATE["state"] = "done" if rc == 0 else "error"
            if rc != 0:
                BACKTEST_STATE["error"] = (f"Backtest exited with code {rc}. "
                                           "See the log below.")
            BACKTEST_PROC["proc"] = None


def start_backtest(top_n=10, weighting="score", rebalance="monthly",
                   cost_bps=10.0, start="", end=""):
    """Start a backtest run. Returns (ok, message)."""
    script = ROOT / "portfolio" / "backtest.py"
    if not script.exists():
        return False, "portfolio/backtest.py not found."
    try:
        top_n = int(top_n)
        cost_bps = float(cost_bps)
    except (TypeError, ValueError):
        return False, "Top N and cost must be numbers."
    if top_n < 1 or top_n > 100:
        return False, "Top N must be between 1 and 100."
    if weighting not in ("score", "equal", "rank"):
        return False, f"Unknown weighting: {weighting!r}."
    if rebalance not in ("monthly", "quarterly", "annual", "2-year",
                         "3-year", "5-year", "buy-and-hold"):
        return False, f"Unknown rebalance frequency: {rebalance!r}."
    if cost_bps < 0 or cost_bps > 1000:
        return False, "Cost must be between 0 and 1000 bps."
    argv = [sys.executable or "python3", str(script),
            "--top-n", str(top_n), "--weighting", weighting,
            "--rebalance", rebalance, "--cost-bps", str(cost_bps)]
    if start:
        argv += ["--start", str(start)]
    if end:
        argv += ["--end", str(end)]
    with BACKTEST_LOCK:
        if BACKTEST_STATE["state"] == "running":
            return False, "A backtest is already running."
        BACKTEST_STATE.update({
            "state": "running",
            "started_at": datetime.now().isoformat(timespec="seconds"),
            "finished_at": None, "exit_code": None, "error": None,
        })
        BACKTEST_LOG.clear()
        BACKTEST_LOG.append("$ " + " ".join(argv[1:]))
    try:
        proc = subprocess.Popen(
            argv, cwd=str(ROOT), stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, bufsize=1,
            env={**os.environ},
        )
    except Exception as exc:  # noqa: BLE001
        with BACKTEST_LOCK:
            BACKTEST_STATE.update({"state": "error", "error": f"Could not start: {exc}"})
        return False, f"Could not start backtest: {exc}"
    with BACKTEST_LOCK:
        BACKTEST_PROC["proc"] = proc
    threading.Thread(target=_backtest_reader, args=(proc,), daemon=True).start()
    return True, "Backtest started."


# ---------------------------------------------------------------------------
# HTTP layer (stdlib only -- no extra dependencies)
# ---------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # keep the console quiet
        pass

    def _send_json(self, payload, status=200):
        body = json.dumps(payload, default=_json_default).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path, content_type):
        try:
            data = path.read_bytes()
        except OSError:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        if path in ("/", "/index.html"):
            self._send_file(STATIC_DIR / "index.html", "text/html; charset=utf-8")
        elif path == "/app.js":
            self._send_file(STATIC_DIR / "app.js", "text/javascript; charset=utf-8")
        elif path == "/styles.css":
            self._send_file(STATIC_DIR / "styles.css", "text/css; charset=utf-8")
        elif path == "/api/health":
            self._send_json({"ok": True, "db_module_error": DB_IMPORT_ERROR,
                             "qual_db_exists": QUAL_DB_PATH.exists()})
        elif path == "/api/overview":
            rows, q_err = load_quantitative()
            qual, qual_err = load_qualitative()
            notes = load_notes()
            flag_counts = {}
            for row in rows:
                for flag in row["flags"]:
                    flag_counts[flag] = flag_counts.get(flag, 0) + 1
            self._send_json({
                "rows": rows, "qualitative": qual, "notes": notes,
                "quant_error": q_err, "qual_error": qual_err,
                "flag_counts": flag_counts,
                "flag_meanings": FLAG_MEANINGS,
                "metric_labels": METRIC_LABELS,
                "stale_days": STALE_DAYS,
                "ranking_modes": load_modes(),
                "ranking_locked": RANK_LOCKED_ABSOLUTE,
                "generated_at": datetime.now().isoformat(timespec="seconds"),
            })
        elif path == "/api/notes":
            self._send_json(load_notes())
        elif path == "/api/run/status":
            self._send_json(run_status())
        elif path == "/api/portfolio":
            if portfolio_mod is None:
                self._send_json({"ok": False,
                                 "error": f"Portfolio module unavailable: {PORTFOLIO_IMPORT_ERROR}"})
            else:
                try:
                    qs = parse_qs(parsed.query)
                    weighting = (qs.get("weighting") or ["score"])[0]
                    top_n = int((qs.get("top_n") or ["10"])[0])
                    pf = portfolio_mod.build_portfolio(top_n=top_n, weighting=weighting)
                    self._send_json({"ok": True, "portfolio": pf})
                except Exception as exc:  # noqa: BLE001
                    self._send_json({"ok": False, "error": _short_error(exc)})
        elif path == "/api/backtest":
            self._send_json(backtest_status())
        elif path == "/api/backfill":
            self._send_json(backfill_status())
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):  # noqa: N802
        parsed = urlparse(self.path)
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            payload = json.loads(raw) if raw else {}
        except ValueError:
            self._send_json({"error": "Invalid JSON."}, status=400)
            return
        if parsed.path == "/api/notes":
            ticker = str(payload.get("ticker") or "").strip().upper()
            note = str(payload.get("note") or "").strip()
            severity = str(payload.get("severity") or "check")
            metric = str(payload.get("metric") or "").strip() or None
            if not ticker or not note:
                self._send_json({"error": "A ticker and a note are required."}, status=400)
                return
            notes = load_notes()
            entry = {"id": uuid.uuid4().hex[:8],
                     "created": datetime.now().isoformat(timespec="seconds"),
                     "severity": severity, "metric": metric, "note": note}
            notes.setdefault(ticker, []).append(entry)
            save_notes(notes)
            self._send_json({"ok": True, "notes": notes})
        elif parsed.path == "/api/notes/delete":
            ticker = str(payload.get("ticker") or "").strip().upper()
            note_id = str(payload.get("id") or "")
            notes = load_notes()
            notes[ticker] = [n for n in notes.get(ticker, []) if n.get("id") != note_id]
            if not notes[ticker]:
                notes.pop(ticker, None)
            save_notes(notes)
            self._send_json({"ok": True, "notes": notes})
        elif parsed.path == "/api/run":
            ok, message = start_run(
                str(payload.get("job") or ""),
                payload.get("limit", 25),
                str(payload.get("requeue") or "none"),
            )
            self._send_json({"ok": ok, "message": message, "run": run_status()},
                            status=200 if ok else 409)
        elif parsed.path == "/api/run/stop":
            stopped = stop_run()
            self._send_json({"ok": True, "stopped": stopped, "run": run_status()})
        elif parsed.path == "/api/ranking-modes":
            modes = save_modes(payload.get("modes") or {})
            self._send_json({"ok": True, "modes": modes})
        elif parsed.path == "/api/backtest/run":
            ok, message = start_backtest(
                payload.get("top_n", 10),
                str(payload.get("weighting") or "score"),
                str(payload.get("rebalance") or "monthly"),
                payload.get("cost_bps", 10.0),
                str(payload.get("start") or ""),
                str(payload.get("end") or ""),
            )
            self._send_json({"ok": ok, "message": message, "backtest": backtest_status()},
                            status=200 if ok else 409)
        elif parsed.path == "/api/backfill/run":
            ok, message = start_backfill(
                payload.get("start_year", 2016),
                str(payload.get("rebalance") or "quarterly"),
                payload.get("max_tickers", 0),
                bool(payload.get("force", False)),
            )
            self._send_json({"ok": ok, "message": message, "backfill": backfill_status()},
                            status=200 if ok else 409)
        else:
            self.send_response(404)
            self.end_headers()


def main():
    server = None
    port = PORT
    last_error = None
    for _ in range(10):  # if the port is busy, try the next few
        try:
            server = ThreadingHTTPServer((HOST, port), Handler)
            break
        except OSError as exc:
            last_error = exc
            port += 1
    if server is None:
        raise SystemExit(f"Could not start the dashboard server: {last_error}")
    url = f"http://{HOST}:{port}"
    print(f"Valuproj dashboard running at {url}")
    print("Press Ctrl+C in VS Code to stop it. Data is read live from MySQL + qualitative_agent.db.")
    if OPEN_BROWSER:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nDashboard stopped.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
