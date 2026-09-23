# financial_data_pull — notes for agents

This project pulls a ticker's data into an immutable local store and verifies its
evidence (statement arithmetic and release ties). It does not interpret, normalize or
forecast; modeling and valuation belong downstream.

## Read first

- `README.md` — what the library pulls (including the newest call transcripts a plain
  pull acquires — two by default), where the data lands, and store guarantees
  provides.
- `tests/run_all.sh` — five offline suites that pin those guarantees; read the
  suite covering the path you are about to change.

## Conventions the code does not show

- **Providers own the network.** Fetching lives in `providers/`; `pull.py`
  orchestrates and reaches a client library only through a provider.
- **Tests stay offline.** Double `providers.sec.statements`, `providers.sec.earnings_8k`,
  `providers.yahoo.fetch` and `providers.alphavantage.*`, and wrap the store with
  `tests/test_store._TempPlane`, so a suite run touches neither the network nor the
  repo's `data/`. `bash tests/run_all.sh` runs all five suites. The opposite check —
  `scripts/check_store_view.py`, which reads a populated `data/` and nothing else — is
  deliberately outside the suite.
- **Statement tables are not uniform across issuers.** `dimension` is a *boolean*: the
  consolidated line is `False` and segment rows share the concept, so filter it or a
  segment answers for the company. The consolidated revenue label is "Total revenue"
  (ANET) or "Net sales" (VRT); the equity total is `StockholdersEquity` (ANET, VRT) or
  `StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest` (AVGO); the
  cash-flow ending figure is cash *and restricted cash*, which is not the balance
  sheet's cash line. Period columns carry the filing's own label — `2026-06-30 (Q2)` for
  a calendar filer, `2026-08-02 (Q3)` for AVGO — so quarter arithmetic must read the
  label, never the month.
- **A later 10-K can round an earlier year.** FY2023 revenue is `5,860,168,000` in the
  FY2023 10-K and `5,860,200,000` in the FY2025 10-K's comparative column. Derive Q4
  from the filing that owns the period (FY from that year's 10-K, 9M from that year's
  Q3 10-Q) or the derived quarter inherits the rounding. A Q4 whose 9M only a later
  filing's restated comparative supplies is still derived, and the history marks that
  column `(Q4 derived from comparative)` so the provenance is visible.
  `scripts/check_store_view.py` does it that way and ties to the press release exactly.
- **The store's guarantees are the product.** Atomic publish, immutable snapshots,
  hash-checked reads, and per-dataset status with a reason are what this library is
  for. A change near `save_raw`, `commit_snapshot` or `read_verified_table` earns a
  test that goes red when the guarantee breaks.
- **Scope keys describe acquisitions, not serves.** `sources=["yahoo"]` and
  `sources=["sec"]` publish separate snapshots with separate keys — issuer + ticker
  + source set + transcript quarters + 8-K depth. Any held snapshot for the requested
  ticker serves the verified union with zero network; only `refresh=True` acquires.
  Refreshes fold held evidence forward. A held ticker's first refresh may re-acquire
  the full source set once because the canonical default scope widened.
- **Scope-key serialization is load-bearing.** `eight_ks` joins the scope dict only
  when truthy, so an explicit no-release request preserves its historical key shape.
  A golden test pins that hash; changing serialization needlessly churns acquisition
  identities. The key records what a refresh acquires, not whether evidence can serve.
- **`data/csv/` is derived, never evidence.** `export_csv` reads the newest snapshot —
  the same view `read_table` reads — through the same hash check, verifying every table
  before it writes the first CSV; a recorded parquet that has vanished refuses the export
  rather than falling back to an older snapshot. A table the newest snapshot lacks is
  absent from the export and its stale CSV is removed, so the directory is one version of
  the evidence rather than a mixture. The parquet store stays the source of truth.
- **`data/derived/` is derived too, and rewritable.** `views.py` builds the document
  copies, the transcript Markdown and the 8-K cell dump from the snapshots already
  held — under the same hash gating (`read_verified_table`, or the original's sha256
  against its content-addressed directory), so an unverified byte, or a recorded artifact
  that has vanished, refuses the export with a `ValueError` the CLI prints rather than
  reaching a view. A name composed from filing metadata is validated with
  `store.safe_component` before it becomes a path. `data/csv/` keeps its flat `<table>.csv`
  paths; the documents view is the only place a readable filing name exists.
- **The cache has no TTL.** Freshness is the caller's `refresh=True`; an age rule
  would arrive as a new opt-in parameter, never as a change to the default.
- **Keys stay out of artifacts.** `.env` is gitignored, and `store.clean_error`
  redacts credentials and URLs before exception text reaches a manifest — providers
  use it too, so a failing dataset is legible without leaking the key it was called
  with.
- **Pins are load-bearing.** Dependency versions move deliberately, with the
  Parquet round-trip and the parsed press release in mind.
- **Logging is finish-only, on stderr.** The library logs but never configures a
  handler, so an importable use is silent; the CLI installs one and prints one INFO
  line per dataset plus one human run status (`CACHED — evidence already held, zero
  network (snapshot <run-id>)`, `NEW SNAPSHOT <run-id> — N requests spent`, nothing
  held, or `FAILED — nothing published`). Degraded datasets get a WARNING naming their
  reason. `--quiet` drops the handler to WARNING and silences the status line; stdout
  stays the JSON result alone.

## Gotchas

- `from financial_data_pull import pull` binds the **function** (the package
  re-exports it). Import from `financial_data_pull.pull` to reach `SOURCES`,
  `Ceiling`, `scope_key`, or the module itself.
- **Transcripts derive from reported quarters.** With `alpha_vantage` requested,
  `transcripts=None` labels the newest 2 distinct calendar quarters represented by
  held SEC filing periods; on a first acquisition, labels come from that run's SEC
  filings. Name explicit `YYYYQN` labels to ask for more quarters.
  A source set with no SEC evidence anywhere must provide explicit labels.
  `transcripts=[]` / `--transcripts none` opts out. Held transcript quarters are
  frozen and never re-asked, including on refresh — a serve returns all of them,
  whatever depth a new acquisition would ask for; new calls are paced 1.5 seconds
  apart (two per acquisition by default).
- **Earnings releases are on by default.** `eight_ks=None` with `sec` requests 12
  Item 2.02 releases; `eight_ks=0` opts out. To acquire a different depth, use
  `refresh=True`; changing depth acquires the full requested source set.
- **Serve before acquire.** Any ticker with held snapshots serves their verified
  union with zero network, shaped by request filters. Only `refresh=True` acquires.
  Since the canonical scope widened to 12 reported quarters and 12 releases, a held
  ticker's first refresh may re-acquire the full source set once (~22 SEC requests,
  ~98 MB originals, plus other requested sources).
- A newly reported transcript may not yet be available from Alpha Vantage; it is
  recorded as missing. Use `--refresh` to retry acquisition.
- A live acquisition spends quota and minutes. Tests never run one.
