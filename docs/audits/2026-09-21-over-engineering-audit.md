# Over-engineering audit — financial_data_pull

- **Date:** 2026-09-21
- **Scope:** whole repository (1,875 tracked lines; 1,320 in `src`)
- **Method:** ponytail-audit — over-engineering and complexity only. Correctness,
  security and performance were out of scope.
- **Baseline:** `bash tests/run_all.sh` green (3 offline suites) before and after
  the audit. Nothing below is a bug; every item is deletable complexity.
- **Applies nothing.** Findings only.

## Findings, biggest cut first

1. **`delete:` contracts.py's numeric bounds, `pattern`/`minLength`,
   `minProperties`, nested-object recursion, `minItems` and `unique_by` branches.**
   `coverage.json` uses only `required`, `type`, `enum`, `required_when` and array
   `items`; ~40 of the file's 164 lines are unreachable, and `import re` goes with
   them.
   `[src/financial_data_pull/contracts.py:10,71-73,89-106,132-139,144-154]`

2. **`yagni:` the packaged-schema mechanism.** `SCHEMA_DIR`'s packaged-copy branch
   plus the wheel `force-include` exist so "an installed copy can validate", but
   the only install is `pip install -e .` — no distribution is ever built.
   Replacement: `SCHEMA_DIR = _REPO_SCHEMAS`.
   `[src/financial_data_pull/contracts.py:14-22, pyproject.toml:33-36]`

3. **`shrink:` two identical scope-key snapshot scans.** The `cache_only` loop
   re-implements `_cache_lookup`'s body. Have `_cache_lookup` return
   `(snap, manifest)` and let both callers shape their own reply. −6 lines.
   `[src/financial_data_pull/pull.py:273-294, 325-332]`

4. **`shrink:` held-transcript reuse opens the same `snapshot.json`/coverage up to
   three times** (`held_transcript_quarters`, the inline originals lookup,
   `_held_transcript_segments`). Return `quarter -> (snap, original_path)` from one
   walk. −10 lines, two fewer I/O paths.
   `[src/financial_data_pull/pull.py:198-224, 411-417]`

5. **`shrink:` `_transcript_row` re-implements `_row`'s envelope.** Call
   `_row(..., {"period_of_report": quarter}, ...)` with the label passed through.
   −10 lines; behaviour-equivalent but changes the `label` wording and adds an
   `as_of` key (both contract-valid).
   `[src/financial_data_pull/pull.py:79-101, 179-196]`

6. **`delete:` `from . import providers  # noqa: F401`.** The next line's explicit
   submodule import already sets the package attributes (verified:
   `providers.sec is sec`), and nothing reads `providers.<x>`.
   `[src/financial_data_pull/pull.py:27]`

7. **`delete:` `dev = ["build"]` and its comment citing
   `scripts/check_dist_contents.py`,** a file this repository never copied.
   −1 dependency. `[pyproject.toml:20-24]`

8. **`shrink:` `_statements_from_sec`'s 20-F fallback rewrites `rows` with two list
   comprehensions to replace one dataset each.** A dict keyed by dataset removes
   both. `[src/financial_data_pull/pull.py:162,172]`

9. **`delete:` `DEFAULT_SOURCES = SOURCES`,** an alias with one caller.
   `[src/financial_data_pull/pull.py:39,313]`

10. **`delete:` `FRED_API_KEY=` in `.env.example`.** FRED is out of scope and no
    code reads it. `[.env.example:7]`

11. **`shrink:` `contracts.read_table(path)`** is a one-caller wrapper over
    `pd.read_parquet` whose lazy import buys nothing — pandas is a hard dependency
    that `pull.py` already imports at module scope.
    `[src/financial_data_pull/contracts.py:38-41,67]`

## Considered and left alone

- `safe_path`'s containment assertion — unreachable after the component regex, but
  it is the path-traversal guard.
- The `sdist` excludes, `save_raw`'s suffix regex, the atomic-publish and
  hash-gating paths — data-leak and data-loss guards.
- The five `if ceiling:` guards — `Ceiling` is always truthy, but the plan and
  `tests/test_ceiling.py` pin that contract.
- The three hand-rolled offline suites and `run_all.sh` — no pytest, no fixtures:
  already the lazy option.
- `docs/plans` and `docs/specs` — process artifacts, not code.

**net: −42 lines across nine files (97 added, 139 removed), −1 dependency, plus the
two superseded build documents deleted (823 lines).**

## Status — 2026-09-21

Applied: 1, 2, 3, 4, 6, 7, 9, 10, 11. Declined on merit: 5 (adds an `as_of` key
and rewrites the label text in every transcript coverage row) and 8 (saves ~1–2
lines on the 20-F acquisition path).

The project's upstream-copy convention was retired at the same time, so findings 1,
2 and 11 no longer carried a re-application cost and moved from "skipped" to
applied. The two build documents that recorded the copying
(`docs/plans/2026-09-21-financial-data-pull.md`,
`docs/specs/2026-09-21-financial-data-pull.md`) were deleted, and `README.md` and
`AGENTS.md` no longer reference the earlier project.

Line references above describe the files as audited, before these edits.
