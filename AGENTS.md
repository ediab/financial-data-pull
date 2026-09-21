# financial_data_pull — notes for agents

Acquisition and caching only. This project pulls a ticker's data into an immutable
local store; interpretation lives downstream, so the model, checks, valuation,
memo and workbook work belongs in the consuming project.

## Read first

- `README.md` — what the library pulls (including the last four call transcripts a
  plain pull acquires by default), where the data lands, and the guarantees the store
  provides.
- `tests/run_all.sh` — four offline suites that pin those guarantees; read the
  suite covering the path you are about to change.

## Conventions the code does not show

- **Providers own the network.** Fetching lives in `providers/`; `pull.py`
  orchestrates and reaches a client library only through a provider.
- **Tests stay offline.** Double `providers.sec.statements`, `providers.sec.earnings_8k`,
  `providers.yahoo.fetch` and `providers.alphavantage.*`, and wrap the store with
  `tests/test_store._TempPlane`, so a suite run touches neither the network nor the
  repo's `data/`. `bash tests/run_all.sh` runs all four suites.
- **The store's guarantees are the product.** Atomic publish, immutable snapshots,
  hash-checked reads, and per-dataset status with a reason are what this library is
  for. A change near `save_raw`, `commit_snapshot` or `read_verified_table` earns a
  test that goes red when the guarantee breaks.
- **Scope is per source set.** `sources=["yahoo"]` and `sources=["sec"]` publish
  separate snapshots with separate cache keys — the key is issuer + ticker + source
  set + transcript quarters + 8-K depth — which is what keeps a price refresh from
  re-pulling 11 filings. Keep the split.
- **Scope-key serialization is load-bearing.** `eight_ks` joins the scope dict only
  when truthy, so a pull asking for no 8-Ks hashes exactly as it did before the
  parameter existed and still answers from the snapshots already held. A golden test
  pins that hash; changing the serialization orphans every held snapshot, which
  becomes a permanent cache miss and a re-download. (It is the *derived transcript
  quarters* a plain pull now carries that move the default scope, once a quarter —
  see the transcripts gotcha.)
- **`data/csv/` is derived, never evidence.** `export_csv` takes each table from the
  newest snapshot that carries it, through the same hash check as `read_table`; a
  recorded parquet that has vanished refuses the export rather than falling back to
  an older snapshot. The parquet store stays the source of truth.
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
- **Transcripts are in the default pull and need `alpha_vantage` among `sources`.**
  `transcripts=None` derives the last 4 completed calendar quarters from today
  (`pull.last_completed_quarters`), but only when `alpha_vantage` is in the source
  set — a restricted default pull such as `sources=["sec"]` stays transcript-free
  rather than raising. `transcripts=[]` / `--transcripts none` opts out; an explicit
  `YYYYQN` list overrides. Because the derived labels change every calendar quarter, a
  plain pull's scope moves quarterly: the first plain pull after a rollover
  re-acquires the whole source set (SEC ~22 requests, ~98 MB of originals, Yahoo,
  estimates, one new transcript) — not just the new quarter. A quarter already held
  costs 0.
- 8-Ks need `sec` among `sources`, and `eight_ks` is part of the scope, so adding
  (or changing) it is a fresh acquisition of the whole source set — not just the
  filings, and not a cache hit for the 8-Ks alone.
- A quarter that just ended may not be on Alpha Vantage yet: it is recorded
  `MISSING`, and because the scope then caches, later plain pulls keep missing it.
  Pass `--refresh` (or the explicit quarter label once it is posted) to retry.
- A live pull spends real quota and minutes — `sec` is 22 requests for 11 filings,
  Alpha Vantage's free tier is tightly limited, and preserved originals run to
  ~98 MB per ticker. Tests never run one.
