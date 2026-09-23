# financial-data-pull

Pulls a ticker's financial data from SEC EDGAR, Alpha Vantage and Yahoo into local
immutable snapshots. A ticker with held evidence is served from the verified union;
network acquisition is explicit through `refresh=True` or the first pull.

The library verifies its evidence — statement arithmetic and earnings-release ties —
and does not interpret, normalize or forecast the data.

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
.venv/bin/financial-data-pull NVDA --refresh                # a new version; warns if thinner
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

## How to pull

```python
from financial_data_pull import export_csv, manifest, pull, read_table
from financial_data_pull.views import export_views

result = pull("NVDA")                  # serves held union or acquires if nothing is held
pull("NVDA", refresh=True)             # explicitly acquire a new snapshot
pull("NVDA", cache_only=True)          # zero network; MISSING if nothing is held
pull("NVDA", sources=["yahoo"])        # filters a held bundle; refresh acquires this source set
pull("NVDA", transcripts=[])           # opt out of transcripts on acquisition
pull("NVDA", eight_ks=0)                # opt out of earnings releases

manifest("NVDA")                       # newest snapshot manifest
read_table("NVDA", "income_annual_0")  # hash-verified read
export_csv("NVDA")                     # newest-snapshot CSV set
export_views("NVDA")                   # releases/transcripts and 8k_cells.csv
```

Every successful `pull` bundle has `tables`, `report`, `provenance`, `absent`,
`status`, and run/snapshot facts. `tables` contains per-filing datasets plus computed
`income_history`, `balance_history`, and `cashflow_history` when those families are
held. `report` contains the verdict (`CHECKED`, `DISCREPANCY`, or `UNCHECKED`), six
named checks (`balance`, `ytd_sum`, `q4_fy`, `revenue_release`, `cash_reconcile`,
`quarter_window`), tolerances, identified concepts and gaps; checks never block
otherwise publishable evidence. `provenance` maps tables to their source snapshot
and hash (history tables name source snapshots), and `absent` names requested
families with no held evidence. Acquisition results also include `snapshot_dir`, `coverage_path`,
`scope_key`, `sources`, `transcripts`, `eight_ks`, `table_hashes`, `statuses`,
`open_gaps`, `requests`, and sometimes `reported_quarters` and its note. Served
results include `snapshot`, `snapshot_dirs`, and `cache_only`. CLI JSON includes the
run facts, `report`, `provenance`, `absent`, and `table_names` (`{name: {rows,
columns}}`) rather than DataFrame contents.

A plain call for any held ticker serves the verified union of its snapshots with zero
network; filters shape that bundle. Only `refresh=True` acquires and folds held
evidence forward. The canonical scope is 12 reported quarters, with 12 earnings
releases by default when SEC is requested (`eight_ks=0` opts out). To acquire a
changed release depth, use `refresh=True`; it acquires the full requested source set.
Transcript labels
derive from SEC filing periods; when no SEC evidence exists, pass explicit labels. A
held ticker's first refresh may re-acquire the full source set once because the
canonical scope widened. An acquisition can make up to 12 Alpha Vantage transcript
calls at 1.5-second spacing; a quarter already held is never re-asked.

Report tolerances: statement arithmetic within $1,000 of filing rounding; release
ties within 0.5 × the release's stated scale (1e6, then 1e3); cash reconciliation
below `0.05 × 5e9`; and 12 contiguous quarter ends with 80–105-day gaps (11 noted).

## What it pulls

| Source | Tables | Notes |
|---|---|---|
| SEC EDGAR (edgartools) | up to 36 | 3 annual 10-K + 9 quarterly 10-Q → `income_`, `balance_`, `cashflow_` each, `_annual_0..2` / `_quarterly_0..8` (index 0 is the most recent filing) |
| Yahoo (yfinance) | 8 | `yahoo_prices` (1 year of daily bars) + 7 analyst datasets (`yahoo_earnings_estimate`, `yahoo_revenue_estimate`, `yahoo_eps_trend`, `yahoo_eps_revisions`, `yahoo_analyst_price_targets`, `yahoo_recommendations`, `yahoo_upgrades_downgrades`) |
| Alpha Vantage | 2 | `av_earnings_estimates` (one request) + `av_transcript`, up to 12 filing-reported quarters (1.5s spacing, retried on the free tier's limiter). Transcripts require `alpha_vantage`; labels derive from held SEC filing periods, or the current SEC fetch on a first acquisition. Without SEC evidence, supply explicit `YYYYQN` labels. `--transcripts none` (or `transcripts=[]`) opts out |
| SEC EDGAR | 1 | `sec_8k` — one row per Item 2.02 earnings 8-K (ticker, filing date, accession, items, exhibit file and path). The Exhibit 99.1 press release is preserved untouched under `raw/`; default depth is 12 with SEC, `--earnings-8k N` changes it and `0` opts out |

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
data/csv/<ticker>/<table>.csv                         newest snapshot tables + history CSV extras
data/derived/<ticker>/documents/8-k/<date>-<accession>-<exhibit>.htm   copies of the exhibits
data/derived/<ticker>/documents/transcript/<quarter>.md                rendered call transcript
data/derived/<ticker>/8k_cells.csv                     one row per cell of every release table
```

A `run-id` looks like `2026-09-21T145130+0000-fa3741`. A refresh adds a new one.

`--export-csv` writes one CSV per manifest table in the newest snapshot plus the
three `<family>_history.csv` extras when those families exist. It prints the snapshot
each table came from (`income_annual_0 ← 2026-09-21T145130+0000-fa3741`). The directory
holds one evidence version, never a mixture; stale files outside that set are pruned.
A named index becomes a column of its own — the
dates on `yahoo_prices`, the period on the analyst frames — because an undated price
row is not evidence. The CSVs are derived: they can always be rewritten from the
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
- **Serve-first; refresh is explicit.** Any held snapshot for the requested ticker
  serves the verified union of its evidence with zero network; source, transcript,
  and release filters shape the bundle. `refresh=True` acquires and folds held
  evidence forward. Scope keys remain acquisition facts, not the serve condition.
  The canonical scope widened to 12 reported quarters and 12 releases, so a held
  ticker's first refresh may re-acquire its whole source set once (~22 SEC statement
  requests and ~98 MB originals, plus other requested sources). Held transcript
  quarters are frozen and never re-asked. Default transcript labels come from SEC
  filing periods; a source set without SEC evidence requires explicit labels.
  Missing provider quarters are recorded as gaps; use `refresh=True` to retry.
- **`cache_only=True` never touches the network**, and returns
  `{"status": "MISSING: NOT_RETRIEVED"}` when no ticker-matching snapshot is held.

## Providers and quotas

Alpha Vantage is a source for earnings estimates and transcripts and nothing
else — it is never a price source and never a fallback for Yahoo. Its free tier is
tightly limited. An acquisition may make up to 12 transcript calls, spaced at 1.5
seconds; a quarter already held is frozen evidence and never re-asked. The estimates
call is separate. `--ceiling` caps a run (requests are counted and returned either way).
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
scope: the first one re-acquires its whole source set — Yahoo, Alpha Vantage and the
statement filings — and not only the 8-Ks. When only SEC evidence is wanted, name
`--sources sec` (which also keeps the run transcript-free, since transcripts need
`alpha_vantage`).

## Tests

```sh
bash tests/run_all.sh     # five suites, all offline: providers are doubled
```

`scripts/check_store_view.py` is the other direction — against a real store:

```sh
.venv/bin/python scripts/check_store_view.py            # every issuer held under data/
.venv/bin/python scripts/check_store_view.py ANET VRT   # named issuers
```

It is not part of `run_all.sh` because it needs a populated `data/`; it reads what is held
and writes nothing. It answers the consumer's question instead of the library's: is the
latest snapshot self-contained (every table readable with no `run_id`), does the balance
sheet balance and the cash reconcile, does the quarter-building arithmetic tie out
(`YTD = Σ quarters`, `Q4 = FY − 9M`), and does every quarter's revenue appear in that
quarter's own earnings release. A derived Q4 that matches the press release to the dollar
is the strongest offline proof the tables are right; a quarter that does not match is
either a parse bug or a period you built from the wrong filing.

## Out of scope

Forecasting, modeling, valuation, delivery, memo, workbooks, Excel recalculation,
company-document fetching beyond the archived 8-K exhibits, per-ticker metric maps
(the 8-K cell dump ships; interpreting it does not), PDF parsing, DuckDB, FRED and
macro series. The library verifies as-filed arithmetic and release ties; it does not
interpret, normalize or forecast.
