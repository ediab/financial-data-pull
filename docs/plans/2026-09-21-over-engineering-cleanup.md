# Plan — apply the over-engineering audit

- **Date:** 2026-09-21
- **Original request:** "how to implement the findings of docs/audits/2026-09-21-over-engineering-audit.md"
- **Working directory:** `/Users/eliasdiab/Dev/financial_data_pull`
- **Git:** Git repository on `main` (verified with `git branch --show-current`).
- **Baseline:** `bash tests/run_all.sh` green — three offline suites, no network.

---

# Part 1 — For the owner

## Goal

Remove the complexity found by the audit, without changing what the library pulls,
stores or reports. Nine of the eleven findings are deletions in `pull.py`,
`pyproject.toml` and `.env.example`.

## Scope, after the copy convention was retired

The upstream-copy convention is gone (Unit 7): `config.py`, `store.py`,
`contracts.py` and `providers/*` are ordinary files of this repository, so findings
1, 2 and 11 carry no re-application cost and are now Unit 6.

Findings 5 and 8 stay declined on merit: 5 saves ~9 lines but adds an `as_of` key
to every transcript coverage row (a published evidence document), and 8 saves ~1–2
lines while touching the 20-F acquisition path.

## Recommended approach

Four small edits, each verified by the suite that already covers that path, plus
one assertion for the only path with no test today: a held transcript's preserved
original being carried into a new snapshot.

## What success looks like

1. `bash tests/run_all.sh` ends with the same three `all … passed` lines; no test
   weakened or deleted.
2. `.venv/bin/financial-data-pull VRT --sources yahoo --cache-only` (the source set
   the local snapshots were acquired for) still prints `"status": "CACHED"` with
   `cache_only`, `snapshot`, `snapshot_dir`; `ZZZZ --cache-only` still prints
   `"MISSING: NOT_RETRIEVED"`. Without `--sources` the scope is the full source
   set, which was never pulled locally, so `MISSING` there is correct.
3. Removing the single line that carries a held quarter's original into a new
   snapshot makes the suite fail; with it present, the new snapshot names the same
   original file as the first.

---

# Part 2 — For the executor

## Unit 1 — dead names in `pull.py` (findings 6, 9)

No behaviour change.

1. Delete line 27, `from . import providers  # noqa: F401 — imports the package so
   submodule attrs exist`. The next line,
   `from .providers import alphavantage, sec, yahoo`, already binds the submodules
   and sets the package attributes (`providers.sec is sec` was verified), and
   nothing in the file reads `providers.<x>`.
2. Delete line 39, `DEFAULT_SOURCES = SOURCES`. At line 313 change
   `sources = list(sources or DEFAULT_SOURCES)` to `sources = list(sources or SOURCES)`.
   `DEFAULT_SOURCES` has no other reference — not in `cli.py`, `__init__.py`,
   tests or README.

**Done when** `grep -n "DEFAULT_SOURCES\|from . import providers"
src/financial_data_pull/pull.py` prints nothing and the suite is green.

## Unit 2 — one snapshot scan instead of two (finding 3)

`[src/financial_data_pull/pull.py:273-294, 325-332]`

1. `_cache_lookup(issuer, key)` becomes the single scanner: return
   `(snap, manifest)` for the first manifest whose `scope_key` matches, else
   `None`. Keep its docstring, keep it private — its only caller is `pull()`
   (line 334).
2. `pull()`'s `cache_only` branch calls it and returns
   `{"issuer", "cache_only": True, "status": "CACHED", "snapshot_dir", "snapshot"}`
   on a hit and `{"status": "MISSING: NOT_RETRIEVED", "issuer"}` on a miss — keys
   and strings exactly as today. README documents the miss shape;
   `tests/test_pull_offline.py` asserts the hit shape.
3. `pull()`'s `not refresh` branch builds the cached reply it returns today from
   the same tuple: move the reply dict literal from lines 283-293 into the caller
   **unchanged**, `note` string included. Only the return type of `_cache_lookup`
   changes to `tuple[Path, dict] | None`; its dict body must not be rewritten.

**Done when** the suite is green, including
`test_a_repeat_pull_is_cached_with_zero_provider_calls`.

## Unit 3 — read a held quarter's snapshot once (finding 4)

`[src/financial_data_pull/pull.py:198-224, 395-417]`

1. `held_transcript_quarters` returns `dict[str, tuple[str, str | None]]`:
   quarter → (snapshot dir, that snapshot's preserved original for
   `av_transcript_<quarter>` or `None`). The manifest it already reads is the
   source; no extra file reads.
2. In `pull()`, the cached-quarter branch unpacks as
   `snap_dir, original = held[quarter]` and deletes lines 411-417 wholesale — the
   inline manifest re-read and its `if held_manifest_path.is_file():` guard. The
   guard is replaced by `if original:`, so a snapshot with no recorded original
   behaves as it does today. Keep `_held_transcript_segments(snap_dir, quarter)`
   and `provider_meta[...]["snapshot"] = Path(snap_dir).name` unchanged.
3. Add the missing guard to
   `tests/test_pull_offline.py::test_a_cached_quarter_is_reused_and_the_rest_is_acquired`:
   read each snapshot's manifest with
   `json.loads((Path(result["snapshot_dir"]) / "snapshot.json").read_text())` —
   not `manifest("NVDA")`, which resolves to the newest run — and assert the new
   one's `originals["av_transcript_2025Q1"]` equals the first one's, and that the
   file exists. Nothing asserts the carry-forward today.

**Done when** the suite is green, and commenting out the carry line makes the new
assertion fail (restore it afterwards).

## Unit 4 — two stale deletions (findings 7, 10)

1. `pyproject.toml`: delete the whole `[project.optional-dependencies]` block
   (lines 20-24), including the comment citing `scripts/check_dist_contents.py`,
   a file this repository never copied. Keep `[project.scripts]`, `[build-system]`
   and the hatch sections. Nothing references `.[dev]` anywhere.
2. `.env.example`: delete lines 6-7 — the FRED comment and `FRED_API_KEY=`. Keep
   `EDGAR_IDENTITY` and `ALPHAVANTAGE_API_KEY`.

**Done when** `grep -n "optional-dependencies\|build\b" pyproject.toml` shows only
build-system lines, and no runtime dependency changed (no reinstall needed).

## Unit 5 — record the outcome in the audit doc

1. Append a `## Status` section to
   `docs/audits/2026-09-21-over-engineering-audit.md`: applied 1, 2, 3, 4, 6, 7, 9,
   10, 11; declined on merit 5 and 8; the copy convention retired and the two build
   documents deleted.
2. Replace the closing net line with the real figure from `git diff --stat`, and
   note that the new documents are untracked.

## Unit 6 — trim `contracts.py` and drop the packaged-schema mechanism (findings 1, 2, 11)

1. Remove the validator branches unreachable with the single coverage contract:
   numeric `minimum`/`maximum`, string `pattern`/`minLength`, `minProperties`,
   nested-object recursion, `minItems`, `unique_by`, and the multi-type/nullable
   handling in `_type_ok` — a declared type is one name, and anything else is left
   unchecked. `import re` goes with them.
2. `SCHEMA_DIR` becomes the repository-root path only: delete the packaged-copy
   branch, and the wheel `force-include` plus its comment from `pyproject.toml`.
3. Inline `read_table` into `read_verified_table` (`pd.read_parquet`), dropping the
   lazy import and the one-caller wrapper.
4. Add the missing negative test: `tests/test_store.py` must show the contract still
   rejects a missing required key, an explicit null, a bad enum and a
   `required_when` gap — the trim's only new risk.

**Done when** the suite is green including that test, and
`grep -n "import re\|unique_by\|minItems" src/financial_data_pull/contracts.py`
prints nothing.

## Unit 7 — retire the upstream references

1. `AGENTS.md`: point "Read first" at `README.md` and `tests/run_all.sh` instead of
   the two deleted documents, and delete the "Copied code stays in step with
   upstream" bullet.
2. `README.md`: rewrite the two sentences naming the earlier project — the scope
   paragraph and the closing line of "Out of scope".
3. Delete `docs/plans/2026-09-21-financial-data-pull.md` and
   `docs/specs/2026-09-21-financial-data-pull.md` (823 lines describing the copy
   step) and the now-empty `docs/specs/`.

**Done when** `grep -rniE "equity[-_]research" . --exclude-dir=.git --exclude-dir=.venv`
prints nothing.

## Do not touch

`store.py`, `providers/*`, `cli.py`, `schemas/coverage.json`, `tests/run_all.sh`,
`test_ceiling.py`. The hash-gating, atomic-publish and path-containment code is the
product — only the files named in the units above change.

## Verification

- `bash tests/run_all.sh` — the whole check.
- `git diff --stat` — expect `pull.py`, `contracts.py`, `test_pull_offline.py`,
  `test_store.py`, `pyproject.toml`, `.env.example`, `README.md`, `AGENTS.md`, plus
  the two deleted documents.
- The Unit 3 red-check, restored afterwards.
- Optional, read-only: `.venv/bin/financial-data-pull VRT --cache-only` and
  `.venv/bin/financial-data-pull ZZZZ --cache-only`. Never run a pull without
  `--cache-only`.

## Risks and dependencies

- Units 2 and 3 sit on the cache-hit and evidence-carry paths, and the dicts they
  return are user-visible — hence the exact-keys constraint and the new assertion.
- `_cache_lookup`, `held_transcript_quarters` and `_transcript_row` are
  file-private with no external callers, so their shapes are free to change.
- No dependencies, no migrations, no destructive steps.
