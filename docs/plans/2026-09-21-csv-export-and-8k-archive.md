# Plan: CSV export + 8-K archiving + skill

Date: 2026-09-21 · Working dir: `/Users/eliasdiab/Dev/financial_data_pull` · Branch: `main`
Brief: `docs/specs/2026-09-21-csv-export-and-8k-archive.md`

> **Status: implemented 2026-09-21** — all five units in; 39 offline checks green
> (`bash tests/run_all.sh`). Acceptance examples 1, 2 and 4 verified against the real
> VRT plane, example 3 with a live SEC-only run (`VRT --sources sec --earnings-8k 10`,
> 32 requests, 10 releases). Five review findings fixed: CLI error handling,
> vanished-parquet refusal, empty-exhibit guard, acquisition-flag conflict, quota note.
> Also exercised by an all-source AVGO pull (43 tables, 52 coverage rows, no gaps).
> The changes are still uncommitted.

## Goal and scope

Make held data readable without knowing run-ids, archive earnings 8-K exhibits
from EDGAR, and give agents a `pull-financial-data` skill. Store semantics
(immutable snapshots, atomic publish, hash-checked reads) untouched.

## Key decisions and rationale

- CSV export selects per table the **newest snapshot that carries it** — a table
  exists in a snapshot only when that dataset was RETRIEVED, so presence is the
  "good" rule. Provenance printed per table.
- 8-Ks join the existing `sec` provider as an **explicit acquisition parameter**
  (`eight_ks=N`), included in the scope key exactly like transcripts — a cached
  plain pull is not silently missing 8-Ks, and different depths don't mask
  each other.
- Exhibit 99.1 HTML saved via existing `store.save_raw` (content-addressed under
  `raw/`); a `sec_8k` table indexes it. No new store machinery.
- No DuckDB, no parsing of press-release numbers.
- **Scope-key stability (blocker from review).** `scope_key` hashes a dict that
  always carries `transcripts`; the `eight_ks` key is added to the dict **only
  when truthy** (`scope["eight_ks"] = sorted(eight_ks)` if set, otherwise
  omitted). A default-None pull must serialize byte-identically to today, or
  every existing cached snapshot misses forever and re-downloads. Guarded by a
  golden test.
- **Coverage rows are per filing**: `dataset="sec_8k_<accession>"` (mirrors
  `av_transcript_<quarter>`), so the aggregated `statuses` dict — which collapses
  duplicate dataset names — keeps one entry per filing.
- **Item 2.02 filtering**: use `filing.items` metadata (zero extra requests).
  Empty/legacy `items` → an explicit MISSING row, never a silent skip. 8-K/A
  amendments: skipped (they amend an existing release; the original is the
  evidence the model reads).
- **Exhibit saving**: `Attachment.content` returns `str` — encode UTF-8 before
  `save_raw`. Suffix comes from the exhibit's actual extension (EX-99s can be
  PDF), not a hardcoded `.htm`.
- **Reason enum values** (wrong value trips CONTRACT_VIOLATION): no Item 2.02
  8-Ks found → `MISSING` + `NOT_PUBLISHED`; per-filing fetch failure → `FAILED`
  + `NOT_RETRIEVABLE`.
- **Quota honesty**: `--earnings-8k N` is a new scope; the first run re-acquires
  the named source set whole (Yahoo + AV + 11 SEC filings) plus the 8-Ks — the
  transcripts gotcha. README/skill point at `--sources sec` for document-only
  pulls.
- **Latest-snapshot drift**: an 8-K-only run becomes `latest_snapshot_dir`, so a
  bare `read_table("VRT", ...)` would hit it and KeyError. Skill/README steer
  agents to the CSVs (which encode the newest-carrying rule) or an explicit
  `run_id`.

## Relevant files

- `src/financial_data_pull/pull.py` — add `export_csv(issuer, out_dir)`; add an
  `eight_ks` branch in `pull()` mirroring the transcripts branch; extend
  `scope_key` with `eight_ks` (omitted from the dict when unset — see Key
  decisions); manifest records `eight_ks` alongside `transcripts`.
- `src/financial_data_pull/providers/sec.py` — add `earnings_8k(ticker, count,
  issuer, ceiling)`. edgartools 5.58 surface to use: `get_filings(form="8-K")`
  (`_filings.py:1245`), `filing.items` metadata for the Item 2.02 filter (zero
  extra requests), `filing.attachments` (`_filings.py:1593`), `filing.exhibits`,
  `Attachments.query("document_type == 'EX-99.1'")`, `Attachment.content`
  (`attachments.py:349/379` — returns `str`; encode UTF-8 for `save_raw`).
  Filter on `filing.items` containing "2.02", skip 8-K/A, derive the `save_raw`
  suffix from the attachment's extension. **Verify the exact items/attachment
  filter against the installed API before writing it.**
- `src/financial_data_pull/cli.py` — `--export-csv` (zero network) and
  `--earnings-8k N` flags.
- `~/.pi/agent/skills/pull-financial-data/SKILL.md` + mirror to
  `~/dev/pi-dotfiles/home/skills/` (pi-dotfiles sync rule).
- `tests/test_pull_offline.py` (offline doubles, `_TempPlane`), `README.md`.

## Implementation units (ordered)

1. **CSV export.** `export_csv`: walk `store.snapshot_dirs(issuer)` newest→
   oldest; the first snapshot containing each table wins — snapshots lacking the
   table are skipped via the `KeyError` `read_verified_table` raises. Read via
   that same hash check; write `data/csv/<issuer>/<table>.csv` with
   `encoding="utf-8"` (note in README: `av_transcript` content is long quoted
   multi-line text); return `{table: snapshot_id}` so the CLI prints provenance.
   `--export-csv` makes no network call; exit 0.
2. **8-K acquisition.** `sec.earnings_8k`: fetch the ticker's 8-K filings, keep
   Item 2.02 ones (via `filing.items`; empty items → explicit MISSING row), skip
   8-K/A, take `count`. Save each Exhibit 99.1 (`Attachment.content` encoded
   UTF-8) with `save_raw(issuer, "sec", data, suffix=<actual extension>)`.
   Build the `sec_8k` DataFrame: ticker, filing_date, accession, items, exhibit
   file, exhibit path. Coverage: one row per filing
   (`dataset="sec_8k_<accession>"`, `period=filing_date`); none found → `MISSING`
   + `NOT_PUBLISHED`; fetch failure → `FAILED` + `NOT_RETRIEVABLE`. Wire into
   `pull()` behind `eight_ks=N`; extend `scope_key` (omitted when unset);
   manifests carry exhibit paths in `originals` and list `eight_ks`.
3. **CLI + docs.** The two flags; README table row for `sec_8k`, layout note for
   `data/csv/`.
4. **Skill.** One markdown: fresh pull → `pull("TICKER")` (+`refresh=True`);
   read-only → `cache_only=True` + CSVs (preferred over bare `read_table`, which
   reads only the newest snapshot — an 8-K-only run would KeyError) or an
   explicit `run_id`; documents → `sec_8k.csv` exhibit paths; note quotas
   (SEC ≈ 22 requests + ~1 per 8-K filing; `--earnings-8k` is a new scope and
   re-acquires the source set whole — use `--sources sec` for document-only
   pulls; Alpha Vantage free tier; transcripts opt-in).
5. **Tests.** Offline, in `test_pull_offline.py`: export picks the
   newest-carrying snapshot per table (the VRT "repair vs fa3741" shape: a later
   run holding only `av_earnings_estimates` must not hide `fa3741`'s
   statements); `--export-csv` provenance output; 8-K double → `sec_8k` rows +
   raw `.htm` on disk + scope-key separation (different `eight_ks` ≠ cache hit).

## Constraints

- Tests stay offline: double the SEC provider, wrap the store with
  `tests/test_store._TempPlane`.
- Do not touch `save_raw`, `commit_snapshot`, `read_verified_table` semantics.
- Cache has no TTL; freshness stays the caller's `refresh=True`.
- Keep `.env` and credentials out of artifacts (`store.clean_error` already
  covers exception text).

## Verification

- `bash tests/run_all.sh` — all three suites green.
- Offline tests added: golden scope-key stability (old inputs, unchanged hash);
  export picks the newest-carrying snapshot per table; `--export-csv`
  provenance; 8-K double → per-filing `sec_8k_<accession>` rows + raw exhibit on
  disk + scope separation.
- Manual live check (costs quota): `financial-data-pull VRT --earnings-8k 10
  --refresh`, then `--export-csv`, open one exhibit HTML in a browser.

## Success examples (acceptance criteria)

1. After `pull("VRT", cache_only=True)` and export: `data/csv/VRT/
   income_quarterly_0.csv` exists and matches the `fa3741` parquet — the later
   `repair` snapshot did not blank it.
2. `financial-data-pull VRT --export-csv` prints one `table ← snapshot-id` line
   per exported table; every printed snapshot contains that table's parquet.
3. `pull("VRT", eight_ks=10)` produces a `sec_8k` table in the snapshot with 10
   rows, each `exhibit_path` pointing to an existing file under
   `data/raw/VRT/sec/`; `--export-csv` then yields `sec_8k.csv`. A later
   `pull("VRT", eight_ks=5)` is a cache MISS (separate scope).
4. Golden scope-key check: `scope_key("VRT", "VRT", ["yahoo"], [])` returns the
   same hash it does today (old inputs, old key); adding `eight_ks=10` changes
   it, omitting it does not.

## Risks / dependencies

- edgartools' items/attachment filtering differs slightly per API version —
  verify against the installed 5.58 surface first (`filing.items` metadata and
  `Attachment.content` were confirmed present in the review).
- A sec refresh with 8-Ks costs ~1 request per filing (SGML fetch); the existing
  `--ceiling sec=N` counts them. First `--earnings-8k` run re-acquires the whole
  source set (new scope) — documented in README/skill.
- The skill lives in `~/.pi/agent/skills/` and mirrors to
  `~/dev/pi-dotfiles/home/skills/`; the repo copy is canonical, the sync rule in
  AGENTS.md covers the mirror (cannot be pinned by `run_all.sh`).
