"""Shared database helpers for the value-investing pipeline.

Both pipeline stages use this module so connection handling and the schema
live in exactly one place.
"""
import os

from dotenv import load_dotenv
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import URL

load_dotenv("info.env")

# Queue statuses used across the pipeline:
#   queued  - waiting to be evaluated
#   success - evaluated, enough metrics to be ranked
#   partial - evaluated, but too few metrics to rank (e.g. foreign filers with no US-GAAP data)
#   failed  - evaluation errored; re-queue with `initial_screen.py --requeue failed`

QUEUE_DDL = """
CREATE TABLE IF NOT EXISTS queue (
    Ticker VARCHAR(20) PRIMARY KEY,
    Status VARCHAR(20) NOT NULL
)
"""

STOCK_STATS_DDL = """
CREATE TABLE IF NOT EXISTS stock_stats (
    ticker                  VARCHAR(20) PRIMARY KEY,
    `date`                  DATE,
    sector                  VARCHAR(64),
    industry                VARCHAR(64),
    fiscal_year_end         DATE,
    pe_ratio                DOUBLE,
    pb_ratio                DOUBLE,
    p_dcf                   DOUBLE,
    debt_to_equity          DOUBLE,
    reinvestment_efficiency DOUBLE,
    roe                     DOUBLE,
    data_flags              VARCHAR(255),
    composite_score         DOUBLE,
    overall_rank            INT
)
"""

# Point-in-time ranking snapshots (see portfolio/snapshots.py). stock_stats
# only keeps each ticker's latest evaluation, so without this table there is
# no history to backtest on. One row per ticker per run date; re-running on
# the same day overwrites that day's snapshot.
RANKING_SNAPSHOTS_DDL = """
CREATE TABLE IF NOT EXISTS ranking_snapshots (
    snapshot_date           DATE NOT NULL,
    ticker                  VARCHAR(20) NOT NULL,
    composite_score         DOUBLE,
    overall_rank            INT,
    sector                  VARCHAR(64),
    industry                VARCHAR(64),
    pe_ratio                DOUBLE,
    pb_ratio                DOUBLE,
    p_dcf                   DOUBLE,
    debt_to_equity          DOUBLE,
    reinvestment_efficiency DOUBLE,
    roe                     DOUBLE,
    data_flags              VARCHAR(255),
    source                  VARCHAR(16) NOT NULL DEFAULT 'live',
    PRIMARY KEY (snapshot_date, ticker)
)
"""

# Columns added to ranking_snapshots after its first version. ensure_schema()
# adds any that are missing so an existing table upgrades in place.
RANKING_SNAPSHOTS_ADDED_COLUMNS = {
    # 'live' = saved by a Stage 2 run; 'backfill' = reconstructed history (portfolio/history.py)
    "source": "VARCHAR(16) NOT NULL DEFAULT 'live'",
}

# Columns added after the first version of the schema. ensure_schema() adds
# any that are missing so an existing database upgrades in place.
STOCK_STATS_ADDED_COLUMNS = {
    "sector": "VARCHAR(64)",
    "industry": "VARCHAR(64)",
    "fiscal_year_end": "DATE",
    "roe": "DOUBLE",
    "data_flags": "VARCHAR(255)",
    "composite_score": "DOUBLE",
    "overall_rank": "INT",
}


def get_engine():
    """Build a SQLAlchemy engine from env vars.

    URL.create() escapes the credentials; interpolating the password into an
    f-string URL breaks as soon as it contains characters like @ / : or #.
    """
    url = URL.create(
        "mysql+pymysql",
        username=os.environ["DB_USERNAME"],
        password=os.environ["DB_PASSWORD"],
        host=os.environ.get("DB_HOST", "localhost"),
        database=os.environ["DB_NAME"],
    )
    return create_engine(url, pool_pre_ping=True)


def ensure_schema(engine):
    """Create tables if needed and add any columns introduced in later versions."""
    with engine.begin() as conn:
        conn.execute(text(QUEUE_DDL))
        conn.execute(text(STOCK_STATS_DDL))
        conn.execute(text(RANKING_SNAPSHOTS_DDL))

    existing = {c["name"] for c in inspect(engine).get_columns("ranking_snapshots")}
    with engine.begin() as conn:
        for col, ddl in RANKING_SNAPSHOTS_ADDED_COLUMNS.items():
            if col not in existing:
                conn.execute(text(f"ALTER TABLE ranking_snapshots ADD COLUMN {col} {ddl}"))

    existing = {c["name"] for c in inspect(engine).get_columns("stock_stats")}
    with engine.begin() as conn:
        for col, ddl in STOCK_STATS_ADDED_COLUMNS.items():
            if col not in existing:
                conn.execute(text(f"ALTER TABLE stock_stats ADD COLUMN {col} {ddl}"))
