# financial_data_pull — brief

Date: 2026-09-21
Status: agreed brief, not yet planned or implemented

## Idea and purpose

A standalone Python library that acquires financial data for a ticker from
primary and market providers, and caches it locally and immutably. It is the
acquisition + caching layer of `~/Dev/equity-research` extracted into its own
project, with the research workflow removed.

The library pulls data and caches it. It does not interpret it.

## Who uses it and what they do

The owner, or another project, calls `pull(ticker)` from Python or through a
thin CLI. The first call hits the network and publishes a snapshot; repeat calls
for the same scope read the cache back with zero network calls. Data that could
not be acquired is returned as an explicit status with a reason — never silently
omitted, never fabricated, never substituted.

## Smallest useful version

Verbatim reuse of equity-research's acquisition and store layer. Only three
things cannot be copied as-is (see *Deviations from upstream*).

```
financial_data_pull/
  pyproject.toml            hatchling; deps pinned to match upstream
  .env                      copied from equity-research; gitignored
  .env.example
  .gitignore                data/, .env, .venv/, __pycache__/
  docs/specs/
  schemas/coverage.json     copied verbatim
  src/financial_data_pull/
    config.py               verbatim; ROOT env var renamed
    store.py                verbatim minus the snapshot-kind filter
    contracts.py            read_verified_table + validate_against + load_schema
    cli.py                  thin wrapper over pull()
    providers/sec.py        verbatim (SEC EDGAR via EdgarTools)
    providers/yahoo.py      verbatim (yfinance)
    providers/alphavantage.py  verbatim
    pull.py                 new and slimmed (see below)
  tests/
```

Dependencies: `pandas`, `pyarrow`, `edgartools`, `yfinance` — pinned to the same
versions as equity-research. `openpyxl` and `pypdf` are **not** needed (workbook
and PDF parsing live downstream of the pull).

### Public surface

```python
from financial_data_pull import pull

pull("NVDA")                 # sources = sec + alpha_vantage + yahoo → one snapshot
pull("NVDA", sources=["yahoo"])                      # own snapshot, own refresh cadence
pull("NVDA", sources=["sec"])
pull("NVDA", sources=["alpha_vantage"], transcripts=["2025Q1", "2025Q2"])
pull("NVDA", cache_only=True)                        # zero network
pull("NVDA", refresh=True)                           # publishes a new snapshot version
pull("NVDA", ceilings={"alpha_vantage": 10, "sec": 40})
```

### Store layout

Identical to equity-research:

```
data/raw/<ticker>/<provider>/<sha256>/payload.<ext>       content-addressed originals
data/tables/<ticker>/<run-id>/<table>.parquet + snapshot.json
data/coverage/<ticker>/<run-id>.json                      one status row per dataset
```

Properties carried over unchanged:

- Snapshots are versioned and immutable. A refresh **adds** a version; it never
  mutates an existing one.
- Atomic publish: tables are written into `.staging-*`, the manifest is written
  *inside* that directory, then the directory is renamed into place. A crash
  cannot leave a half-written snapshot that `cache_only` would trust.
- Every table is hashed in the manifest, and reads go through the hash check
  that fails closed.
- Partial failure preserves the datasets that succeeded.
- Coverage rows report `RETRIEVED` / `MISSING` / `FAILED` / `RATE_LIMITED` /
  `PARSE_FAILED`, each failure with a reason (`NOT_PUBLISHED`,
  `NOT_RETRIEVABLE`, `NOT_SUPPLIED`, `PREMIUM_OR_UNCOVERED`).
- Cache-first: a plain run returns `CACHED` for a matching scope with zero
  network calls. Fresher data is an explicit `refresh=True`.
- `cache_only=True` never touches the network.

### Deviations from upstream

1. **`pull.py` is new (~200 lines vs 698).** The approval, case, proposal and
   plan machinery is removed; the cache key becomes a hash of
   `(ticker, source set, transcript quarters)` owned by the library rather than
   a `plan_hash` derived from an approved source plan. One code path replaces
   the three snapshot-kind entrypoints.
2. **`store.py` loses the snapshot-kind filter.** `snapshot_kind()` and the
   `kind=` argument to `snapshot_dirs()` / `latest_snapshot_dir()` go away; the
   manifest's `sources` field takes over snapshot selection.
3. **`config.py` renames its root override** from `EQUITY_RESEARCH_ROOT` to
   `FINANCIAL_DATA_PULL_ROOT`.

## What is pulled

Verified against the real equity-research snapshot
`data/tables/NVDA/2026-09-19T180230+0000-5f0f6d/` — 42 tables, 20 preserved
originals, one snapshot.

### SEC EDGAR via EdgarTools — 33 tables (11 filings × 3 statements)

- 3 annual 10-K filings → `income_annual_0..2`, `balance_annual_0..2`,
  `cashflow_annual_0..2`
- 8 quarterly 10-Q filings → `income_quarterly_0..7`, `balance_quarterly_0..7`,
  `cashflow_quarterly_0..7`
- Each table is one row per XBRL concept (~33–46 rows) with ~18–20 columns:
  `concept`, `label`, `standard_concept`, the period columns, and structural
  columns (`level`, `abstract`, `dimension`, `is_breakdown`, `dimension_axis`,
  `dimension_member`, `dimension_member_label`, `dimension_label`)
- Period columns are labelled as the filing states them. For a
  January-year-end company like NVDA: annual flows carry `(FY)` tags
  (`2026-01-25 (FY)`), quarterly flows carry both `(Qn)` and `(YTD)`, and
  balance sheets use bare point-in-time dates because a stock is observed at an
  instant. Quarters and year-to-date are never conflated.
- Each filing's full-text submission is preserved as an immutable original
  (11 of the 20 originals)
- 20-F fallback: tried only when **zero** annual 10-Ks are retrievable (foreign
  private issuers). A failed quarterly or a transport error is not evidence of
  an FPI.
- A statement a filing genuinely does not carry is recorded `MISSING` with a
  reason, not treated as an error.
- Each filing is fetched once and yields every statement it carries.

### Alpha Vantage — 1 table, plus `av_transcript` when requested

- `av_earnings_estimates` — 41 rows × 18 columns for NVDA; both `fiscal year`
  and `fiscal quarter` horizons; estimate dates spanning 2017-07-31 →
  2028-01-31.
  - EPS: average, high, low, analyst count, 7/30/60/90-day revision history,
    and up/down revision counts over trailing 7 and 30 days
  - Revenue: average, high, low, analyst count
- `av_transcript` — only when `transcripts=[...]` is passed. One provider call
  per `YYYYQN` quarter, 1.5s minimum spacing, 3 retries with backoff on the free
  tier's rate limit. Columns: `quarter`, `segment`, `speaker`, `title`,
  `content`. A real VRT pull produced 135 and 762 segment rows across two
  quarters.
- A throttled quarter is retried, never recorded as a missing quarter. A quarter
  the provider does not carry is `MISSING` with the provider's own message.
- Alpha Vantage provides estimates and transcripts only. It is **not** the price
  source — see Yahoo below.

### Yahoo via yfinance — 8 tables

- `yahoo_prices` — 251 rows of daily OHLCV over 1 year: `Open`, `High`, `Low`,
  `Close`, `Volume`, `Dividends`, `Stock Splits`
- `yahoo_earnings_estimate` — 4 rows indexed `0q`, `+1q`, `0y`, `+1y`: `avg`,
  `low`, `high`, `yearAgoEps`, `numberOfAnalysts`, `growth`, `currency`
- `yahoo_revenue_estimate` — same 4 rows: `avg`, `low`, `high`,
  `numberOfAnalysts`, `yearAgoRevenue`, `growth`, `currency`
- `yahoo_eps_trend` — same 4 rows: `current`, `7daysAgo`, `30daysAgo`,
  `60daysAgo`, `90daysAgo`, `currency`
- `yahoo_eps_revisions` — same 4 rows: `upLast7days`, `upLast30days`,
  `downLast30days`, `downLast7Days`, `currency`
- `yahoo_analyst_price_targets` — 1 row: `current`, `high`, `low`, `mean`,
  `median`
- `yahoo_recommendations` — 4 rows: `period`, `strongBuy`, `buy`, `hold`,
  `sell`, `strongSell`
- `yahoo_upgrades_downgrades` — 983 rows for NVDA, date-indexed: `Firm`,
  `ToGrade`, `FromGrade`, `Action`, `priceTargetAction`, `currentPriceTarget`,
  `priorPriceTarget`
- yfinance exposes no raw-response accessor, so the preserved payload for these
  eight is the normalised frame as JSON, and the manifest says so.

### Alongside every run

- One `coverage/<ticker>/<run-id>.json` with a row per dataset
- `snapshot.json` recording `sources`, `retrieved_at`, per-provider metadata,
  SHA-256 per table, and original paths
- Content-addressed originals under `raw/<ticker>/<provider>/<sha256>/`

### Known ceilings in the data

- `yahoo_prices` is **1 year of daily bars only** — no intraday, no longer
  history
- The four Yahoo estimate tables are indexed by provider-relative labels
  (`0q`, `+1y`) that are only resolvable against the snapshot's stated as-of
  date. The manifest records that basis explicitly.
- Alpha Vantage's free tier is heavily rate-limited; a wide transcript pull can
  exhaust it.

## Explicit exclusions

- No approvals, cases, proposals, or `assignments/` — **no G0 gate**
- No `Ceiling`-as-consent. The request counter is always tracked and returned; a
  ceiling applies only when `ceilings={...}` is passed, otherwise a run is
  uncapped
- No `facts`, model, checks, valuation, delivery, memo, workbook, recalc, or
  lock modules
- No `openpyxl` or `pypdf` dependencies
- No company-document URL fetching or discovery
- No DuckDB
- **No FRED / macro series** (dropped 2026-09-21; upstream's `providers/fred.py`
  is not copied)

## Decisions taken

| Question | Decision |
|---|---|
| Which datasets | Mirror equity-research exactly: SEC statements, AV estimates + transcripts, Yahoo prices + analyst datasets |
| Approval scaffolding | Gate dropped entirely; request ceiling kept as an optional parameter |
| Snapshot shape | One snapshot per `(ticker, source set)`, so refresh granularity belongs to the caller. No `statements`/`benchmark`/`transcript` kinds. |
| FRED | Dropped |

## Success examples

1. `pull("NVDA")` twice → the second returns `status: "CACHED"` and makes **zero**
   network calls. Verify with the network blocked, or by asserting no new
   `coverage/` file and no new `tables/` directory appeared.
2. `pull("NVDA", sources=["yahoo"])` then `pull("NVDA", sources=["sec"])` → two
   distinct snapshots. The first holds exactly the 8 `yahoo_*` tables; the second
   holds only `income_*`, `balance_*`, `cashflow_*`. Neither is returned as a
   cache hit for the other.
3. `pull("ZZZZ")` (bogus ticker) → no exception raised; every dataset is
   `FAILED` or `MISSING` **with a `reason`**, and a contract-valid coverage file
   exists on disk.

## Open question

None blocking. `cli.py` is assumed to be a thin wrapper matching the Python
surface above (`financial-data-pull NVDA --sources sec --refresh`); it can be
dropped if only the importable library is wanted.
