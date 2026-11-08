"""Stage 1: screen Yahoo Finance for value candidates and load them into the `queue` table.

Safe to re-run: tickers already in the queue keep their status. Use --requeue to
send previously evaluated tickers back through stage 2 (e.g. after a new
earnings season).

    python initial_screen.py                    # add new candidates only
    python initial_screen.py --requeue failed   # also retry failed tickers
    python initial_screen.py --requeue all      # re-evaluate everything
"""
import argparse
import re
import time

import yfinance as yf
from sqlalchemy import text
from yfinance import EquityQuery

from db import ensure_schema, get_engine

# --- screen parameters (edit here) -------------------------------------------
# Yahoo exchange codes: NMS = Nasdaq Global Select, NYQ = NYSE.
# Also valid if you want broader coverage: NGM (Nasdaq Global Market),
# NCM (Nasdaq Capital Market), ASE (NYSE American).
EXCHANGES = ["NMS", "NYQ"]
SECTORS = [
    "Real Estate", "Energy", "Financial Services", "Consumer Cyclical",
    "Consumer Defensive", "Basic Materials", "Industrials", "Utilities",
    "Communication Services",
]
MIN_MARKET_CAP = 2_000_000_000
PAGE_SIZE = 250  # Yahoo's hard maximum per request

# Preferred shares look like BAC-PL, WFC-PY, etc.
PREFERRED_RE = re.compile(r"-P[A-Z]?$")


def build_query():
    # No valuation filter here on purpose: the prescreen is only market cap
    # (>$2B) + exchange + sector. P/E and P/B are ranking metrics in Stage 2
    # (repeatable.py), not prescreen gates -- a cheap stock and an expensive
    # one both enter the queue, and the ranking sorts them out.
    return EquityQuery("and", [
        EquityQuery("gt", ["intradaymarketcap", MIN_MARKET_CAP]),
        EquityQuery("is-in", ["exchange", *EXCHANGES]),
        EquityQuery("is-in", ["sector", *SECTORS]),
    ])


def screen_for_tickers():
    """Return a de-duplicated list of tickers matching the screen.

    Yahoo returns at most 250 rows per request, so we page with `offset` until
    every match has been collected. The original single-call version silently
    dropped everything past the first 250 results.
    """
    query = build_query()
    symbols = {}  # dict preserves order and de-duplicates
    offset = 0

    while True:
        resp = yf.screen(
            query,
            offset=offset,
            size=PAGE_SIZE,
            sortField="intradaymarketcap",  # stable order so pages don't overlap
            sortAsc=False,
        )
        quotes = (resp or {}).get("quotes", [])
        if not quotes:
            break

        for quote in quotes:
            symbol = quote.get("symbol")
            if not symbol or PREFERRED_RE.search(symbol):
                continue
            if quote.get("quoteType", "EQUITY") != "EQUITY":
                continue
            symbols[symbol.upper()] = None

        offset += len(quotes)
        total = resp.get("total", 0)
        if offset >= total:
            break
        time.sleep(1)  # be polite to Yahoo between pages

    return list(symbols)


def load_queue(engine, tickers, requeue="none"):
    """Insert new tickers as 'queued'. Existing tickers are left untouched."""
    with engine.begin() as conn:
        before = conn.execute(text("SELECT COUNT(*) FROM queue")).scalar()

        if tickers:
            conn.execute(
                text(
                    "INSERT INTO queue (Ticker, Status) VALUES (:ticker, 'queued') "
                    "ON DUPLICATE KEY UPDATE Ticker = Ticker"
                ),
                [{"ticker": t} for t in tickers],
            )

        if requeue == "failed":
            conn.execute(text("UPDATE queue SET Status = 'queued' WHERE Status = 'failed'"))
        elif requeue == "all":
            conn.execute(text("UPDATE queue SET Status = 'queued'"))

        after = conn.execute(text("SELECT COUNT(*) FROM queue")).scalar()
        queued = conn.execute(text("SELECT COUNT(*) FROM queue WHERE Status = 'queued'")).scalar()

    return after - before, queued


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    parser.add_argument("--requeue", choices=["none", "failed", "all"], default="none")
    args = parser.parse_args()

    engine = get_engine()
    ensure_schema(engine)

    tickers = screen_for_tickers()
    print(f"Screen matched {len(tickers)} tickers")

    new, queued = load_queue(engine, tickers, args.requeue)
    print(f"{new} new tickers added; {queued} now waiting in queue")


if __name__ == "__main__":
    main()
