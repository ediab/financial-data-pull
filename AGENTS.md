# financial_data_pull — notes for agents

Acquisition and caching only. This project pulls a ticker's data into an immutable
local store; interpretation lives downstream, so the model, checks, valuation,
memo and workbook work belongs in the consuming project.

## Read first

- `docs/plans/2026-09-21-financial-data-pull.md` — the implementation plan. It
  records why each deviation from upstream exists, and the per-file lists of what
  may change. Read it before touching `store.py` or `pull.py`.
- `docs/specs/2026-09-21-financial-data-pull.md` — the agreed brief.

## Conventions the code does not show

- **Providers own the network.** Fetching lives in `providers/`; `pull.py`
  orchestrates and reaches a client library only through a provider.
- **Tests stay offline.** Double `providers.sec.statements`, `providers.yahoo.fetch`
  and `providers.alphavantage.*`, and wrap the store with
  `tests/test_store._TempPlane`, so a suite run touches neither the network nor the
  repo's `data/`. `bash tests/run_all.sh` runs all three suites.
- **Copied code stays in step with upstream.** `config.py`, `store.py`,
  `contracts.py` and `providers/*` are copies of
  `~/Dev/equity-research/src/equity_research/`. To change one, copy the upstream
  file again and re-apply the intended delta (the plan lists them per file) rather
  than hand-editing both copies.
- **The store's guarantees are the product.** Atomic publish, immutable snapshots,
  hash-checked reads, and per-dataset status with a reason are what this library is
  for. A change near `save_raw`, `commit_snapshot` or `read_verified_table` earns a
  test that goes red when the guarantee breaks.
- **Scope is per source set.** `sources=["yahoo"]` and `sources=["sec"]` publish
  separate snapshots with separate cache keys, which is what keeps a price refresh
  from re-pulling 11 filings. Keep the split.
- **The cache has no TTL.** Freshness is the caller's `refresh=True`; an age rule
  would arrive as a new opt-in parameter, never as a change to the default.
- **Keys stay out of artifacts.** `.env` is gitignored, and `_clean_error` redacts
  credentials and URLs before exception text reaches a manifest.
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
