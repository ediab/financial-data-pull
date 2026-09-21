# financial-data-pull

Pulls a ticker's financial data from SEC EDGAR, Alpha Vantage and Yahoo, and
stores it locally so the next call for the same thing reads the cache instead of
the network.

It acquires and caches data. It does not interpret it: no model, no checks, no
valuation.

The scope is deliberately narrow: no approval gates, no cases, no proposals, no
memo, no workbooks, no FRED and no macro series. The pull and its cache are the
whole product.

## Install

```sh
python3.10 -m venv .venv
.venv/bin/pip install -e .
cp .env.example .env        # then fill in EDGAR_IDENTITY and ALPHAVANTAGE_API_KEY
```

Dependencies are pinned (`pandas==2.3.3`, `pyarrow==25.0.1`, `edgartools==5.58.0`,
`yfinance==1.7.0`). The pins are load-bearing: Parquet round-tripping is a
correctness concern, not an implementation detail.

`.env` holds live API keys. It is gitignored and never packaged.

## Use

```sh
.venv/bin/financial-data-pull NVDA                       # every source
.venv/bin/financial-data-pull NVDA --sources yahoo       # one source set
.venv/bin/financial-data-pull NVDA --sources sec --refresh
.venv/bin/financial-data-pull NVDA --cache-only           # zero network
.venv/bin/financial-data-pull NVDA --ceiling alpha_vantage=10,sec=40
```

Or from Python:

```python
from financial_data_pull import manifest, pull, read_table

result = pull("NVDA")                  # first call: network + a new snapshot
pull("NVDA")                           # {"status": "CACHED", ...} — zero network
pull("NVDA", refresh=True)             # add a new snapshot version
pull("NVDA", cache_only=True)          # never touches the network
pull("NVDA", sources=["yahoo"])        # its own snapshot, its own refresh cadence
pull("NVDA", sources=["alpha_vantage"], transcripts=["2026Q1", "2026Q2"])

manifest("NVDA")                       # the newest snapshot's snapshot.json
read_table("NVDA", "income_annual_0")  # hash-verified read
```

## What it pulls

| Source | Tables | Notes |
|---|---|---|
| SEC EDGAR (edgartools) | 33 | 3 annual 10-K + 8 quarterly 10-Q → `income_`, `balance_`, `cashflow_` each, `_annual_0..2` / `_quarterly_0..7` (index 0 is the most recent filing) |
| Yahoo (yfinance) | 8 | `yahoo_prices` (1 year of daily bars) + 7 analyst datasets (`yahoo_earnings_estimate`, `yahoo_revenue_estimate`, `yahoo_eps_trend`, `yahoo_eps_revisions`, `yahoo_analyst_price_targets`, `yahoo_recommendations`, `yahoo_upgrades_downgrades`) |
| Alpha Vantage | 1 | `av_earnings_estimates` — one request |
| Alpha Vantage (opt-in) | 1 | `av_transcript`, one call per `YYYYQN` quarter, 1.5s spacing, retried on the free tier's limiter |

SEC tables are one row per XBRL concept. Period columns are labelled as the filing
states them: flows carry `(FY)`, `(Qn)` or `(YTD)`, balance sheets carry bare
point-in-time dates. A statement a filing genuinely does not carry is reported
`MISSING`, not invented.

## Where the data goes

Under `data/` in the project root (gitignored), or wherever
`FINANCIAL_DATA_PULL_ROOT` points:

```
data/raw/<ticker>/<provider>/<sha256>/payload.<ext>   originals, content-addressed
data/tables/<ticker>/<run-id>/<table>.parquet         the tables
data/tables/<ticker>/<run-id>/snapshot.json           manifest: sources, hashes, provenance
data/coverage/<ticker>/<run-id>.json                  one row per dataset: status + reason
```

A `run-id` looks like `2026-09-21T145130+0000-fa3741`. A refresh adds a new one.

## Properties

- **Immutable and versioned.** A refresh adds a version; it never rewrites one.
- **Atomic publish.** Tables are staged, the manifest is written inside the
  staging directory, then the directory is renamed into place. A crash cannot
  leave a half-written snapshot that a cache read would trust.
- **Hash-checked reads.** Every table's sha256 is recorded in the manifest, and
  `read_table` refuses a file that no longer matches it.
- **Honest partial failure.** A provider that fails degrades to `FAILED` coverage
  rows with a reason; the datasets that succeeded are still published. A run that
  retrieved nothing at all publishes no snapshot — it records the same coverage
  rows and reports `FAILED` — so a failed acquisition never reads back as `CACHED`.
  The CLI exits non-zero for a run that published nothing, so a scheduled pull cannot
  report success while the store is unchanged.
- **Cache-first, per source set.** `pull("NVDA")` twice makes one network run.
  The cache key is the issuer, the ticker, the source set and the transcript
  quarters, and it contains no date — so the cache never expires on its own.
  Freshness is the explicit `refresh=True`. Because the key is per source set, a
  daily Yahoo refresh does not re-pull 11 SEC filings.
- **`cache_only=True` never touches the network**, and returns
  `{"status": "MISSING: NOT_RETRIEVED"}` when nothing is held for that scope.

## Providers and quotas

Alpha Vantage is a source for earnings estimates and transcripts and nothing
else — it is never a price source and never a fallback for Yahoo. The free tier is
tightly limited, so a plain `pull(ticker)` costs exactly one request, transcripts
are opt-in, and `--ceiling` caps a run (requests are counted and returned either
way). Transcript calls are counted under their own `alpha_vantage_transcripts` key,
so `--ceiling alpha_vantage=10,alpha_vantage_transcripts=2` caps the two
independently.

Yahoo is unofficial: yfinance changes under you, and a failure there shows up as
`FAILED` coverage rows with everything else still succeeding. A failing Yahoo does
not block a pull.

SEC requires `EDGAR_IDENTITY` in `.env`; expect ~22 requests for the 11 filings,
and the preserved full-text submissions dominate the disk (~98 MB for one ticker).

## Tests

```sh
bash tests/run_all.sh     # three suites, all offline: providers are doubled
```

## Out of scope

The model, checks, valuation, delivery, memo, workbooks, Excel recalculation,
company-document fetching, PDF parsing, DuckDB, FRED and macro series. They live
downstream of the pull.
