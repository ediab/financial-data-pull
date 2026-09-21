# Plan — financial_data_pull

- **Date:** 2026-09-21
- **Original request:** "create a python library that pulls financial data for a ticker from EDGAR using Edgartools and from Alpha Vantage... copy the .env from ../equity-research and the way it pulls and caches data. The goal of this project is to ONLY do the data pull / caching."
- **Working directory:** `/Users/eliasdiab/Dev/financial_data_pull`
- **Git:** Git repository on `main`, remote `origin` =
  `git@github.com:ediab/financial-data-pull.git` (**private**). Created 2026-09-21
  with the spec, plan and `.gitignore` as the first commit (`63e9b8b`). `.env` is
  verified ignored. Push is a normal `git push` from here on.
- **Agreed brief:** `docs/specs/2026-09-21-financial-data-pull.md`
- **Source of truth for all copied code:** `/Users/eliasdiab/Dev/equity-research`

---

# Part 1 — For the owner (plain English)

## Goal

A library that fetches a ticker's financial data from SEC EDGAR, Alpha Vantage
and Yahoo, and stores it locally in a way that cannot be silently corrupted. One
call pulls; the next call for the same thing reads the cache. If something could
not be fetched, the result says so and says why — it never pretends the data is
missing or fills the gap with something else.

## What we're building

We copy equity-research's proven acquisition and storage code almost word for
word. It already does everything asked for: content-addressed originals, immutable
versioned snapshots, atomic publishing, hash-checked reads, and per-dataset status
with reasons.

Three things cannot be copied: the file that orchestrates a pull (because it is
built around an approval gate we are dropping), the storage helper's
snapshot-type filter (no longer needed), and the root-folder environment variable
name.

## Scope

**In:** SEC statements (3 annual + 8 quarterly filings × income/balance/cashflow),
Alpha Vantage earnings estimates and earnings-call transcripts, Yahoo prices and
the seven analyst datasets, the local cache, a thin CLI, and tests.

**Out:** approval gates and cases, the model, checks, valuation, delivery, memo,
workbooks, PDF parsing, company-document URL fetching, DuckDB, FRED, and macro
series.

## Meaningful choices

- **No approval gate.** A pull just pulls. You keep an optional request ceiling
  (`ceilings={"alpha_vantage": 10}`) so a refresh loop can't burn your Alpha
  Vantage quota — but the request counter is always tracked and reported either
  way.
- **Snapshots are per source set.** `pull("NVDA", sources=["yahoo"])` and
  `pull("NVDA", sources=["sec"])` produce separate snapshots you can refresh on
  different schedules, so a daily price update never re-fetches 11 SEC filings.
- **Alpha Vantage is not the price source.** It provides estimates and
  transcripts only. Prices come from Yahoo. This matches equity-research and is
  why Yahoo and yfinance are in scope despite your original description.
- **FRED dropped,** as you asked.

## Success looks like

1. Calling `pull("NVDA")` twice: the second call returns `CACHED` and makes
   **zero** network requests.
2. `pull("NVDA", sources=["yahoo"])` then `pull("NVDA", sources=["sec"])` gives
   two separate snapshots; the Yahoo one holds only the 8 `yahoo_*` tables, the
   SEC one only statements, and neither satisfies a request for the other.
3. `pull("ZZZZ")` (a bogus ticker) raises nothing; every dataset comes back
   `FAILED` or `MISSING` **with a reason**, and a valid coverage file is on disk.

## Recommended approach

Copy the store and provider layers verbatim (they are small and already
correct), then write a new ~200-line orchestration file with the approval
machinery removed. Verify with offline tests that monkeypatch the providers, so
no test ever touches the network. Finish with one opt-in live pull you run
yourself.

---

# Part 2 — For the executor

A fresh session with no memory should be able to follow this. Every path is
absolute-relative to `/Users/eliasdiab/Dev/financial_data_pull` unless stated
otherwise. `UPSTREAM` refers to `/Users/eliasdiab/Dev/equity-research`.

When copying, **change only what the unit says to change**. Do not "improve"
upstream code, do not reformat, do not add type annotations, do not rename
variables.

## Environment facts (verified, do not re-derive)

| Item | Value |
|---|---|
| Python | 3.10.13 |
| pandas | 2.3.3 |
| pyarrow | 25.0.1 |
| edgartools | 5.58.0 |
| yfinance | 1.7.0 |
| Upstream venv | `UPSTREAM/.venv/bin/python` |

`.env` keys present upstream: `EDGAR_IDENTITY`, `ALPHAVANTAGE_API_KEY`,
`FRED_API_KEY`. `FRED_API_KEY` is **not** needed here; leave the line in the
copied `.env` (harmless, and keeps the two files diffable) but do not read it.

## Unit 1 — Project skeleton and environment

**Files**

- `pyproject.toml` — mirror `UPSTREAM/pyproject.toml` with these changes:
  - `name = "financial-data-pull"`, `version = "0.1.0"`
  - `description` — one line about pulling and caching ticker data
  - `dependencies` — `pandas==2.3.3`, `pyarrow==25.0.1`, `edgartools==5.58.0`,
    `yfinance==1.7.0`. **Drop** `openpyxl`, `pypdf`. Keep the comment about
    pinning the Parquet engine.
  - `[project.scripts]` → `financial-data-pull = "financial_data_pull.cli:main"`
  - `[tool.hatch.build.targets.wheel] packages = ["src/financial_data_pull"]`
  - keep the `force-include` of `schemas` → `financial_data_pull/schemas`
  - `sdist` `exclude` list: keep `/data`, `/.env`, `/.venv`, `/tests`,
    `/docs`; drop the entries for directories that will not exist
    (`/documents`, `/assignments`, `/.pi`, `/references`, `/skills`,
    `/policies`, `/templates`, `/scripts`, `/licenses`, `/UPSTREAM.md`)
- `.env` — copy `UPSTREAM/.env` verbatim (live keys; gitignored)
- `.env.example` — copy `UPSTREAM/.env.example` verbatim
- `.gitignore` — **already written and committed** (do not recreate). It
  contains `data/`, `.env`, `.venv/`, `__pycache__/`, `*.pyc`, `.pytest_cache/`,
  `.DS_Store`. It is committed *before* the `.env` copy lands, which is the point:
  the live-key file must be ignored from the moment it exists.
- `schemas/coverage.json` — copy `UPSTREAM/schemas/coverage.json` verbatim
- `src/financial_data_pull/__init__.py` — new, small:

```python
from .pull import manifest, pull, read_table

__version__ = "0.1.0"
__all__ = ["__version__", "manifest", "pull", "read_table"]
```

  **Import eagerly, not lazily.** Upstream's `__init__.py` uses a lazy
  `__getattr__` to avoid importing pandas in processes that only want a version
  constant. Do not copy that here: the public name `pull` collides with the
  submodule `financial_data_pull.pull`. The first lazy access imports the
  submodule, and the import system then binds the *module* as an attribute on
  the package — so `financial_data_pull.pull` silently changes from the function
  to the module object after first use. Eager import has no such ambiguity, and
  pandas is a hard dependency of every useful call anyway.

**Steps**

```sh
cd /Users/eliasdiab/Dev/financial_data_pull
python3.10 -m venv .venv
.venv/bin/pip install -e .
```

**Done when:** `.venv/bin/python -c "import financial_data_pull; print(financial_data_pull.__version__)"`
prints `0.1.0`, and `.venv/bin/python -c "import pandas, pyarrow, edgar, yfinance"` succeeds.

**Do not:** create `data/`, `documents/`, `assignments/`, or a git repo.

## Unit 2 — Store layer

**`src/financial_data_pull/config.py`** — copy `UPSTREAM/src/equity_research/config.py`
(49 lines) verbatim, with two edits:

1. `os.environ.get("EQUITY_RESEARCH_ROOT")` → `os.environ.get("FINANCIAL_DATA_PULL_ROOT")`
2. The docstring line naming `EQUITY_RESEARCH_ROOT` → `FINANCIAL_DATA_PULL_ROOT`

Leave `ENV_PATH = ROOT / ".env"`, `_parse_env_file` and `get()` untouched.
`_resolve_root()` needs no change: `config.py` sits at the same depth
(`src/<pkg>/config.py`), so the `pyproject.toml` check still finds the repo root.

**`src/financial_data_pull/store.py`** — copy `UPSTREAM/src/equity_research/store.py`
(195 lines), then apply exactly these six removals:

1. Delete `DOCUMENTS = ROOT / "documents"`.
2. Delete the `snapshot_kind()` function entirely.
3. `snapshot_dirs(issuer, kind=None)` → `snapshot_dirs(issuer)`. Drop the `kind`
   filter and the paragraph in its docstring about the statements/benchmark
   split. Keep the paragraph explaining that staging directories are hidden by
   name. The returned list stays `sorted(...)`.
4. `latest_snapshot_dir(issuer, kind=None)` → `latest_snapshot_dir(issuer)`.
5. Delete `write_coverage()` (the new `pull.py` writes coverage the way
   upstream's `_publish_snapshot` does, and validates earlier).
6. In the module docstring, update the layout comment and drop the
   `references/data-contract.md, policies/sources.md` citation and the DuckDB
   line.

Keep verbatim, unchanged: `safe_component`, `safe_path`, `now_iso`,
`sha256_bytes`, `sha256_file`, `atomic_write_json`, `save_raw`, `staging_dir`,
`commit_snapshot`, `snapshot_by_id`, `write_snapshot_manifest`, `coverage_path`,
and the `DATA`/`RAW`/`TABLES`/`COVERAGE` constants.

Also change the line-24 comment `# EQUITY_RESEARCH_ROOT-aware` to
`# FINANCIAL_DATA_PULL_ROOT-aware`.

**`src/financial_data_pull/contracts.py`** — copy
`UPSTREAM/src/equity_research/contracts.py` (164 lines) **verbatim, no edits**.
It is self-contained: it imports only stdlib, plus `pandas` and `store` lazily
inside functions.

**`src/financial_data_pull/providers/__init__.py`** — empty file, as upstream.

**Done when:** `.venv/bin/python -c "import sys; sys.path.insert(0,'src'); from financial_data_pull import config, store, contracts; print(config.ROOT)"`
prints the project root and creates no directories.

**Do not:** touch `safe_path`, the suffix regex in `save_raw`, or the hash
re-verification in `save_raw`. Those are the integrity guarantees.

## Unit 3 — Providers

Copy these three verbatim from `UPSTREAM/src/equity_research/providers/`.
**Keep the code identical; edit only the module docstrings** to remove stale
approval language.

1. **`sec.py`** (78 lines). Docstring: replace "The ONLY module allowed to open
   network connections to SEC. Runs after G0 approval (source-plan.json +
   approvals.json). Uses EDGAR_IDENTITY from .env." with a line saying it is the
   only module that opens network connections to SEC and needs `EDGAR_IDENTITY`
   from `.env`. Keep everything else: `STATEMENT_KINDS`, `_identity()`,
   `statements()`, the 20-F-aware behaviour, the full-text original preservation.
2. **`yahoo.py`** (119 lines). Docstring: drop "Approved acquisition layer —" and
   the sentence about the model seal. Keep all of it: `DATASETS`, `_PAYLOAD_KIND`,
   `_meta`, `fetch`, the eight `grab(...)` calls, the `PermissionError` re-raise,
   and the `yahoo_` table prefix.
3. **`alphavantage.py`** (139 lines). Docstring: drop "(owner-approved extension,
   2026-09-19)" and "Snapshot-only second source for consensus cross-check".
   Keep `earnings_estimates`, `earnings_call_transcript`, `_transcript_meta`, and
   the rate-limit detection verbatim.

**Do not** copy `UPSTREAM/src/equity_research/providers/fred.py` — FRED is out
of scope.

**Do not** copy `company_documents.py`.

**Done when:**
```sh
.venv/bin/python -c "import sys; sys.path.insert(0,'src'); from financial_data_pull.providers import sec, yahoo, alphavantage; print(sec.STATEMENT_KINDS, len(yahoo.DATASETS))"
```
prints `{'income': 'income_statement', 'balance': 'balance_sheet', 'cashflow': 'cash_flow_statement'} 8`.

## Unit 4 — `pull.py` (the only genuinely new code)

New file `src/financial_data_pull/pull.py`, roughly 200 lines. Assemble it from
named pieces of `UPSTREAM/src/equity_research/pull.py`, which are individually
reusable. Read that file first (698 lines).

### 4a. Copy verbatim from upstream `pull.py`

- All module constants: `RESERVED_COLUMNS`, `RUN_STATUSES`, `ANNUAL_FILINGS = 3`,
  `QUARTERLY_FILINGS = 8`, `STATEMENT_PREFIXES`,
  `TRANSCRIPT_MIN_INTERVAL_SECONDS = 1.5`, `TRANSCRIPT_RATE_LIMIT_RETRIES = 3`,
  `PERIOD_COLUMN`, `POINT_IN_TIME`, plus a new
  `SOURCES = ("sec", "alpha_vantage", "yahoo")` and
  `DEFAULT_SOURCES = SOURCES`.
- `_clean_error`, `_period_labels`, `_row`, `_statements_from_sec`
  (all verbatim — `_statements_from_sec` takes `ceiling`, which still exists)
- `_transcript_row` and `_held_transcript_segments` verbatim.
- `held_transcript_quarters` with one adaptation: its body calls
  `store.snapshot_dirs(issuer, kind=TRANSCRIPT_KIND)`, and the `kind` parameter
  no longer exists (Unit 2 removes it). Walk all snapshots
  (`store.snapshot_dirs(issuer)`) and, per coverage row, additionally require
  `row["dataset"].startswith("av_transcript_")` before recording the quarter.
- The list comprehensions that build the `provider_meta`/coverage rows for Yahoo
  and Alpha Vantage inside upstream's `_benchmark_run`
- The body of upstream's `transcripts()` acquisition loop (from `spacing = ...`
  through `tables = {...}`), minus every `approvals.*` call. That region alone
  does not compile — it references names defined earlier in `transcripts()`.
  Define them in `pull()` before the loop:
  `wanted = [q for q in (transcripts or []) if q]`,
  `held = {} if refresh else held_transcript_quarters(issuer)`,
  `live_calls = 0`, `segments: list[dict] = []`, `missing: list[str] = []`.
  Drop upstream's all-quarters-cached early return and its PROPOSAL branches —
  the whole-snapshot scope key (4c) already returns CACHED for a fully-held,
  previously-pulled scope, and a new scope that happens to reuse held quarters
  should still publish its own snapshot (with zero live calls).
- The whole of upstream's `_publish_snapshot`, minus the `case`, `plan` and
  `kind` parameters, and minus its `approvals.set_pin` / `approvals.update_run`
  blocks

Delete from the copied code: `is_benchmark_table`, `BENCHMARK_PREFIXES`,
`LEGACY_ANALYST_TABLES`, `STATEMENTS_KIND`/`BENCHMARK_KIND`/`TRANSCRIPT_KIND`,
`_cache_lookup` (rewritten), `fetch_documents`, `run`, `_benchmark_run`,
`transcripts` (both replaced by the new `pull`), and `_statements_from_sec`'s
20-F block stays.

### 4b. New: `Ceiling`

```python
class Ceiling:
    """Counts provider requests; enforces only the limits that were named."""
    def __init__(self, limits: dict[str, int]):
        self.limits = dict(limits)
        self.used: dict[str, int] = {}

    def spend(self, provider: str, n: int = 1) -> None:
        self.used[provider] = self.used.get(provider, 0) + n
        limit = self.limits.get(provider)
        if limit is not None and self.used[provider] > limit:
            raise PermissionError(
                f"{provider} request ceiling exceeded: {self.used[provider]} > {limit}")
```

This is upstream's `approvals.Ceiling` (line 502) with one deliberate change:
an unnamed provider is **counted but not capped**. Upstream raises on unnamed
providers because that meant "not in the approved plan"; here there is no plan,
and the counter must always run so `pull()` can report request counts.

The providers call `ceiling.spend("sec")`, `"yahoo"`, `"alpha_vantage"`, and
`"alpha_vantage_transcripts"`. Always pass a real `Ceiling`, never `None`, so
round-trips are counted. `Ceiling` is truthy, so the providers' `if ceiling:`
guards behave as before.

### 4c. New: scope key and cache lookup

```python
def scope_key(issuer: str, sources, transcripts) -> str:
    """Identity of what a run acquires. Ceilings are excluded on purpose: a
    tighter cap must not make already-held evidence look like a cache miss."""
    scope = {"issuer": issuer, "sources": sorted(set(sources)),
             "transcripts": sorted(transcripts or [])}
    return hashlib.sha256(json.dumps(scope, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()
```

`_cache_lookup(issuer, scope_key)` walks `reversed(store.snapshot_dirs(issuer))`,
reads each `snapshot.json`, and returns the upstream-shaped `CACHED` dict for the
first manifest whose `scope_key` matches. Return `None` if none matches.

### 4d. New: public functions

```python
def pull(ticker, *, issuer=None, sources=None, transcripts=None,
         cache_only=False, refresh=False, ceilings=None) -> dict
```

Behaviour, in order:

1. `sources = list(sources or DEFAULT_SOURCES)`; raise `ValueError` for any
   source not in `SOURCES`. Raise `ValueError` if `cache_only and refresh`.
   Raise `ValueError` if `transcripts` is passed without `"alpha_vantage" in
   sources` — transcripts are an Alpha Vantage endpoint and cannot be pulled
   from the other sources.
2. `issuer = issuer or ticker`; `store.safe_component(issuer, "issuer")`.
3. `key = scope_key(issuer, sources, transcripts)`.
4. **cache_only:** return the newest matching snapshot's manifest with
   `{"status": "CACHED", "cache_only": True, ...}`, or
   `{"status": "MISSING: NOT_RETRIEVED", "issuer": issuer}` if none. Newest =
   last in `snapshot_dirs` sort order, the same tie-break
   `latest_snapshot_dir` uses; repeated refreshes can leave several snapshots
   sharing one scope key. Never touches the network.
5. **not refresh:** run `_cache_lookup`; if it hits, return
   `{**hit, "status": "CACHED"}` with zero network calls.

   **The cache contract, stated exactly.** The scope key is
   `(issuer, sorted(sources), sorted(transcripts))` — it contains **no date**, by
   design. So `pull("NVDA")` today and `pull("NVDA")` tomorrow return the same
   scope key, and tomorrow's call is `CACHED` with zero network requests. A
   second call is only a network call when the scope key differs or `refresh=True`.

   **Consequence to be explicit about: the cache never expires on its own.** It
   has no TTL. A snapshot from a year ago is still returned as `CACHED` until the
   caller passes `refresh=True`. That is what was asked for ("pull today, don't
   pull again tomorrow"), and the caller controls freshness explicitly. Do **not**
   add an implicit age-based expiry; if an age rule is ever wanted, add an
   explicit opt-in `max_age_days=` parameter, which is a separate decision.
6. `ceiling = Ceiling(ceilings or {})`,
   `run_id = f"{store.now_iso().replace(':', '')}-{uuid.uuid4().hex[:6]}"`,
   `retrieved_at = store.now_iso()`.
7. Acquire each requested source, **each wrapped so one provider's failure
   becomes coverage rows rather than an exception**, and with `PermissionError`
   re-raised (a ceiling breach is not a data problem) — **except** in the
   transcript loop, which keeps upstream's behaviour: a `PermissionError` from
   `earnings_call_transcript` is recorded as a `MISSING` coverage row for that
   quarter and the loop continues, so one capped or throttled quarter cannot
   discard the rest. A ceiling on `alpha_vantage_transcripts` therefore
   surfaces as MISSING rows, never an exception; `sec`, `yahoo` and
   `alpha_vantage` (estimates) breaches do raise. Acquire in this order so
   the partial-failure contract is easiest to reason about: `sec`, then `yahoo`,
   then `alpha_vantage`, then transcripts. Record every dataset's status in
   `rows`, its frame in `tables`, its metadata in `provider_meta`, and any
   preserved original in `originals`.
8. Validate the coverage document with
   `contracts.validate_against(doc, "coverage")` **before publishing anything**.
   On violation return `{"status": "CONTRACT_VIOLATION", "violations": [...]}`
   and write nothing.
9. Call the adapted `_publish_snapshot(...)`, then return its result extended
   with `"requests": ceiling.used`.

```python
def manifest(ticker, *, issuer=None, run_id=None) -> dict
def read_table(ticker, table, *, issuer=None, run_id=None)
```

- `manifest` returns the `snapshot.json` of the given `run_id`, or of the newest
  published snapshot, or raises `FileNotFoundError` naming the ticker.
- `read_table` returns the frame via `contracts.read_verified_table`, which
  **raises `ValueError` when the file on disk no longer hashes to the value
  recorded in the snapshot manifest** — a corrupted or edited table is refused,
  never returned as if it were the evidence that was published. It also raises
  `KeyError` for a table not in the snapshot, and `FileNotFoundError` when the
  snapshot or the ticker does not exist. (Owner requirement 2026-09-21: a
  mismatch must produce an error, not silently wrong numbers.)

These two are the read half of "pull and cache" (owner requirement
2026-09-21: keep both).

### 4e. `_publish_snapshot` manifest changes

Upstream writes `{"issuer", "run_id", "kind", "plan_hash", "scope_key",
"retrieved_at", "providers", "originals", "table_hashes", "coverage_path"}`.
The new manifest drops `kind`, `plan_hash`, and adds:

```python
"ticker": ticker,
"sources": sorted(sources),
"transcripts": sorted(transcripts or []),
```

`store.write_snapshot_manifest` still stamps `published_at`. Keep the staging →
manifest-inside-staging → `commit_snapshot` rename order exactly as upstream has
it; that ordering is what makes a crash unable to expose a partial snapshot.
Write coverage after the rename with `atomic_write_json` to
`store.coverage_path(issuer, run_id)`, as upstream does.

Return:
```python
{"ticker", "issuer", "status": "RETRIEVED", "snapshot_dir", "coverage_path",
 "scope_key", "sources", "transcripts", "table_hashes",
 "statuses": {dataset: acquisition}, "open_gaps": [...]}
```

**Done when:** `pull("NVDA", cache_only=True)` on an empty `data/` returns
`{"status": "MISSING: NOT_RETRIEVED", ...}` and creates no files.

**Do not:** add a retry framework, logging configuration, async, a provider
plugin registry, or a config file. The providers already degrade correctly.

## Unit 5 — CLI and tests

### CLI

`src/financial_data_pull/cli.py`, ~60 lines, mirroring
`UPSTREAM/cli.py`'s `pull` subparser (line 40) minus the case/approval flags.

```
financial-data-pull TICKER [--issuer NAME] [--sources sec,yahoo]
                          [--transcripts 2025Q1,2025Q2] [--refresh]
                          [--cache-only] [--ceiling alpha_vantage=10,sec=40]
```

`--sources` splits on comma and validates against `pull.SOURCES`;
`--ceiling` parses `provider=n` pairs. Print the returned dict as indented JSON
and exit `0`. Exit `1` on `ValueError` or `PermissionError` with the message on
stderr. No other subcommands.

### Tests

Three files, all offline, all plain `assert` scripts run with
`.venv/bin/python tests/<file>.py`, matching upstream's style (no pytest). Each
starts with
`sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))`.

1. **`tests/test_store.py`** — copy the `_TempPlane` pattern from
   `UPSTREAM/tests/test_g0_and_store.py` (lines 23-40), **minus** the
   `approvals.ASSIGNMENTS` and `store.DOCUMENTS` patches (neither exists here:
   the approvals module is gone and Unit 2 deletes `DOCUMENTS`). Cover:
   - a `.staging-*` directory is invisible to `snapshot_dirs()` and to
     `latest_snapshot_dir()`
   - `commit_snapshot` refuses when `snapshot.json` is absent, and refuses a
     duplicate `run_id`
   - `read_verified_table` raises `ValueError` after a table file is truncated
   - `safe_component` rejects `..`, `/`, and empty strings
   - `save_raw` returns the same path for the same bytes and re-verifies rather
     than trusting an existing file
2. **`tests/test_pull_offline.py`** — monkeypatch
   `financial_data_pull.providers.sec.statements`,
   `.yahoo.fetch` and `.alphavantage.earnings_estimates` /
   `.earnings_call_transcript` with fakes returning small frames. Borrow the
   `_http` monkeypatch helper from `UPSTREAM/tests/test_transcripts.py` (line 25)
   for the Alpha Vantage path. Cover all three success examples:
   - a second identical `pull()` returns `CACHED` and the provider fakes record
     **zero** additional calls
   - `sources=["yahoo"]` then `sources=["sec"]` produce two snapshot directories;
     each `pull()` reports only its own tables in `statuses`, and neither is a
     cache hit for the other
   - a provider raising `Exception` yields no exception from `pull()`; the
     affected datasets are `FAILED` with reason `NOT_RETRIEVABLE`, the others are
     `RETRIEVED`, and the coverage document passes `validate_against`
   - `refresh=True` creates a second snapshot directory and leaves the first
     byte-identical
   - a transcript quarter the provider reports as `MISSING`/`PREMIUM_OR_UNCOVERED`
     produces a coverage row with that reason, and an empty transcript is
     `MISSING`, never a silent success
3. **`tests/test_ceiling.py`** — a named ceiling raises `PermissionError` on the
   call that exceeds it; an unnamed provider is counted in `requests` and is not
   capped; `Ceiling` is truthy. Scope the raise test to `sec`, `yahoo` and
   `alpha_vantage` — a transcript ceiling breach is recorded as `MISSING` rows,
   not raised (see Unit 4 step 7).

`tests/run_all.sh` — copy `UPSTREAM/tests/run_all.sh` structure, reduced to the
three files above, `set -euo pipefail`, `PY="${PY:-.venv/bin/python}"`, no
network and no Excel. Add `echo "== ..."` header lines per suite in the same
style.

**Done when:** `bash tests/run_all.sh` exits `0`, and every test file also
passes when run directly.

## Unit 6 — First live pull (opt-in, run by the owner)

Not part of the automated tests. Run only when the owner says go, because it
spends real API quota and takes minutes.

```sh
.venv/bin/python -m financial_data_pull.cli NVDA --sources yahoo
.venv/bin/python -m financial_data_pull.cli NVDA --sources sec
.venv/bin/python -m financial_data_pull.cli NVDA --sources alpha_vantage
```

Expected: 8 tables in the first snapshot (`len(yahoo.DATASETS)`), 33 in the
second, 1 in the third.
Then re-run each with no flags and confirm `status` is `CACHED`.

---

## Tests / verification summary

| Check | Command | Pass condition |
|---|---|---|
| Install | `.venv/bin/pip install -e .` | exit 0 |
| Import surface | `python -c "import financial_data_pull; print(financial_data_pull.__version__)"` | prints `0.1.0`; pandas loads too (eager import, see Unit 1) |
| Store integrity | `python tests/test_store.py` | exit 0 |
| Pull behaviour | `python tests/test_pull_offline.py` | exit 0 |
| Ceiling | `python tests/test_ceiling.py` | exit 0 |
| Full offline suite | `bash tests/run_all.sh` | exit 0 |
| No network in tests | run `tests/run_all.sh` with the network blocked | exit 0 |
| Live (optional) | Unit 6 commands | tables as listed, second run `CACHED` |

Also confirm `git status`-equivalence by inspection: no `data/`, `.env`, or
`.venv/` path is referenced in `pyproject.toml`'s wheel contents.

## Material risks and dependencies

1. **`yfinance` is unofficial.** Yahoo changes its endpoints without notice, and
   yfinance can break outright. The upstream design already contains this: a
   yfinance failure degrades to `FAILED` coverage rows rather than an exception.
   Do not add a fallback price source. **Owner decision 2026-09-21: the Yahoo
   data is not essential, so if yfinance stops working it stops working** — the
   library reports the datasets `FAILED` with a reason and everything else still
   succeeds. Migration path if that day comes: **OpenBB** is already installed in
   `UPSTREAM/.venv` with `openbb-yfinance`, `openbb-tiingo`, `openbb-fmp`,
   `openbb-intrinio` and `openbb-sec` adapters, so a price provider can be
   swapped behind one interface without redesigning this library. Do not act on
   this now. Note for the record: Stooq, the commonly cited no-key alternative,
   was checked and now serves a JavaScript browser challenge instead of CSV, so
   it is not a drop-in replacement.
2. **Alpha Vantage's free tier is severely rate-limited.** The default
   `pull(ticker)` costs **exactly one** Alpha Vantage request (earnings
   estimates). Transcripts cost one request per quarter plus up to 3 retries at
   1.5s+backoff spacing, and are **opt-in only** — they are never acquired as
   part of a default pull. A wide transcript pull can exhaust a daily quota; the
   ceiling parameter exists for this.

   **Alpha Vantage must never be used to substitute for Yahoo data.** No
   `TIME_SERIES_DAILY` call, no AV prices, no AV fundamentals, no AV fallback
   when a Yahoo dataset fails (owner decision 2026-09-21). Alpha Vantage is a
   source for two datasets — `EARNINGS_ESTIMATES` and
   `EARNINGS_CALL_TRANSCRIPT` — and nothing else. Spending AV quota to paper over
   a Yahoo failure is explicitly out of scope.
3. **A full `pull(ticker)` is slow.** 11 EDGAR filings, 11 full-text submissions,
   8 Yahoo datasets, 1 AV request — minutes, not seconds. Tests must never do
   this; only Unit 6 does, deliberately.
4. **`EDGAR_IDENTITY` is mandatory for SEC.** `sec._identity()` raises
   `RuntimeError` without it. The copied `.env` supplies it.
5. **Pinned versions are load-bearing.** Upstream pins pandas/pyarrow because
   Parquet round-tripping is a correctness risk, not an implementation detail.
   Keep the pins; do not let a resolver float them.
6. **Version control is done.** The repo exists and is private; the spec, plan
   and `.gitignore` are committed and pushed. Unit 1 onward is ordinary work on
   `main`. Do not force-push, rewrite history, or change the remote.
7. **`.env` holds live API keys.** It must never be committed, packaged, or
   printed. `security` is not a concern beyond this, but the `.gitignore` and the
   sdist `exclude` list are the guardrails — verify both exist in Unit 1.

## Explicitly out of scope

Approval gates (G0–G4), cases, assignments, proposals, source plans, the model,
checks, valuation, delivery, memo, workbooks, Excel recalculation, locks, PDF
extraction, document intake, company-document URL fetching, DuckDB, FRED, macro
series, any new provider, and any change to the Git remote.
