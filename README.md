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
`yfinance==1.7.0`, `lxml==6.1.3`). The pins are load-bearing: Parquet round-tripping
and the parsed contents of a press release are correctness concerns, not implementation
details.

`.env` holds live API keys. It is gitignored and never packaged.

## Use

```sh
.venv/bin/financial-data-pull NVDA                       # every source
.venv/bin/financial-data-pull NVDA --sources yahoo       # one source set
.venv/bin/financial-data-pull NVDA --sources sec --refresh
.venv/bin/financial-data-pull NVDA --cache-only           # zero network
.venv/bin/financial-data-pull NVDA --ceiling alpha_vantage=10,sec=40
.venv/bin/financial-data-pull NVDA --earnings-8k 10       # 10 earnings press releases
.venv/bin/financial-data-pull NVDA --transcripts none     # no transcripts this pull
.venv/bin/financial-data-pull NVDA --quiet                # warnings only
.venv/bin/financial-data-pull NVDA --export-csv           # CSVs, zero network
.venv/bin/financial-data-pull NVDA --export-views         # derived views, zero network
.venv/bin/financial-data-pull --index                     # what is held, all tickers
```

The JSON result is printed to stdout. One line per dataset (INFO) and one human
status line for the run go to **stderr**, so redirecting stdout still captures just
the JSON. `--quiet` drops those stderr lines to warnings only.

Or from Python:

```python
from financial_data_pull import export_csv, manifest, pull, read_table
from financial_data_pull.views import export_views

result = pull("NVDA")                  # first call: network + a new snapshot
                                       # (statements, prices and the last 4 transcripts)
pull("NVDA")                           # {"status": "CACHED", ...} — zero network
pull("NVDA", refresh=True)             # add a new snapshot version
pull("NVDA", cache_only=True)          # never touches the network
pull("NVDA", sources=["yahoo"])        # its own snapshot, its own refresh cadence
pull("NVDA", transcripts=[])           # opt out of the default transcripts
pull("NVDA", sources=["alpha_vantage"], transcripts=["2026Q1", "2026Q2"])
pull("NVDA", eight_ks=10)              # Item 2.02 8-Ks + their Exhibit 99.1

manifest("NVDA")                       # the newest snapshot's snapshot.json
read_table("NVDA", "income_annual_0")  # hash-verified read
export_csv("NVDA")                     # {table: snapshot_id}, writes data/csv/NVDA/
export_views("NVDA")                   # {group: count}, writes data/derived/NVDA/
```

To read a table without knowing a `run_id`, prefer the CSVs: `read_table` reads
only the **newest** snapshot, and a later run that acquired something else (an
8-K-only pull, say) does not carry that table. `export_csv` picks, per table, the
newest snapshot that actually holds it.

## What it pulls

| Source | Tables | Notes |
|---|---|---|
| SEC EDGAR (edgartools) | 33 | 3 annual 10-K + 8 quarterly 10-Q → `income_`, `balance_`, `cashflow_` each, `_annual_0..2` / `_quarterly_0..7` (index 0 is the most recent filing) |
| Yahoo (yfinance) | 8 | `yahoo_prices` (1 year of daily bars) + 7 analyst datasets (`yahoo_earnings_estimate`, `yahoo_revenue_estimate`, `yahoo_eps_trend`, `yahoo_eps_revisions`, `yahoo_analyst_price_targets`, `yahoo_recommendations`, `yahoo_upgrades_downgrades`) |
| Alpha Vantage | 2 | `av_earnings_estimates` (one request) + `av_transcript`, one call per quarter of the last 4 **completed** calendar quarters derived from today (1.5s spacing, retried on the free tier's limiter). Transcripts are acquired only when `alpha_vantage` is among `sources`; `--transcripts none` (or `transcripts=[]`) opts out, and an explicit `YYYYQN` list names the quarters instead |
| SEC EDGAR (opt-in) | 1 | `sec_8k` — one row per Item 2.02 earnings 8-K (ticker, filing date, accession, items, exhibit file and path). The Exhibit 99.1 press release is preserved untouched under `raw/`; `--earnings-8k N` sets how many filings, newest first |

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
data/csv/<ticker>/<table>.csv                         readable export, newest good copy per table
data/derived/<ticker>/documents/8-k/<date>-<accession>-<exhibit>.htm   copies of the exhibits
data/derived/<ticker>/documents/transcript/<quarter>.md                rendered call transcript
data/derived/<ticker>/8k_cells.csv                     one row per cell of every release table
```

A `run-id` looks like `2026-09-21T145130+0000-fa3741`. A refresh adds a new one.

`--export-csv` writes one CSV per table and prints the snapshot each table came
from (`income_annual_0 ← 2026-09-21T145130+0000-fa3741`). Each table comes from the
newest snapshot that retrieved it, so a later run holding only some tables does not
hide the rest. The CSVs are derived: they can always be rewritten from the
snapshots, and the snapshots stay the evidence. `av_transcript` content is long
multi-line quoted text, so open that CSV with a real CSV reader, not by eye.

## Derived views

`--export-views` writes `data/derived/<ticker>/` from the snapshots already held and
prints one line per group (`documents/8-k: 10 files`, `8k_cells.csv: 39,038 rows`). It
makes no network call, refuses the acquisition flags, and never writes on a second run
whose bytes are already there. A recorded artifact that has vanished, or one whose hash
no longer matches, refuses the whole export with a message instead of writing a partial
view.

- **`documents/8-k/`** — each preserved Exhibit 99.1 under
  `<filing_date>-<accession>-<exhibit_file>`, for reading in a browser. The file is
  copied only after its sha256 matches the content-addressed directory it sits in, and
  a mismatch aborts the export naming the file.
- **`documents/transcript/`** — one Markdown file per held call quarter, rendered from
  the hash-checked `av_transcript` table: title, a source line naming the snapshot, then
  one paragraph per speaker turn. Blank turns are kept, so the file is the whole call.
- **`8k_cells.csv`** — one row per `<td>`/`<th>` of every table of every held release,
  with `accession`, `filing_date`, `exhibit_sha256`, `table_index`, `caption`, `row_kind`,
  `row_index`, `col_index`, `row_label`, `column_label`, `raw_text`, `value` and `unit`.
  `value` is the cell's digits with a sign (`$ 2,810.6` → `2810.6`, `(3.9)` → `-3.9`,
  `26.1 %` → `26.1`, `1,234` → `1234`) and blank when the cell is not a number — a dash,
  `N/M`, a footnote marker, or a guidance range, which stays legible in `raw_text`.
  An exhibit that is not HTML (the SEC provider allows a PDF press release) has no cells:
  it is named on stderr and contributes no rows, while its copy stays under `documents/8-k/`
  to be read directly.
  `row_kind` is `header` for a `<th>`/`<thead>` cell and for the rows above the first
  parsable value in a table that has one, so header rows can be dropped without
  re-inventing header detection; `column_label` resolves the header cells above a cell
  through colspan/rowspan. There is no per-ticker metric extraction: the cell dump is
  filer-agnostic, and mapping labels to metrics is downstream work.

Derived views are **rewritable and never evidence**. They may be deleted at any time,
they are rebuilt from the store on demand, and a downstream reader cites the snapshot,
not the view. 10-K and 10-Q primary documents are deliberately not copied here: their
only original is a ~9 MB full-text submission, and their statements are already in
`data/csv/`.

`--index` (optionally with a ticker, and never combined with other flags) prints one
block per issuer, aggregating the rows of every coverage file it has recorded: how
many pulls are held and when the last one ran, the filing range behind the statements,
the 8-K dates, the transcript quarters, the snapshot datasets, and one line per
degraded (`MISSING`/`FAILED`/…) row. It is generated, so it cannot go stale the way
a hand-written index would.

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
  The cache key is the issuer, the ticker, the source set, the transcript quarters
  and the 8-K depth, and it contains no date — so the cache never expires on its
  own. Freshness is the explicit `refresh=True`. Because the key is per source set, a
  daily Yahoo refresh does not re-pull 11 SEC filings. A pull without 8-Ks keeps the
  8-K component of its key exactly as it was before the 8-K parameter existed.
- **The default scope moves once per calendar quarter.** A plain pull derives its
  transcript quarters from today, so the key changes at each rollover: the first plain
  pull after one is a **full re-acquisition of the whole source set** — SEC (~22
  requests, ~98 MB of originals), Yahoo, estimates and the one new transcript — not
  just the new quarter. Every quarter already held costs nothing. Pass
  `transcripts=[]` (`--transcripts none`) for a scope that never moves. A quarter
  Alpha Vantage has not posted yet — a call that just ended, typically — is recorded
  `MISSING`, and because the scope then caches, later plain pulls keep missing it
  until `refresh=True` (`--refresh`) retries it.
- **`cache_only=True` never touches the network**, and returns
  `{"status": "MISSING: NOT_RETRIEVED"}` when nothing is held for that scope.

## Providers and quotas

Alpha Vantage is a source for earnings estimates and transcripts and nothing
else — it is never a price source and never a fallback for Yahoo. The free tier is
tightly limited, so a plain `pull(ticker)` spends up to five requests on a first
acquisition — one for `av_earnings_estimates` and one per transcript quarter, four by
default. A quarter already held in a snapshot costs zero. A calendar-quarter rollover
is a new scope, so its first plain pull spends the two AV requests that scope needs —
one for estimates, one for the newly completed transcript — plus the SEC and Yahoo
re-acquisition the new scope implies. `--ceiling` caps a run (requests are counted and
returned either way).
Transcript calls are counted under their own `alpha_vantage_transcripts` key, so
`--ceiling alpha_vantage=10,alpha_vantage_transcripts=2` caps the two
independently.

Yahoo is unofficial: yfinance changes under you, and a failure there shows up as
`FAILED` coverage rows with everything else still succeeding. A failing Yahoo does
not block a pull.

SEC requires `EDGAR_IDENTITY` in `.env`; expect ~22 requests for the 11 filings,
and the preserved full-text submissions dominate the disk (~98 MB for one ticker).
`--earnings-8k N` adds roughly one request per filing whose press release is
archived (the filing's full-text submission, which also carries the exhibit) plus a
few for the filing index itself, and those requests count under the same
`--ceiling sec=N` key. A run with 8-Ks is a new
scope: the first one re-acquires its whole source set — Yahoo, Alpha Vantage and all
11 SEC filings — and not only the 8-Ks. The same holds for a transcript quarter the
default scope has newly picked up. When only the documents are wanted, name
`--sources sec` (which also keeps the run transcript-free, since transcripts need
`alpha_vantage`).

## Tests

```sh
bash tests/run_all.sh     # four suites, all offline: providers are doubled
```

## Out of scope

The model, checks, valuation, delivery, memo, workbooks, Excel recalculation,
company-document fetching beyond the archived 8-K exhibits, per-ticker metric maps built
inside this library (the 8-K cell dump ships; interpreting it does not), PDF parsing,
DuckDB, FRED and macro series. They live downstream of the pull.
