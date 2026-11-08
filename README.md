# Value Investing Pipeline

A staged pipeline that narrows the US equity universe down to a ranked shortlist of potentially undervalued companies, using Yahoo Finance for screening and market data and SEC XBRL filings for fundamentals.

```
 Stage 1                    Stage 2                         Stage 3 (planned)
┌──────────────────┐      ┌───────────────────────────┐    ┌───────────────────────────┐
│ initial_screen.py│      │ repeatable.py             │    │ Local LLM review of       │
│ Yahoo screener   │ ───► │ SEC fundamentals + DCF    │ ─► │ management rationale,     │
│ NYSE + Nasdaq GS │      │ composite percentile rank │    │ transparency, credibility │
│ → `queue` table  │      │ → `stock_stats` table     │    │ and macro trends          │
└──────────────────┘      └───────────────────────────┘    └───────────────────────────┘
```

> **Disclaimer:** This is a research tool, not financial advice. Outputs depend on third-party data (Yahoo Finance via the unofficial `yfinance` library, and SEC filings) that can be incomplete or wrong, and on modelling assumptions you should review. Verify anything important against the primary filings.

## What each stage does

### Stage 1 — `initial_screen.py`

Queries Yahoo's screener and loads matching tickers into the `queue` table with status `queued`.

Default criteria (edit the constants at the top of the file):

| Criterion | Default |
|---|---|
| Market cap | > $2B |
| Exchanges | `NMS` (Nasdaq Global Select), `NYQ` (NYSE) |
| Sectors | Real Estate, Energy, Financial Services, Consumer Cyclical, Consumer Defensive, Basic Materials, Industrials, Utilities, Communication Services |

No valuation filter: P/E and P/B are ranking metrics in Stage 2, not prescreen gates — a cheap stock and an expensive one both enter the queue, and the ranking sorts them out.

It pages through all results (Yahoo caps each request at 250), drops preferred shares and non-equities, and is safe to re-run: tickers already in the queue keep their status.

### Stage 2 — `repeatable.py`

Takes tickers from the queue, computes the metrics below, and stores them in `stock_stats` along with a composite rank.

| Metric | Column | Definition | Better |
|---|---|---|---|
| P/E | `pe_ratio` | Yahoo trailing P/E (must be > 0) | lower |
| P/B | `pb_ratio` | Yahoo price/book (must be > 0) | lower |
| P/DCF | `p_dcf` | Market cap ÷ DCF equity value (< 1 suggests undervalued) | lower |
| Debt/Equity | `debt_to_equity` | Total debt ÷ shareholders' equity | lower |
| Reinvestment efficiency | `reinvestment_efficiency` | Change in market cap ÷ change in retained earnings over the lookback window (Buffett's "one-dollar premise") | higher |
| ROE | `roe` | Median annual ROE over the last 5 fiscal years (needs ≥ 3 years) | higher |

**DCF method.** Unlevered free cash flow per fiscal year is `CFO − CapEx + Interest × (1 − tax)`. The starting value is the mean of the last 3 years; growth is the median year-over-year change, clamped to ±10%. Cash flows are projected 5 years at a 9% discount rate with 2.5% terminal growth (Gordon growth), then `equity value = enterprise value + cash − debt`. A year with missing CapEx is skipped rather than treated as zero. If P/DCF lands outside 0.01–100 it is discarded as a data error.

**Financial Services and Real Estate** skip the DCF and debt/equity metrics, since free-cash-flow valuation and conventional leverage ratios don't apply to banks, insurers, and REITs. They are still ranked on the remaining metrics.

**Industry** (from Yahoo, stored alongside sector in `stock_stats`) is the narrower peer grouping used by the dashboard for peer-relative z-scores: each metric is compared against its industry peers when at least 5 of them have that metric, otherwise its sector, otherwise all stocks.

**Ranking.** Each metric becomes a percentile rank, the percentiles are averaged into `composite_score`, and `overall_rank` 1 is best. Rules:

- A ticker needs at least 4 of the 6 metrics to be ranked.
- Companies with negative equity are not ranked (their book-based ratios are meaningless).
- A missing metric counts as a neutral 0.5.
- Rows older than 45 days are ignored.
- Set `RANK_WITHIN_SECTOR = True` to rank against sector peers instead of the whole universe.

**Queue statuses**

| Status | Meaning |
|---|---|
| `queued` | Waiting to be evaluated |
| `success` | Evaluated with enough metrics to be ranked |
| `partial` | Evaluated, but too few metrics to rank (e.g. foreign filers without US-GAAP data) |
| `failed` | Errored; retry with `initial_screen.py --requeue failed` |

### Stage 3 — local LLM review *(planned, not yet implemented)*

The intended next step is to feed the top-ranked tickers (for example, `WHERE overall_rank <= 25`) to a local LLM to assess management rationale, transparency, credibility, and broader macro context.

## Front-end dashboard

`frontend/` is a local, read-only dashboard for reviewing pipeline output:
open `frontend/app.py` in VS Code and press Run (no terminal flags) — it
serves the ranked shortlist, every metric, every data flag in plain English,
and the qualitative scores at `http://127.0.0.1:8123`, with per-ticker review
notes for anything that looks wrong. See `frontend/README.md` for details.

## Portfolio & backtesting

`portfolio/` turns the ranked shortlist into an implementable strategy
and backtests it. `portfolio.py` builds the top-10 portfolio, weighted by
score (weight ∝ 1 − composite score; equal and rank weighting available).
`backtest.py` replays the strategy over point-in-time ranking snapshots
(`ranking_snapshots`, saved automatically each time Stage 2 runs) against
Yahoo prices, vs buy-and-hold S&P 500, with monthly, quarterly,
annual, 2-year, 3-year or 5-year rebalancing (any whole-month window
works from the command line, e.g. `--rebalance 18m`) and a cost per
rebalance. It needs at least two snapshot
dates, so the first real backtest becomes possible after Stage 2 has run
on two different dates — until then it refuses rather than replaying
today's ranking over past prices (look-ahead bias). Both are also in the
dashboard's Backtest tab, which has the portfolio, the history backfill,
and a simulate form (pick a period and a holding period — 1 month, 3
months, 1 year, 2 years, 3 years, 5 years, or buy and hold — and it
charts the strategy against the S&P 500 with Sharpe, drawdown and
the rest). See `portfolio/README.md`
for the method, caveats, and the self-test.

`history.py` rebuilds the past instead of waiting for snapshots to
accumulate: for each rebalance date back to 2016 (snapshot frequency
is your choice — monthly, quarterly, annual, every 2 / 3 / 5 years,
quarterly by default), it reconstructs what the pipeline would have
computed on that date — from the 10-K filings that were public by then
and the price on the day — and saves the result as a snapshot
(`source = 'backfill'`).
Run it from the dashboard's Portfolio panel; it is resumable and the
first run takes a while (years of prices for ~600 tickers).
`analysis.py` adds the comprehensive reporting: monthly and annual
returns, worst drawdowns, beta / alpha / correlation / tracking error /
information ratio vs the S&P 500, Sortino, Calmar, turnover and costs.
Known limits: only tickers in today's `stock_stats` can be
reconstructed (survivorship bias), and P/E uses annual EPS, not TTM
EPS — see `portfolio/README.md` for the full method and limits.

## Setup

**Requirements:** Python 3.10+ (developed on 3.12) and MySQL 8 or MariaDB 10.x. Tested on MariaDB 10.11.

```bash
git clone <your-repo-url>
cd <your-repo>
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp .env.example info.env     # then fill in the values
```

`info.env`:

| Variable | Required | Notes |
|---|---|---|
| `DB_USERNAME` | yes | |
| `DB_PASSWORD` | yes | Special characters are fine |
| `DB_NAME` | yes | The database must already exist; tables are created automatically |
| `DB_HOST` | no | Defaults to `localhost` |
| `SEC_USER_AGENT` | yes (stage 2) | The SEC requires a contact identifier, e.g. `Jane Doe jane@example.com`. Requests without one are blocked |

`info.env` is git-ignored. Never commit real credentials.

Tables are created on first run. If you already have `queue` or `stock_stats` tables from an earlier version, missing columns are added automatically. `stock_stats` needs a primary or unique key on `ticker`.

## Usage

```bash
# 1. Populate the queue
python initial_screen.py                     # add new candidates only
python initial_screen.py --requeue failed    # also retry failed tickers
python initial_screen.py --requeue all       # re-evaluate everything (e.g. after earnings season)

# 2. Evaluate and rank
python repeatable.py                         # process 25 queued tickers
python repeatable.py --limit 0               # process the whole queue
python repeatable.py --rank-only             # recompute rankings without fetching anything
```

Stage 2 is designed to be run repeatedly. It stops cleanly if Yahoo or the SEC rate-limits you, leaving unprocessed tickers queued for the next run.

View the results:

```sql
SELECT overall_rank, ticker, sector, ROUND(composite_score, 3) AS score,
       p_dcf, roe, debt_to_equity, data_flags
FROM stock_stats
WHERE overall_rank IS NOT NULL
ORDER BY overall_rank
LIMIT 25;
```

## Configuration

Assumptions live as constants at the top of each script.

| File | Constants |
|---|---|
| `initial_screen.py` | `EXCHANGES`, `SECTORS`, `MIN_MARKET_CAP` |
| `repeatable.py` | `DISCOUNT_RATE`, `TERMINAL_GROWTH_RATE`, `HORIZON_YEARS`, `LOOKBACK_YEARS`, `GROWTH_CAP`, `GROWTH_FLOOR`, `BASE_FCF_YEARS`, `DEFAULT_TAX_RATE`, `MIN_METRICS_TO_RANK`, `RANK_WITHIN_SECTOR`, `MAX_STATS_AGE_DAYS`, `FINANCIAL_SECTORS` |

DCF output is highly sensitive to the discount rate and growth assumptions. Treat P/DCF as a relative screening signal, not a price target.

## Data flags

The `data_flags` column records why a metric is missing or was adjusted, so you can tell a bad company from bad data.

| Flag | Meaning |
|---|---|
| `neg_equity` | Negative book equity; excluded from ranking |
| `no_us_gaap` | No US-GAAP facts (typically a foreign filer using IFRS) |
| `no_sec_cik` | Ticker not found in the SEC ticker map |
| `no_recent_10k` | Latest annual filing is more than ~600 days old |
| `sec_fetch_failed` | SEC request errored |
| `fin_sector_skip` | DCF and debt/equity skipped for a financial or real estate company |
| `dcf_insufficient_fcf` | Fewer than 4 years of usable free cash flow |
| `dcf_nonpositive_fcf` | Normalised free cash flow is zero or negative |
| `dcf_no_debt_data` | Company pays interest but reports no debt tag |
| `dcf_nonpositive_equity` | DCF equity value is zero or negative |
| `pdcf_out_of_range` | P/DCF outside 0.01–100, discarded as a likely data error |
| `no_market_cap` | Yahoo returned no market cap |
| `re_missing_data` | Retained earnings unavailable for the start or end period |
| `re_not_increasing` | Retained earnings did not grow over the window |
| `re_no_market_cap` | Historical market cap unavailable (including skipped due to a stock split) |
| `re_change_immaterial` | Retained earnings change too small for a meaningful ratio |
| `de_no_debt_data` | Debt/equity not computable (see `dcf_no_debt_data`) |
| `<name>_failed` | Unexpected error computing that metric (`yf_info`, `roe`, `re_check`, `de`, `dcf`) |

## Data sources and caching

- **Yahoo Finance** via [`yfinance`](https://github.com/ranaroussi/yfinance): screening, market cap, P/E, P/B, price and share history. This is an unofficial interface and may break or throttle you.
- **SEC EDGAR** XBRL company facts (10-K filings only): cash flow, balance sheet, and income statement items. Each company is downloaded once, trimmed to the fields used, and cached in `sec_cache/` for 7 days. The ticker-to-CIK map (`company_tickers.json`) is downloaded automatically and refreshed weekly. Both are git-ignored.

## Known limitations

- Foreign filers that report under IFRS (20-F) have no US-GAAP data and end up `partial` and unranked.
- Reinvestment efficiency is distorted by buybacks charged against retained earnings and by share issuance. Treat it as a screen, not a verdict.
- Market-cap comparisons are skipped for any company that split its stock inside the lookback window.
- Debt is approximated from XBRL tags and can miss unusual instruments. Operating leases are not included.
- Only annual filings are used, so data can lag by up to a year.

## Project layout

```
initial_screen.py     Stage 1: Yahoo screen → queue
repeatable.py         Stage 2: fundamentals, DCF, ranking → stock_stats
db.py                 Shared connection + schema management
frontend/             Local dashboard (run frontend/app.py in VS Code)
portfolio/            Portfolio construction, backtesting, history backfill
requirements.txt
.env.example          Copy to info.env and fill in (never commit info.env)
```

## Tests

There is no test suite in this repo yet. `portfolio/history.py` and
`portfolio/backtest.py` each have a built-in self-test (`SELF_TEST = True`
at the top of the file, or `--self-test`) that checks the point-in-time
reconstruction and the backtest engine against hand-computed values.
