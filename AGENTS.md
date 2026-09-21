# financial_data_pull — notes for agents

Acquisition and caching only. This project pulls a ticker's data into an immutable
local store; interpretation lives downstream, so the model, checks, valuation,
memo and workbook work belongs in the consuming project.

## Read first

- `README.md` — what the library pulls, where the data lands, and the guarantees
  the store provides.
- `tests/run_all.sh` — three offline suites that pin those guarantees; read the
  suite covering the path you are about to change.

## Conventions the code does not show

- **Providers own the network.** Fetching lives in `providers/`; `pull.py`
  orchestrates and reaches a client library only through a provider.
- **Tests stay offline.** Double `providers.sec.statements`, `providers.yahoo.fetch`
  and `providers.alphavantage.*`, and wrap the store with
  `tests/test_store._TempPlane`, so a suite run touches neither the network nor the
  repo's `data/`. `bash tests/run_all.sh` runs all three suites.
- **The store's guarantees are the product.** Atomic publish, immutable snapshots,
  hash-checked reads, and per-dataset status with a reason are what this library is
  for. A change near `save_raw`, `commit_snapshot` or `read_verified_table` earns a
  test that goes red when the guarantee breaks.
- **Scope is per source set.** `sources=["yahoo"]` and `sources=["sec"]` publish
  separate snapshots with separate cache keys — the key is issuer + ticker + source
  set + transcript quarters — which is what keeps a price refresh from re-pulling 11
  filings. Keep the split.
- **The cache has no TTL.** Freshness is the caller's `refresh=True`; an age rule
  would arrive as a new opt-in parameter, never as a change to the default.
- **Keys stay out of artifacts.** `.env` is gitignored, and `store.clean_error`
  redacts credentials and URLs before exception text reaches a manifest — providers
  use it too, so a failing dataset is legible without leaking the key it was called
  with.
- **Pins are load-bearing.** Dependency versions move deliberately, with the
  Parquet round-trip in mind.

## Gotchas

- `from financial_data_pull import pull` binds the **function** (the package
  re-exports it). Import from `financial_data_pull.pull` to reach `SOURCES`,
  `Ceiling`, `scope_key`, or the module itself.
- Transcripts need `alpha_vantage` among `sources`, and such a run re-acquires the
  estimates too: a source set is acquired whole, so adding transcript quarters
  makes a new scope and a new snapshot.
- A live pull spends real quota and minutes — `sec` is 22 requests for 11 filings,
  Alpha Vantage's free tier is tightly limited, and preserved originals run to
  ~98 MB per ticker. Tests never run one.
