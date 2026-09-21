"""Pull orchestration: one run = one snapshot + one coverage record.

A run acquires the sources it was asked for and publishes them as one snapshot,
scoped to (issuer, source set, transcript quarters). A snapshot becomes visible
only after its manifest is written inside it and the directory is renamed into
place, so a crash cannot leave a half-written snapshot that a cache-only read
would trust. Coverage rows report what was actually fetched — never a hardcoded
period claim — and are validated before they are published.

A plain run is cache-first: a snapshot already held for the same scope is
returned with zero network calls, and fresher data is an explicit `refresh`.
The scope key carries no date, so the cache never expires on its own — the
caller controls freshness.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from pathlib import Path

import pandas as pd

from . import contracts, store
from . import providers  # noqa: F401 — imports the package so submodule attrs exist
from .providers import alphavantage, sec, yahoo  # explicit submodule handles

RESERVED_COLUMNS = {"concept", "label", "dimension"}
RUN_STATUSES = ("MISSING", "FAILED", "RATE_LIMITED", "PARSE_FAILED")
# how much filing history one acquisition covers (master plan §4 data baseline)
ANNUAL_FILINGS = 3
QUARTERLY_FILINGS = 8
STATEMENT_PREFIXES = ("income_", "balance_", "cashflow_")
# the source sets a snapshot can be scoped to; a snapshot satisfies only the
# scope it was acquired for, so each may be refreshed on its own schedule
SOURCES = ("sec", "alpha_vantage", "yahoo")
DEFAULT_SOURCES = SOURCES

# Alpha Vantage's free tier allows roughly one request per second and returns a
# rate-limit message rather than an HTTP error when a burst exceeds it. Transcripts
# are acquired one quarter at a time with a minimum spacing and a bounded retry, so a
# throttled quarter is retried rather than permanently recorded as missing.
TRANSCRIPT_MIN_INTERVAL_SECONDS = 1.5
TRANSCRIPT_RATE_LIMIT_RETRIES = 3
# An XBRL column is a period only when it looks like one; frame structure columns
# (standard_concept, level, abstract, is_breakdown, …) are not periods.
PERIOD_COLUMN = re.compile(r"^\d{4}-\d{2}-\d{2} \((Q\d|FY|YTD)\)$")
POINT_IN_TIME = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _clean_error(exc: BaseException, limit: int = 80) -> str:
    """Exception text is untrusted input: keep the type and a short, flat hint.

    Truncating is not sanitising, so anything that looks like a credential or a
    full URL is redacted before the text can reach a manifest.
    """
    message = re.sub(r"\s+", " ", str(exc)).strip()
    message = re.sub(r"(?i)(api_?key|apikey|token|secret)=\S+", r"\1=<redacted>", message)
    message = re.sub(r"https?://\S+", "<url>", message)
    return f"{type(exc).__name__}: {message[:limit]}" if message else type(exc).__name__


def _period_labels(df: pd.DataFrame | None, dataset: str = "") -> list[str]:
    """The period labels a frame actually carries.

    Balance sheets use bare period-end dates (a stock is observed at an instant);
    flows use `(Qn)`/`(YTD)`/`(FY)` tags. Reporting one as the other would claim
    periods the filing does not state.
    """
    if df is None:
        return []
    pattern = POINT_IN_TIME if dataset.startswith("balance_") else PERIOD_COLUMN
    return [str(c) for c in df.columns
            if str(c) not in RESERVED_COLUMNS and pattern.match(str(c))]


def _row(issuer: str, dataset: str, acquisition: str, reason: str | None,
         meta: dict, df: pd.DataFrame | None, retrieved_at: str,
         detail: str | None = None) -> dict:
    form = meta.get("form")
    period = meta.get("period_of_report") or meta.get("filing_date") or "snapshot"
    row = {
        "company": issuer,
        "dataset": dataset,
        "period": f"{form} {period}" if form else str(period),
        "acquisition": acquisition,
        "verification": "UNCHECKED",
        "origin": "AUTOMATIC",
        "retrieved_at": retrieved_at,
        "as_of": meta.get("as_of") or meta.get("period_of_report"),
        "filing": meta.get("accession"),
    }
    observed = _period_labels(df, dataset) if dataset.startswith(STATEMENT_PREFIXES) else []
    if observed:
        row["periods_observed"] = observed
    if detail:
        row["label"] = detail
    if acquisition in RUN_STATUSES:
        row["reason"] = reason or "NOT_RETRIEVABLE"
    # optional fields are omitted, never emitted as null — a null would fail the
    # contract instead of simply being absent
    return {k: v for k, v in row.items() if v is not None}


def _statements_from_sec(issuer: str, ticker: str, ceiling, retrieved_at: str
                         ) -> tuple[dict[str, pd.DataFrame], dict, list[dict]]:
    """Fetch statements across filing history: 3 annual + 8 quarterly filings.

    Each filing is fetched once and yields every statement it carries. A filing
    that genuinely does not carry a statement produces a MISSING row with a
    reason rather than a silent absence. Domestic issuers use 10-K/10-Q; 20-F is
    tried only when no annual 10-K could be retrieved at all, because a failed
    quarterly or a transport error is not evidence of a foreign private issuer.
    """
    tables: dict[str, pd.DataFrame] = {}
    meta: dict[str, dict] = {}
    rows: list[dict] = []
    annual_retrieved = 0

    for freq, form, count in (("annual", "10-K", ANNUAL_FILINGS),
                              ("quarterly", "10-Q", QUARTERLY_FILINGS)):
        for index in range(count):
            try:
                frames, m = sec.statements(ticker, form, index, issuer=issuer, ceiling=ceiling)
            except PermissionError:
                raise  # an approval/ceiling refusal is not a data problem
            except Exception as e:  # noqa: BLE001
                detail = _clean_error(e)
                key = f"{freq}_{index}"
                meta[key] = {"ticker": ticker, "form": form, "status": "FAILED", "detail": detail}
                rows.append(_row(issuer, f"sec_{freq}_{index}", "FAILED", "NOT_RETRIEVABLE",
                                 {"ticker": ticker, "form": form}, None, retrieved_at, detail=detail))
                if index == 0:
                    break  # no filings of this form at all — do not ask for more
                continue
            meta[f"{freq}_{index}"] = m
            if freq == "annual":
                annual_retrieved += 1
            for kind in sec.STATEMENT_KINDS:
                name = f"{kind}_{freq}_{index}"
                if kind in frames:
                    tables[name] = frames[kind]
                    rows.append(_row(issuer, name, "RETRIEVED", None, m, frames[kind], retrieved_at))
                else:
                    rows.append(_row(issuer, name, "MISSING", "NOT_PUBLISHED", m, None,
                                     retrieved_at, detail="statement not carried by this filing"))

    if annual_retrieved == 0:
        # foreign private issuers file 20-F annually; 6-K interims are documents,
        # not XBRL frames, so only the annual branch is retried
        for index in range(2):
            try:
                frames, m = sec.statements(ticker, "20-F", index, issuer=issuer, ceiling=ceiling)
            except PermissionError:
                raise
            except Exception as e:  # noqa: BLE001
                detail = _clean_error(e)
                key = f"annual_{index}"
                meta[key] = {**meta.get(key, {}), "form": "20-F", "fallback_detail": detail}
                rows = [r for r in rows if r["dataset"] != f"sec_annual_{index}"]
                rows.append(_row(issuer, f"sec_annual_{index}", "MISSING", "NOT_PUBLISHED",
                                 {"ticker": ticker, "form": "20-F"}, None, retrieved_at,
                                 detail=f"10-K and 20-F both unavailable ({detail})"))
                break
            meta[f"annual_{index}"] = {**m, "issuer_type": "foreign_private"}
            for kind in sec.STATEMENT_KINDS:
                name = f"{kind}_annual_{index}"
                if kind in frames:
                    tables[name] = frames[kind]
                    rows = [r for r in rows if r["dataset"] != name]
                    rows.append(_row(issuer, name, "RETRIEVED", None, m, frames[kind],
                                     retrieved_at, detail="20-F annual; interims are documents"))

    return tables, meta, rows


def _transcript_row(issuer: str, quarter: str, meta: dict, retrieved_at: str) -> dict:
    """One coverage row per company+quarter, honest about what was not acquired."""
    acquisition = meta.get("status") or "MISSING"
    row = {
        "company": issuer,
        "dataset": f"av_transcript_{quarter}",
        "period": quarter,
        "acquisition": acquisition,
        "verification": "UNCHECKED",
        "origin": "AUTOMATIC",
        "retrieved_at": retrieved_at,
    }
    if acquisition != "RETRIEVED":
        row["reason"] = meta.get("reason") or "NOT_RETRIEVABLE"
        if meta.get("detail"):
            row["label"] = f"transcript not acquired for {quarter}: {meta['detail']}"
    return {k: v for k, v in row.items() if v is not None}


def held_transcript_quarters(issuer: str) -> dict[str, str]:
    """company+quarter -> the frozen snapshot that already holds it (no network).

    A frozen transcript is evidence: a later acquisition reuses it rather than
    re-pulling, so the provider is asked once per quarter unless `--refresh`.
    """
    held: dict[str, str] = {}
    for snap in reversed(store.snapshot_dirs(issuer)):
        manifest_path = snap / "snapshot.json"
        if not manifest_path.is_file():
            continue
        manifest = json.loads(manifest_path.read_text())
        coverage = manifest.get("coverage_path")
        if not coverage or not Path(coverage).is_file():
            continue
        try:
            rows = json.loads(Path(coverage).read_text()).get("rows", [])
        except (json.JSONDecodeError, OSError):
            continue
        for row in rows:
            quarter = row.get("period")
            if (str(row.get("dataset") or "").startswith("av_transcript_")
                    and row.get("acquisition") == "RETRIEVED" and quarter
                    and quarter not in held):
                held[quarter] = str(snap)
    return held


def _held_transcript_segments(snap_dir: str, quarter: str) -> list[dict]:
    """The frozen segments for one quarter, read back hash-verified.

    The cached path is still frozen evidence: the table is read through the same
    hash check as a citation, so a tampered or partial table is refused rather than
    silently reused as if it were the quarter's transcript.
    """
    snap = Path(snap_dir)
    try:
        frame = contracts.read_verified_table(snap, "av_transcript")
    except (KeyError, ValueError) as exc:
        raise ValueError(
            f"the frozen transcript snapshot {snap.name} cannot back a cached quarter: "
            f"{exc}") from exc
    if "quarter" not in frame.columns:
        raise ValueError(f"snapshot {snap.name} carries no av_transcript rows")
    rows = []
    for record in frame[frame["quarter"] == quarter].to_dict("records"):
        rows.append({k: record.get(k) for k in ("quarter", "segment", "speaker", "title",
                                                "content")})
    return rows


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


def scope_key(issuer: str, sources, transcripts) -> str:
    """Identity of what a run acquires. Ceilings are excluded on purpose: a
    tighter cap must not make already-held evidence look like a cache miss."""
    scope = {"issuer": issuer, "sources": sorted(set(sources)),
             "transcripts": sorted(transcripts or [])}
    return hashlib.sha256(json.dumps(scope, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


def _cache_lookup(issuer: str, scope_key: str) -> dict | None:
    """Reuse evidence already held for this acquisition scope.

    A plain pull is cache-first: a snapshot acquired for the same issuer, source
    set and transcript quarters is returned with zero network calls. New
    acquisition — including fetching fresher data — is an explicit `refresh`.
    """
    for snap in reversed(store.snapshot_dirs(issuer)):
        manifest = json.loads((snap / "snapshot.json").read_text())
        if manifest.get("scope_key") == scope_key:
            return {
                "issuer": issuer,
                "status": "CACHED",
                "snapshot_dir": str(snap),
                "retrieved_at": manifest.get("retrieved_at"),
                "coverage_path": manifest.get("coverage_path"),
                "tables": len(manifest.get("table_hashes") or {}),
                "originals": len(manifest.get("originals") or {}),
                "note": ("evidence already held for this scope — zero network calls; "
                         "pass --refresh for a new version"),
            }
    return None


def pull(ticker: str, *, issuer: str | None = None, sources=None, transcripts=None,
         cache_only: bool = False, refresh: bool = False,
         ceilings: dict[str, int] | None = None) -> dict:
    """Acquire a snapshot for one ticker from the sources named.

    cache_only: never touches the network — the newest snapshot already published
    for this exact scope, or a MISSING status naming it.
    plain run: cache-first — a snapshot held for the same scope is returned with
    zero network calls. The scope is (issuer, sources, transcripts) and carries
    no date, so the cache does not expire on its own: a repeat pull stays CACHED
    until the caller passes `refresh=True`.
    refresh: ADDS a new snapshot version; published snapshots are never touched.

    `ceilings` names per-provider request limits to enforce, for example
    {"alpha_vantage": 10}. Requests are counted and returned either way.
    """
    sources = list(sources or DEFAULT_SOURCES)
    for source in sources:
        if source not in SOURCES:
            raise ValueError(f"unknown source {source!r}; expected one of {list(SOURCES)}")
    if cache_only and refresh:
        raise ValueError("cache_only and refresh cannot be combined")
    if transcripts and "alpha_vantage" not in sources:
        raise ValueError("transcripts come from alpha_vantage; name it in sources")
    issuer = issuer or ticker
    store.safe_component(issuer, "issuer")
    key = scope_key(issuer, sources, transcripts)

    if cache_only:
        for snap in reversed(store.snapshot_dirs(issuer)):
            manifest = json.loads((snap / "snapshot.json").read_text())
            if manifest.get("scope_key") == key:
                return {"issuer": issuer, "cache_only": True, "status": "CACHED",
                        "snapshot_dir": str(snap), "snapshot": manifest}
        return {"status": "MISSING: NOT_RETRIEVED", "issuer": issuer}

    if not refresh:
        cached = _cache_lookup(issuer, key)
        if cached:
            return {**cached, "status": "CACHED"}

    ceiling = Ceiling(ceilings or {})
    run_id = f"{store.now_iso().replace(':', '')}-{uuid.uuid4().hex[:6]}"
    retrieved_at = store.now_iso()
    rows: list[dict] = []
    tables: dict[str, pd.DataFrame] = {}
    provider_meta: dict[str, dict] = {}
    originals: dict[str, str] = {}

    # --- statements (SEC/edgartools, originals preserved per filing) ---
    if "sec" in sources:
        st_tables, st_meta, st_rows = _statements_from_sec(issuer, ticker, ceiling, retrieved_at)
        tables.update(st_tables)
        rows.extend(st_rows)
        provider_meta["sec"] = {"status": "RETRIEVED" if st_tables else "FAILED", "filings": st_meta}
        originals.update({k: v["original_path"] for k, v in st_meta.items()
                          if isinstance(v, dict) and v.get("original_path")})

    # --- Yahoo analyst snapshot datasets ---
    if "yahoo" in sources:
        try:
            yf_frames, yf_meta = yahoo.fetch(ticker, issuer=issuer, ceiling=ceiling)
        except PermissionError:
            raise
        except Exception as e:  # noqa: BLE001 — provider-level failure degrades to coverage rows
            detail = _clean_error(e)
            yf_frames = {}
            yf_meta = {"statuses": {name: "FAILED" for name in yahoo.DATASETS},
                       "reasons": {name: "NOT_RETRIEVABLE" for name in yahoo.DATASETS},
                       "detail": detail, "retrieved_at": retrieved_at}
        provider_meta["yahoo"] = yf_meta
        tables.update(yf_frames)
        originals.update(yf_meta.get("originals") or {})
        for name, status in yf_meta["statuses"].items():
            rows.append(_row(issuer, name, status, (yf_meta.get("reasons") or {}).get(name),
                             {}, yf_frames.get(name), retrieved_at,
                             detail=yf_meta.get("detail")))

    # --- Alpha Vantage estimates (reports a missing key rather than raising) ---
    if "alpha_vantage" in sources:
        try:
            av_data, av_meta = alphavantage.earnings_estimates(ticker, issuer=issuer, ceiling=ceiling)
        except PermissionError:
            raise
        except Exception as e:  # noqa: BLE001
            av_data, av_meta = None, {"status": "FAILED", "reason": "NOT_RETRIEVABLE",
                                      "detail": _clean_error(e)}
        provider_meta["alpha_vantage"] = av_meta
        rows.append(_row(issuer, "av_earnings_estimates", av_meta.get("status", "MISSING"),
                         av_meta.get("reason"), {}, None, retrieved_at,
                         detail=av_meta.get("detail")))
        if av_data and av_data.get("estimates"):
            tables["av_earnings_estimates"] = pd.DataFrame(av_data["estimates"])
        if av_meta.get("original_path"):
            originals["av_earnings_estimates"] = av_meta["original_path"]

    # --- earnings-call transcripts: one quarter at a time, one row each ---
    wanted = [q for q in (transcripts or []) if q]
    held = {} if refresh else held_transcript_quarters(issuer)
    live_calls = 0
    segments: list[dict] = []
    missing: list[str] = []
    if wanted:
        spacing = TRANSCRIPT_MIN_INTERVAL_SECONDS
        for quarter in wanted:
            if quarter in held:
                rows.append(_transcript_row(issuer, quarter, {"status": "RETRIEVED"},
                                            retrieved_at))
                segments.extend(_held_transcript_segments(held[quarter], quarter))
                provider_meta.setdefault("alpha_vantage_transcripts", {})[quarter] = {
                    "status": "CACHED", "snapshot": Path(held[quarter]).name}
                # carry the cached quarter's preserved original into this snapshot's
                # manifest too, so every quarter's raw payload is reachable from the
                # snapshot that cites it — not only from the earlier one
                held_manifest_path = Path(held[quarter]) / "snapshot.json"
                if held_manifest_path.is_file():
                    held_originals = (json.loads(held_manifest_path.read_text())
                                      .get("originals") or {})
                    origin = held_originals.get(f"av_transcript_{quarter}")
                    if origin:
                        originals[f"av_transcript_{quarter}"] = origin
                continue
            if live_calls and spacing:
                time.sleep(spacing)  # respect the free tier rather than tripping its limiter
            live_calls += 1
            for attempt in range(TRANSCRIPT_RATE_LIMIT_RETRIES + 1):
                try:
                    data, meta = alphavantage.earnings_call_transcript(
                        ticker, quarter, issuer=issuer, ceiling=ceiling)
                except PermissionError as exc:
                    # a ceiling or scope refusal is recorded, not raised mid-run: the
                    # quarter is still named missing and the rest is still acquired
                    data, meta = None, {"provider": "alpha_vantage", "dataset": "av_transcript",
                                        "quarter": quarter, "status": "MISSING",
                                        "reason": "NOT_RETRIEVABLE", "detail": str(exc)}
                    break
                if meta.get("status") != "RATE_LIMITED" \
                        or attempt == TRANSCRIPT_RATE_LIMIT_RETRIES:
                    break
                # a throttled quarter is retried with backoff, never recorded as a
                # coverage gap; the final attempt's honest status is what is recorded
                time.sleep(spacing * (2 ** (attempt + 1)))
            provider_meta.setdefault("alpha_vantage_transcripts", {})[quarter] = {
                k: v for k, v in meta.items() if k not in ("label",)}
            rows.append(_transcript_row(issuer, quarter, meta, retrieved_at))
            if meta.get("status") != "RETRIEVED" or not data:
                # an empty transcript is a MISSING quarter, never a silent success
                missing.append(quarter)
                continue
            for index, segment in enumerate(data.get("segments") or []):
                segments.append({"quarter": quarter, "segment": index, **segment})
            if meta.get("original_path"):
                originals[f"av_transcript_{quarter}"] = meta["original_path"]

        if segments:
            tables["av_transcript"] = pd.DataFrame(segments)

    # --- validate before anything is published (fail closed) ---
    coverage_doc = {"issuer": issuer, "run_id": run_id, "recorded_at": retrieved_at,
                    "rows": rows, "snapshot_id": run_id}
    violations = contracts.validate_against(coverage_doc, "coverage")
    if violations:
        return {"status": "CONTRACT_VIOLATION", "issuer": issuer, "violations": violations}

    result = _publish_snapshot(ticker, issuer, sources, transcripts, run_id, retrieved_at,
                               rows, tables, provider_meta, originals)
    return {**result, "requests": ceiling.used}


def _publish_snapshot(ticker: str, issuer: str, sources: list[str], transcripts,
                      run_id: str, retrieved_at: str, rows: list[dict],
                      tables: dict[str, pd.DataFrame], provider_meta: dict[str, dict],
                      originals: dict[str, str]) -> dict:
    """Stage, hash and atomically publish one snapshot, then record coverage."""
    # --- stage the snapshot, then publish it atomically ---
    staging = store.staging_dir(issuer, run_id)
    table_hashes: dict[str, str] = {}
    for name, df in tables.items():
        df.to_parquet(staging / f"{name}.parquet")
        table_hashes[name] = store.sha256_file(staging / f"{name}.parquet")

    cov_target = store.coverage_path(issuer, run_id)
    key = scope_key(issuer, sources, transcripts)
    manifest = {
        "issuer": issuer,
        "ticker": ticker,
        "run_id": run_id,
        "sources": sorted(sources),
        "transcripts": sorted(transcripts or []),
        "scope_key": key,
        "retrieved_at": retrieved_at,
        "providers": provider_meta,
        "originals": originals,
        "table_hashes": table_hashes,
        "coverage_path": str(cov_target),
    }
    store.write_snapshot_manifest(staging, manifest)
    snap_dir = store.commit_snapshot(staging, run_id)

    # coverage is renamed into place only after the snapshot it describes exists
    cov_tmp = cov_target.with_name(f".pending-{cov_target.name}")
    store.atomic_write_json(cov_tmp, {"issuer": issuer, "run_id": run_id,
                                      "recorded_at": retrieved_at, "rows": rows,
                                      "snapshot_id": run_id})
    cov_tmp.replace(cov_target)

    gaps = [f"{r['dataset']}: {r.get('reason') or r['acquisition']}"
            for r in rows if r["acquisition"] != "RETRIEVED"]
    return {"ticker": ticker, "issuer": issuer, "status": "RETRIEVED",
            "snapshot_dir": str(snap_dir), "coverage_path": str(cov_target),
            "scope_key": key,
            "sources": sorted(sources), "transcripts": sorted(transcripts or []),
            "table_hashes": table_hashes,
            "statuses": {r["dataset"]: r["acquisition"] for r in rows},
            "open_gaps": gaps}


def manifest(ticker: str, *, issuer: str | None = None, run_id: str | None = None) -> dict:
    """The manifest of a published snapshot: the pinned `run_id`, else the newest.

    Raises FileNotFoundError naming the ticker when no such snapshot exists.
    """
    issuer = issuer or ticker
    store.safe_component(issuer, "issuer")
    snap = (store.snapshot_by_id(issuer, run_id) if run_id
            else store.latest_snapshot_dir(issuer))
    if snap is None:
        raise FileNotFoundError(f"no snapshot for {ticker!r}")
    return json.loads((snap / "snapshot.json").read_text())


def read_table(ticker: str, table: str, *, issuer: str | None = None,
               run_id: str | None = None):
    """One snapshot table, read only if its recorded hash still matches.

    A table whose file no longer hashes to the value the snapshot recorded is
    refused with ValueError rather than returned as if it were the evidence that
    was published. KeyError names a table the snapshot does not carry;
    FileNotFoundError names a ticker or snapshot that does not exist.
    """
    issuer = issuer or ticker
    store.safe_component(issuer, "issuer")
    snap = (store.snapshot_by_id(issuer, run_id) if run_id
            else store.latest_snapshot_dir(issuer))
    if snap is None:
        raise FileNotFoundError(f"no snapshot for {ticker!r}")
    return contracts.read_verified_table(snap, table)
