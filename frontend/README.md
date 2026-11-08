# Valuproj dashboard (local front-end)

A read-only dashboard for the pipeline, so you can see the ranked
shortlist, every metric, every data flag in plain English, and the
qualitative scores — and jot down anything that looks wrong, per ticker.

## Design

"The ledger": a research desk's working paper. Paper-white background,
ink-navy text, one ledger-green accent (positive / primary actions),
brick red for negative. Serif (Georgia) for the masthead and headings,
sans for the interface, tabular figures throughout. The overview figures
sit in a single ruled tape, sections are flat and separated by hairlines
(no rounded cards), and the ranked table is the hero — sticky header,
serif rank, green/red z-scores. System fonts only; nothing is fetched
from the network. Accessibility: skip link, visible keyboard focus,
`prefers-reduced-motion` respected, all controls labelled.

## Run it

1. Open the `valuproj` folder in VS Code.
2. Open `frontend/app.py` and press **Run** (top-right Run button, or F5).
   No terminal flags — the port and host are constants at the top of `app.py`.
3. Your browser should open at `http://127.0.0.1:8123`.
   If it doesn't, copy that address into your browser.
4. Press Ctrl+C in the VS Code terminal to stop it.

It only listens on `127.0.0.1`, so nothing leaves your machine.

## Running the pipeline from the dashboard

The **Pipeline** panel at the top runs the real scripts — no re-implementation:

- **Run Stage 2** — evaluates the next 10 / 25 / 50 / 100 queued tickers, or
  all of them (`repeatable.py --limit N`, 0 = all). This is the one to use to
  keep working through the queue.
- **Recompute rankings only** — `repeatable.py --rank-only`: refreshes the
  composite scores and ranks without fetching anything.
- **Run Stage 1** — `initial_screen.py`: screens Yahoo for new candidates.
  "Add new only" is the normal choice; "Retry failed" re-queues tickers
  that errored; "Re-evaluate all" sends everything back through Stage 2.

Only one run at a time — starting another while one is running is refused.
**Stop run** ends the current run; tickers already finished stay saved in
the database.

While a run is active (with **Live updates** ticked, the default), the page
polls the server every few seconds: the run status shows how many tickers
are done and which one is being evaluated now, the log streams the script's
own output, and the ranked table refreshes live from the database as
tickers finish — new rows and updated ranks pop in without pressing
Refresh. Untick Live updates to pause the polling; Refresh still works
manually at any time. Your filters and any note you are typing are kept
while the table refreshes.

Runs use the same Python, working directory, and `info.env` as running the
scripts in VS Code, so Stage 2 still needs `SEC_USER_AGENT` set. A run can
take a while (each ticker hits Yahoo and the SEC) — you can keep browsing
the dashboard while it works.

## Peer-relative z-scores

Each of P/E, P/B, debt/equity and ROE has a **switch** in the "Rank by"
row that controls both how the metric is ranked and how its column is
shown: switch on and the column shows its **peer z-score**, switch off
and it shows its **absolute value**. There is no separate display toggle
— the switch is the display.
**P/DCF and reinvestment efficiency always show absolute values** — only
the absolute number matters for those, so no z-score is computed for them
and they do not count toward the Peer z average.

- **Sign:** positive always means *better than peers* — cheaper for P/E,
  P/B and debt/equity; higher for ROE.
  A P/E z-score of +1.5 means that P/E sits 1.5 peer standard deviations
  below the peer mean; −1.5 means 1.5 above it.
- **Peers, narrowest first:** the ticker's **industry** when at least 5
  industry peers have that metric, otherwise its **sector**, otherwise all
  stocks in the table. Each z-score records which scope was used, the peer
  mean and standard deviation, and the peer count — hover a cell in the
  table, or open the detail view, to see them.
- **Peer z** (the column after Score) is the mean of the ticker's available
  z-scores — a quick peer-relative summary. The CSV export includes it, plus
  the industry column.
- **Where industry comes from:** Stage 2 stores Yahoo's industry for each
  ticker in `stock_stats.industry` (added to `db.py` alongside sector).
  Rows evaluated before that change have no industry yet, so their
  z-scores fall back to sector until you re-run Stage 2 for them — use the
  Pipeline panel's Run Stage 2 for that. Peer stats prefer rows evaluated
  in the last 45 days; if too few fresh rows have a metric, all rows with
  that metric are used instead.

## Ranking toggles — what goes into Rank and Score

Rank and Score in the table are **recomputed live in the dashboard** from
the switches in the "Rank by" row (saved in `frontend/ranking_modes.json`,
so your choices survive a restart):

- **P/E, P/B, debt/equity, ROE** — each can count by its **absolute value**
  or by its **peer z-score**. Flip a switch and the rankings recompute
  right away, and that metric's column switches between raw values and
  z-scores with it — the switch controls both. In z-score mode a metric's
  percentile is taken over the tickers' z-scores (higher z = better),
  instead of over their absolute values.
- **P/DCF and reinvestment efficiency are absolute only** — only the
  absolute number matters for those, so they have no switch.

The live ranking mirrors Stage 2's `recompute_rankings()` exactly
otherwise: only rows evaluated in the last 45 days with at least 4 of 6
usable metrics (usable = a value in absolute mode, a z-score in relative
mode) and no negative-equity flag are ranked; a missing metric counts as
neutral 0.5; the composite is the mean of the six per-metric percentiles
and rank 1 is best. Stage 2's stored rank and score are still shown in
each row's detail view, so you can compare the live ranking against the
one the pipeline wrote.

## Tabs: Shortlist / Backtest

The dashboard has two tabs (remembered in the URL hash, e.g. `#backtest`,
and in the browser, so a refresh keeps the tab you were on). **Shortlist**
is the ranked table, the pipeline runner and the review notes.
**Backtest** is everything strategy-related: the portfolio, the history
backfill, and the simulation. The backtest tab's data loads the first
time you open it.

## Portfolio & backtest

The **Portfolio — top 10** panel is the implementable version of the
strategy: the top 10 ranked stocks from the pipeline's stored ranking,
weighted by their scores (weight ∝ 1 − composite score by default; equal
and rank weighting are in the dropdown). The portfolio score is the
weight-weighted average composite score. Rebuild it any time from the
dropdown — it always reads the live database.

**History — rebuild the past** runs `portfolio/history.py`, which
reconstructs the ranking as it stood on each past rebalance date (from
the 10-K filings that were public by then and the price on the day) and
saves them as snapshots, so the backtest can cover years instead of just
the period since today. It is resumable and shows its progress in the
panel; the snapshot coverage line tells you how many dates exist and how
many are backfilled vs live. Snapshot frequency is your choice — monthly,
quarterly (default), yearly, every 2 / 3 / 5 years — using the same
window rules as the backtest. A longer interval is much faster (fewer
dates to rebuild), but keep it at least as frequent as the holding
period you plan to test, since the backtest reuses the latest snapshot
on each rebalance date. First run takes a while (years of prices for
~600 tickers); re-runs are fast because everything is cached.

**Simulate returns** runs `portfolio/backtest.py` on point-in-time ranking
snapshots (saved automatically each time Stage 2 runs, plus any backfilled
history) against Yahoo prices, vs buy-and-hold S&P 500. Pick a time
period (1Y / 3Y / 5Y / 10Y / All, or your own From–To dates) and a
holding period — 1 month, 3 months, 1 year, 2 years, 3 years, 5 years,
or buy and hold — plus how many top stocks to hold and a trading cost
in bps. (Any whole-month window works from the command line, e.g.
`--rebalance 18m`.) On each rebalance date
it buys from the ranking as it stood on that date (the latest snapshot
dated on or before it), never today's ranking replayed over the past; if
no snapshot exists yet it stays in cash and says so. It needs at least
two snapshot dates; if there aren't enough yet it says so instead of
inventing a past it never saw. Results are saved to
`portfolio/backtest_results.json` and shown in the tab: headline stats
(total return, CAGR, volatility, Sharpe, max drawdown, end value, each
against the S&P 500), a "Growth of $100" chart with both lines starting
at 100, a drawdown chart, a "Rebalance history" table showing what was
held at each rebalance and which snapshot it came from, and the full risk
analysis (beta, alpha, tracking error, information ratio, Sortino,
Calmar, monthly returns).
See `portfolio/README.md` for the full method and caveats, including
look-ahead bias and what the self-test proves.

## What it shows

- **Overview cards:** how many tickers are ranked / success / partial /
  failed / queued, how many rows carry flags, how many are stale (>45 days),
  and how many tickers you have flagged yourself.
- **Ranked shortlist:** sortable table of every ticker in `queue` joined to
  `stock_stats` — rank, composite score, P/E, P/B, P/DCF, debt/equity,
  reinvestment efficiency, ROE, metrics present (x/6), date, flags, and the
  latest qualitative scores. Filter by ticker, sector, status, flag, or
  "has flags" / "stale" / "flagged by me". Click a flag chip to filter by it.
  **Export CSV** downloads whatever is currently filtered.
- **Detail view:** click any row. All six metrics, the fiscal year end, the
  evaluation date and age, every flag with a plain-English explanation,
  which metrics are missing, links to Yahoo Finance and SEC EDGAR, and the
  latest management (rationale / transparency / credibility, 0–5) and macro
  (0–10) scores with their justifications, tailwinds and headwinds.
- **Review notes:** in the detail view, add a note ("Looks wrong" /
  "Check later" / "Confirmed OK", optionally tied to one metric). Notes are
  listed in the "Flagged for review" panel at the top and on the row.
  They are stored only in `frontend/review_notes.json` on this machine.

## Data sources

- MySQL/MariaDB via `../db.py` + `info.env` — `queue` + `stock_stats`.
  This is the same connection the pipeline uses, so if the pipeline runs,
  the dashboard can read it.
- `../Agent_Wardrobe/qualitative_agent.db` (SQLite, opened read-only) —
  `qualitative_scores` + `macro_scores`. Optional: the agentic reviewer
  is not part of this repo, so this file normally does not exist and the
  dashboard simply shows "No qualitative score yet" for each ticker.

The dashboard never writes rows to either database. The one exception is
schema: on start it runs the pipeline's own `db.ensure_schema()`, which
creates the tables if they are missing and adds any columns an older
`stock_stats` table lacks — the same upgrade `repeatable.py` performs.
If a source is missing or unreachable, it shows a banner at the top
explaining what is wrong instead of crashing — e.g. MySQL not running,
`info.env` missing, or no qualitative scores yet (run
`Agent_Wardrobe/agent_loop.py` for a ticker first).

## Files

- `app.py` — the server (Python standard library only, plus the pipeline's
  own `db.py` for the MySQL connection). Edit `PORT` / `HOST` /
  `OPEN_BROWSER` at the top to change how it runs.
- `static/index.html`, `static/app.js`, `static/styles.css` — the UI.
- `review_notes.json` — created when you save your first note.

## Troubleshooting

- **Port already in use:** the server tries ports 8123–8132 in order and
  prints the one it actually used.
- **"Could not connect to MySQL":** start MySQL/MariaDB and check `info.env`
  (DB_USERNAME / DB_PASSWORD / DB_NAME). The pipeline needs the same thing.
- **"Unknown column 's.sector'" (fixed Nov 6, 2026):** your `stock_stats`
  table predates the `sector` / `roe` / `data_flags` / `composite_score` /
  `overall_rank` columns. The dashboard now runs `db.ensure_schema()` on
  start and adds them itself. After the upgrade, those columns are empty
  until you re-run `repeatable.py`, which fills sector, ROE, flags, scores
  and ranks — the dashboard only displays what is there.
- **Page loads but stays blank / Refresh does nothing (fixed Nov 6, 2026):**
  the server was crashing on `TypeError: Object of type Decimal is not JSON
  serializable` — PyMySQL returns MySQL numeric columns as `Decimal`, which
  `json.dumps` cannot encode. The dashboard now converts `Decimal` to
  `float` and has a fallback encoder, and a failed data load now shows a
  banner on the page instead of failing silently. If the page ever goes
  blank again, the banner will say so — check the VS Code terminal for the
  traceback.
- **"No qualitative scores yet":** that panel is empty until Stage 3 has
  scored at least one ticker; the quantitative table still works.
- **Blank table, no banner:** press Refresh in the page, then check the
  VS Code terminal for the error the server printed.
