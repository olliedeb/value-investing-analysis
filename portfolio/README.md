# Portfolio & backtesting

The strategy, made implementable and backtestable: each rebalance, hold
the top 10 ranked stocks, weighted by their scores.

## The strategy

1. **Universe:** the pipeline's ranked shortlist (`stock_stats`, fresh
   rows with a composite score from `repeatable.py`).
2. **Selection:** the top 10 by `overall_rank` (rank 1 = best).
3. **Weighting** (`WEIGHTING` in `portfolio.py` and `backtest.py`):
   - `score` (default) — weight ∝ (1 − composite score). The best-ranked
     stock gets the most capital; the portfolio score is the
     weight-weighted average composite score.
   - `equal` — 1/N each.
   - `rank` — weight ∝ (N + 1 − rank).
4. **Rebalancing (holding period):** monthly by default (first trading
   day of the month), quarterly, annual, 2-year, 3-year, 5-year, or
   buy-and-hold (buy once, never rebalance). Any whole-month window works
   too (`--rebalance 18m` for 18 months). Windows start in January of a
   year divisible by the window (2-year: 2024-25, 2026-27, ...; 5-year:
   2020-24, 2025-29, ...). Fully invested; costs are charged on one-way
   turnover at each rebalance (`COST_BPS`, default 10 bps).

## Files

- `portfolio.py` — builds the current top-10 target portfolio from the
  live database. Run it in VS Code (constants at the top), or see it in
  the dashboard's Portfolio panel. Also writes `portfolio_latest.json`.
- `snapshots.py` — point-in-time ranking snapshots (see below).
- `backtest.py` — the backtest engine. Run it in VS Code (constants at
  the top), or from the dashboard's Portfolio panel. Writes
  `backtest_results.json` (equity curve, stats, rebalance log).
- `data/prices/` — cached Yahoo prices, one CSV per ticker, so repeat
  backtests don't re-download. Each file carries Adj Close (the
  total-return price, split- and dividend-adjusted), Close (split-adjusted
  only), and Dividends ($/share on each ex-date). A cache file without a
  Dividends column is treated as stale and re-downloaded. Delete a file
  (or run with `--refresh-prices`) to refresh it.

## Why backtests need snapshots (read this)

A backtest is only valid if it trades on the ranking *as it stood on the
rebalance date*. `stock_stats` only keeps each ticker's latest evaluation,
so without snapshots there is no history to backtest on — replaying
today's ranking over past prices would be look-ahead bias: anyone can
pick today's winners and look at a chart.

On each rebalance date the engine takes the latest snapshot dated on or
before that date (`snap_date = max(snapshot <= rebalance date)`) and buys
its top N. If no snapshot exists yet for that date, it stays in cash and
says so in the warnings — it never reaches forward to a later ranking.
A stock only enters the portfolio once its own ranking exists, and drops
out the moment it leaves the top N. The dashboard's "Rebalance history"
table shows exactly what was held at each rebalance and which snapshot
it came from, so you can check this yourself on any run.

The self-test proves it with a ranking that flips between two dates
(Jan: A then B; Feb: B then C, all prices flat): the January holdings
are A+B and the February holdings are B+C — A is out, C is in, equity
stays exactly 100,000. A backtest that picked today's stocks would have
held C from the start; this one never does.

That is why `repeatable.py` now saves every run's ranked set into the
`ranking_snapshots` table (one row per ticker per run date, via
`snapshots.py`). History accumulates automatically from the day this
ships. The backtest needs **at least two snapshot dates** and refuses to
run before that, saying why. Missing metrics are stored as NULL, never
NaN: a pandas round-trip silently turns `None` into `NaN` in any float
column that has a real value elsewhere, and MySQL (via PyMySQL) refuses
NaN outright — that crashed the first backfill on 2016-01-04, where
tickers with a failed or nonpositive DCF have no P/DCF. Both the ranking
step and the snapshot writer now convert non-finite floats back to
`None` before anything reaches the database.

## History backfill (`history.py`)

Waiting for snapshots to accumulate one Stage 2 run at a time would take
years to cover a decade. `history.py` rebuilds the past instead: for each
rebalance date back to `START_YEAR` (default 2016), it reconstructs what
the pipeline would have computed on that date, from the 10-K filings
that were public by then and the price on the day, and saves the result
as a snapshot with `source = 'backfill'`. Snapshot frequency is your
choice — monthly, quarterly (default), annual, 2-year, 3-year or 5-year
(any whole-month window works from the command line, e.g. `--rebalance
18m`), using the same window rules as the backtest engine, so the two
can never drift apart. A longer interval is much faster (fewer dates to
reconstruct: from 2016 to today, quarterly is ~43 dates, annual ~11, 5-year just 3)
but the backtest reuses the latest snapshot on each rebalance date, so
keep the backfill at least as frequent as the holding period you plan to
test — a monthly backtest over annual snapshots would reuse the same
ranking for a year.

- **SEC facts:** the same `load_us_gaap()` facts, truncated to filings
  made on or before the date (a restatement filed after the date is not
  used). DCF, debt/equity, ROE and reinvestment efficiency are the exact
  functions from `repeatable.py`, run on the truncated facts.
- **Prices:** Yahoo unadjusted closes + shares-outstanding history,
  cached under `data/history/prices/` and `data/history/shares/`.
  P/E uses the latest annual EPS (net income / shares at the fiscal
  year-end) — the live pipeline uses Yahoo's trailing (TTM) P/E, which
  has no historical equivalent in the 10-K-only data.
- **Screen:** the Stage 1 screen is re-applied as of the date (market cap
  > $2B, 9 sectors -- no P/E or P/B gate; those are ranking metrics, not
  prescreen gates). Ranking uses the same percentile rules as
  `recompute_rankings()`.
- **Resumable:** per-ticker results are cached in
  `data/history/rows_cache.json` and completed dates in
  `data/history/backfill_progress.json`. Stop it any time; it picks up
  where it left off. `--force` redoes everything.

Run it in VS Code (constants at the top of `history.py`) or from the
dashboard's Portfolio panel ("History — rebuild the past"). First run:
expect tens of minutes for ~600 tickers (mostly Yahoo downloads, with a
pause between tickers). Re-runs are fast because everything is cached.
Trial it first with `MAX_TICKERS = 5`.

Known limits: only tickers in today's `stock_stats` can be reconstructed
— companies that died or were delisted are missing from early dates
(survivorship bias, flatters the backtest). Sector/industry are today's
values, and the exchange screen is not re-applied. P/E uses annual EPS,
not TTM EPS. A split after a date makes that date's market cap
unknowable, so the ticker is skipped for that date.

Verify the reconstruction itself with the self-test (`SELF_TEST = True`
in `history.py`, or `--self-test`): it checks the filed-date cutoff, the
DCF, every ratio and the ranking against hand-computed values on a
synthetic company.

## Other caveats

- Prices are Yahoo adjusted closes, so returns are TOTAL returns
  (dividends reinvested). The results also carry a `dividends` block:
  cash dividends received, their share of the total gain, the
  portfolio's dividend yield, and the same for the benchmark. The
  equity curve is not double-counted -- the dividend block only splits
  the gain into income vs price appreciation. Delisted tickers with no
  history are skipped (and flagged).
- Costs are an estimate (10 bps on one-way turnover), not a quote, and
  there is no tax, borrow, or liquidity modelling.
- The benchmark is buy-and-hold `^GSPC` from the same start date.
- Sharpe uses a 0% risk-free rate; annualisation uses 252 trading days.
