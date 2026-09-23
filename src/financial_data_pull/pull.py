"""Pull orchestration: one run = one snapshot + one coverage record.

A run acquires the sources it was asked for and publishes them as one snapshot,
scoped to (issuer, ticker, source set, transcript quarters, 8-K depth). A snapshot
becomes visible only after its manifest is written inside it and the directory is
renamed into place, so a crash cannot leave a half-written snapshot that a
cache-only read would trust. Coverage rows report what was actually fetched — never
a hardcoded period claim — and are validated before they are published.

A plain run is cache-first: a snapshot already held for the same scope is
returned with zero network calls, and fresher data is an explicit `refresh`.
The scope key carries no date, so the cache never expires on its own — the
caller controls freshness.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import time
import uuid
from datetime import date
from pathlib import Path

import pandas as pd

from . import contracts, store
from .providers import alphavantage, sec, yahoo  # explicit submodule handles

# The library logs and leaves output to the caller: a NullHandler keeps importable
# use silent (no last-resort handler writing to stderr), and the CLI configures the
# stderr handler and level. One finish line per dataset, one WARNING per degraded
# dataset with its reason — never a line per request.
logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

RESERVED_COLUMNS = {"concept", "label", "dimension"}
RUN_STATUSES = ("MISSING", "FAILED", "RATE_LIMITED", "PARSE_FAILED")
# how much filing history one acquisition covers (master plan §4 data baseline)
ANNUAL_FILINGS = 3
QUARTERLY_FILINGS = 9
STATEMENT_PREFIXES = ("income_", "balance_", "cashflow_")
# the source sets a snapshot can be scoped to; a snapshot satisfies only the
# scope it was acquired for, so each may be refreshed on its own schedule
SOURCES = ("sec", "alpha_vantage", "yahoo")

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
# the transcript quarter label the provider names: `2025Q1`, never `none` or free text
QUARTER_LABEL = re.compile(r"\d{4}Q[1-4]")


def reported_quarters(filings_meta: dict, count: int = 12) -> list[str]:
    """Return distinct calendar-quarter labels represented by filing period ends.

    Missing or invalid period ends are ignored (notably failed filings). The newest
    `count` labels are selected, then returned oldest first for transcript acquisition.
    """
    labels = set()
    for filing in filings_meta.values():
        period = filing.get("period_of_report") if isinstance(filing, dict) else None
        if period is None or pd.isna(period):
            continue
        try:
            end = date.fromisoformat(str(period)[:10])
        except (TypeError, ValueError):
            continue
        labels.add(f"{end.year}Q{(end.month - 1) // 3 + 1}")
    return sorted(labels, reverse=True)[:count][::-1]


def _held_sec_filings(issuer: str, ticker: str) -> dict:
    """The newest ticker-matching published SEC filing metadata, if any."""
    for snap in reversed(store.snapshot_dirs(issuer)):
        manifest_path = snap / "snapshot.json"
        if not manifest_path.is_file():
            continue
        try:
            held_manifest = json.loads(manifest_path.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        if held_manifest.get("ticker") != ticker:
            continue
        filings = ((held_manifest.get("providers") or {}).get("sec") or {}).get("filings")
        if isinstance(filings, dict):
            return filings
    return {}


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


def _statement_row(issuer: str, name: str, kind: str, frames: dict, meta: dict,
                   retrieved_at: str, retrieved_detail: str | None = None) -> dict:
    """One coverage row per statement kind: retrieved, unreadable, or not carried.

    A statement that exists but cannot be converted is PARSE_FAILED with its reason;
    reporting it as "not carried by this filing" would blame the filing for our
    parsing problem. A statement that is genuinely absent is MISSING, never a silent
    gap in the record — both the 10-K and the 20-F branch must speak for every kind.
    """
    if kind in frames:
        return _row(issuer, name, "RETRIEVED", None, meta, frames[kind], retrieved_at,
                    detail=retrieved_detail)
    unreadable = (meta.get("statements_unreadable") or {}).get(kind)
    if unreadable:
        return _row(issuer, name, "PARSE_FAILED", "NOT_RETRIEVABLE", meta, None,
                    retrieved_at, detail=f"statement could not be read ({unreadable})")
    return _row(issuer, name, "MISSING", "NOT_PUBLISHED", meta, None, retrieved_at,
                detail="statement not carried by this filing")


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
                detail = store.clean_error(e)
                key = f"{freq}_{index}"
                logger.warning("sec_%s_%d: FAILED (%s)", freq, index, detail)
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
                rows.append(_statement_row(issuer, name, kind, frames, m, retrieved_at))

    if annual_retrieved == 0:
        # foreign private issuers file 20-F annually; 6-K interims are documents,
        # not XBRL frames, so only the annual branch is retried
        for index in range(2):
            try:
                frames, m = sec.statements(ticker, "20-F", index, issuer=issuer, ceiling=ceiling)
            except PermissionError:
                raise
            except Exception as e:  # noqa: BLE001
                detail = store.clean_error(e)
                logger.warning("sec_annual_%d: MISSING (10-K and 20-F both unavailable: %s)",
                               index, detail)
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
                rows.append(_statement_row(issuer, name, kind, frames, m, retrieved_at,
                                           retrieved_detail="20-F annual; interims are documents"))

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


def held_transcript_quarters(issuer: str) -> dict[str, tuple[str, str | None]]:
    """company+quarter -> (the frozen snapshot holding it, its preserved original).

    A frozen transcript is evidence: a later acquisition reuses it rather than
    re-pulling, so the provider is asked once per quarter — a refresh included. Both
    facts come from the one manifest already being read — no extra file reads.
    """
    held: dict[str, tuple[str, str | None]] = {}
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
                held[quarter] = (str(snap),
                                 (manifest.get("originals") or {})
                                 .get(f"av_transcript_{quarter}"))
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


def scope_key(issuer: str, ticker: str, sources, transcripts, eight_ks=None) -> str:
    """Identity of what a run acquires. The ticker is part of it: one issuer can be
    pulled under two listings (an ADR and its local line), and neither may answer as
    the other's cache hit. Ceilings are excluded on purpose: a tighter cap must not
    make already-held evidence look like a cache miss. The 8-K depth is part of the
    key only when requested, so a run that asks for none serializes exactly as it did
    before the parameter existed and keeps answering from the snapshots already held."""
    scope = {"issuer": issuer, "ticker": ticker, "sources": sorted(set(sources)),
             "transcripts": sorted(transcripts or [])}
    if eight_ks:
        scope["eight_ks"] = eight_ks
    return hashlib.sha256(json.dumps(scope, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


def _cache_lookup(issuer: str, scope_key: str) -> tuple[Path, dict] | None:
    """The evidence already held for this acquisition scope, if any.

    A plain pull is cache-first: a snapshot acquired for the same issuer, source
    set and transcript quarters is returned with zero network calls. New
    acquisition — including fetching fresher data — is an explicit `refresh`.
    """
    for snap in reversed(store.snapshot_dirs(issuer)):
        manifest = json.loads((snap / "snapshot.json").read_text())
        if manifest.get("scope_key") == scope_key:
            return snap, manifest
    return None


def pull(ticker: str, *, issuer: str | None = None, sources=None, transcripts=None,
         eight_ks: int | None = None, cache_only: bool = False, refresh: bool = False,
         ceilings: dict[str, int] | None = None) -> dict:
    """Acquire a snapshot for one ticker from the sources named.

    cache_only: never touches the network — the newest snapshot already published
    for this exact scope, or a MISSING status naming it.
    plain run: cache-first — a snapshot held for the same scope is returned with
    zero network calls. The scope is (issuer, ticker, sources, transcripts, eight_ks)
    and carries no date, so the cache does not expire on its own: a repeat pull stays
    CACHED until the caller passes `refresh=True`.
    nothing retrieved: no snapshot is published at all — a run that acquired no
    evidence records its coverage rows and returns `FAILED`, so the scope is not
    poisoned for later plain pulls.
    refresh: ADDS a new snapshot version; published snapshots are never touched.

    `eight_ks` names how many of the most recent Item 2.02 earnings 8-Ks to archive
    with their Exhibit 99.1; `None` defaults to 12 when `sec` is requested, and 0
    opts out. `transcripts` is left as `None` for the default: when alpha_vantage is
    among the sources, its quarters derive from held SEC filing periods, or from this
    run's SEC fetch on a first acquisition. Without SEC evidence, pass explicit labels.
    An explicit list names the quarters, and `[]` opts out.
    `ceilings` names per-provider request limits to enforce, for example
    {"alpha_vantage": 10}. Requests are counted and returned either way.
    """
    sources = list(SOURCES) if sources is None else list(sources)
    if not sources:
        raise ValueError(f"sources must name at least one of {list(SOURCES)}")
    for source in sources:
        if source not in SOURCES:
            raise ValueError(f"unknown source {source!r}; expected one of {list(SOURCES)}")
    issuer = issuer or ticker
    store.safe_component(issuer, "issuer")
    if cache_only and refresh:
        raise ValueError("cache_only and refresh cannot be combined")
    if eight_ks is None:
        eight_ks = 12 if "sec" in sources else 0
    eight_ks = int(eight_ks)
    if eight_ks < 0:
        raise ValueError("eight_ks is a count of filings and cannot be negative")
    derived_transcripts = transcripts is None and "alpha_vantage" in sources
    sec_prefetched = None
    if derived_transcripts:
        held_filings = _held_sec_filings(issuer, ticker)
        transcripts = reported_quarters(held_filings)
        if not transcripts:
            if "sec" not in sources:
                raise ValueError(
                    "transcripts=[…] required: no SEC evidence to derive reported quarters")
            if cache_only:
                return {"status": "MISSING: NOT_RETRIEVED", "issuer": issuer}
            # A first-ever pull must fetch SEC before its scope can be named. Reuse this
            # fetch below rather than spending the filing requests twice.
            ceiling = Ceiling(ceilings or {})
            retrieved_at = store.now_iso()
            sec_prefetched = _statements_from_sec(issuer, ticker, ceiling, retrieved_at)
            transcripts = reported_quarters(sec_prefetched[1])
    # a repeated quarter would spend a second request and write a second coverage row
    transcripts = list(dict.fromkeys(q for q in (transcripts or []) if q))
    # a label the provider cannot name would spend a real request and record a MISSING
    # quarter that can never fill — fail at the boundary, before any quota is spent
    bad_labels = [q for q in transcripts if not QUARTER_LABEL.fullmatch(q)]
    if bad_labels:
        raise ValueError(f"transcript labels must be YYYYQN, got {bad_labels!r}")
    if transcripts and "alpha_vantage" not in sources:
        raise ValueError("transcripts come from alpha_vantage; name it in sources")
    if eight_ks and "sec" not in sources:
        raise ValueError("8-Ks come from sec; name it in sources")
    key = scope_key(issuer, ticker, sources, transcripts, eight_ks)

    if cache_only:
        hit = _cache_lookup(issuer, key)
        if hit is None:
            logger.info("no snapshot held for scope %s", key)
            return {"status": "MISSING: NOT_RETRIEVED", "issuer": issuer}
        snap, manifest = hit
        logger.info("CACHED %s: snapshot %s", issuer, snap.name)
        return {"issuer": issuer, "cache_only": True, "status": "CACHED",
                "snapshot_dir": str(snap), "snapshot": manifest}

    if not refresh:
        hit = _cache_lookup(issuer, key)
        if hit:
            snap, manifest = hit
            logger.info("CACHED %s: snapshot %s", issuer, snap.name)
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
        logger.info("no snapshot held for scope %s", key)

    if sec_prefetched is None:
        ceiling = Ceiling(ceilings or {})
    run_id = f"{store.now_iso().replace(':', '')}-{uuid.uuid4().hex[:6]}"
    retrieved_at = store.now_iso()
    rows: list[dict] = []
    tables: dict[str, pd.DataFrame] = {}
    provider_meta: dict[str, dict] = {}
    originals: dict[str, str] = {}

    # --- statements (SEC/edgartools, originals preserved per filing) ---
    if "sec" in sources:
        st_tables, st_meta, st_rows = (sec_prefetched if sec_prefetched is not None else
                                       _statements_from_sec(issuer, ticker, ceiling, retrieved_at))
        tables.update(st_tables)
        rows.extend(st_rows)
        provider_meta["sec"] = {"status": "RETRIEVED" if st_tables else "FAILED", "filings": st_meta}
        originals.update({k: v["original_path"] for k, v in st_meta.items()
                          if isinstance(v, dict) and v.get("original_path")})
        retrieved_filings = sum(1 for filing_meta in st_meta.values()
                                if isinstance(filing_meta, dict)
                                and filing_meta.get("status") != "FAILED")
        logger.info("sec: %d filings, %d tables", retrieved_filings, len(st_tables))

        # --- earnings 8-Ks: Item 2.02 filings and their press releases ---
        if eight_ks:
            try:
                eight_frame, eight_meta = sec.earnings_8k(ticker, eight_ks, issuer=issuer,
                                                          ceiling=ceiling)
            except PermissionError:
                raise  # an approval/ceiling refusal is not a data problem
            except Exception as e:  # noqa: BLE001
                eight_frame, eight_meta = None, {
                    "provider": "sec", "dataset": "sec_8k", "ticker": ticker,
                    "status": "FAILED", "reason": "NOT_RETRIEVABLE",
                    "detail": store.clean_error(e), "filings": [],
                    "retrieved_at": retrieved_at}
            provider_meta["sec_8k"] = eight_meta
            # one row per filing: the aggregated statuses dict would otherwise
            # collapse the filings into a single entry
            for record in eight_meta.get("filings") or []:
                rows.append(_row(issuer, f"sec_8k_{record['accession']}", record["status"],
                                 record.get("reason"),
                                 {"ticker": ticker, "filing_date": record.get("filing_date"),
                                  "accession": record.get("accession")},
                                 None, retrieved_at, detail=record.get("detail")))
                if record.get("exhibit_path"):
                    originals[f"sec_8k_{record['accession']}"] = record["exhibit_path"]
            if eight_frame is not None:
                tables["sec_8k"] = eight_frame
            else:
                rows.append(_row(issuer, "sec_8k", eight_meta.get("status") or "MISSING",
                                 eight_meta.get("reason") or "NOT_RETRIEVABLE", {}, None,
                                 retrieved_at, detail=eight_meta.get("detail")))
            archived = sum(1 for record in eight_meta.get("filings") or []
                           if record.get("status") == "RETRIEVED")
            if archived:
                logger.info("sec_8k: %d filings archived", archived)
            else:
                logger.warning("sec_8k: %s (%s)", eight_meta.get("status") or "MISSING",
                               eight_meta.get("detail") or eight_meta.get("reason")
                               or "NOT_RETRIEVABLE")

    # --- Yahoo analyst snapshot datasets ---
    if "yahoo" in sources:
        try:
            yf_frames, yf_meta = yahoo.fetch(ticker, issuer=issuer, ceiling=ceiling)
        except PermissionError:
            raise
        except Exception as e:  # noqa: BLE001 — provider-level failure degrades to coverage rows
            detail = store.clean_error(e)
            yf_frames = {}
            yf_meta = {"statuses": {name: "FAILED" for name in yahoo.DATASETS},
                       "reasons": {name: "NOT_RETRIEVABLE" for name in yahoo.DATASETS},
                       "detail": detail, "retrieved_at": retrieved_at}
        provider_meta["yahoo"] = yf_meta
        tables.update(yf_frames)
        originals.update(yf_meta.get("originals") or {})
        failures = yf_meta.get("failures") or {}
        for name, status in yf_meta["statuses"].items():
            reason = (yf_meta.get("reasons") or {}).get(name)
            detail = failures.get(name) or yf_meta.get("detail")
            rows.append(_row(issuer, name, status, reason, {}, yf_frames.get(name),
                             retrieved_at, detail=detail))
            if status != "RETRIEVED":
                logger.warning("%s: %s (%s)", name, status,
                               detail or reason or "NOT_RETRIEVABLE")
        logger.info("yahoo: %d datasets", sum(1 for status in yf_meta["statuses"].values()
                                               if status == "RETRIEVED"))

    # --- Alpha Vantage estimates (reports a missing key rather than raising) ---
    if "alpha_vantage" in sources:
        try:
            av_data, av_meta = alphavantage.earnings_estimates(ticker, issuer=issuer, ceiling=ceiling)
        except PermissionError:
            raise
        except Exception as e:  # noqa: BLE001
            av_data, av_meta = None, {"status": "FAILED", "reason": "NOT_RETRIEVABLE",
                                      "detail": store.clean_error(e)}
        provider_meta["alpha_vantage"] = av_meta
        rows.append(_row(issuer, "av_earnings_estimates", av_meta.get("status", "MISSING"),
                         av_meta.get("reason"), {}, None, retrieved_at,
                         detail=av_meta.get("detail")))
        if av_meta.get("status") == "RETRIEVED":
            logger.info("av_earnings_estimates: ok")
        else:
            logger.warning("av_earnings_estimates: %s (%s)",
                           av_meta.get("status") or "MISSING",
                           av_meta.get("detail") or av_meta.get("reason")
                           or "NOT_RETRIEVABLE")
        if av_data and av_data.get("estimates"):
            tables["av_earnings_estimates"] = pd.DataFrame(av_data["estimates"])
        if av_meta.get("original_path"):
            originals["av_earnings_estimates"] = av_meta["original_path"]

    # --- earnings-call transcripts: one quarter at a time, one row each ---
    # A transcript quarter already held is frozen evidence: reuse it even on refresh,
    # so a refreshed snapshot folds the held quarters in rather than re-asking the
    # provider. Only quarters not held are fetched. (To force a re-pull of a held
    # quarter, drop it from the store or pass a sources set without alpha_vantage.)
    held = held_transcript_quarters(issuer)
    live_calls = 0
    segments: list[dict] = []
    missing: list[str] = []
    if transcripts:
        spacing = TRANSCRIPT_MIN_INTERVAL_SECONDS
        for quarter in transcripts:
            if quarter in held:
                snap_dir, original = held[quarter]
                rows.append(_transcript_row(issuer, quarter, {"status": "RETRIEVED"},
                                            retrieved_at))
                segments.extend(_held_transcript_segments(snap_dir, quarter))
                provider_meta.setdefault("alpha_vantage_transcripts", {})[quarter] = {
                    "status": "CACHED", "snapshot": Path(snap_dir).name}
                logger.info("av_transcript %s: cached", quarter)
                # carry the cached quarter's preserved original into this snapshot's
                # manifest too, so every quarter's raw payload is reachable from the
                # snapshot that cites it — not only from the earlier one
                if original:
                    originals[f"av_transcript_{quarter}"] = original
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
                                        "reason": "NOT_RETRIEVABLE",
                                        "detail": store.clean_error(exc)}
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
            if meta.get("status") == "RETRIEVED" and data:
                logger.info("av_transcript %s: ok", quarter)
            else:
                logger.warning("av_transcript %s: %s (%s)", quarter,
                               meta.get("status") or "MISSING",
                               meta.get("detail") or meta.get("reason")
                               or "NOT_RETRIEVABLE")
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

    result = _publish_snapshot(ticker, issuer, sources, transcripts, eight_ks, key, run_id,
                               retrieved_at, rows, tables, provider_meta, originals)
    if derived_transcripts:
        result["reported_quarters"] = transcripts
        if len(transcripts) < 12:
            result["reported_quarters_note"] = (
                f"{len(transcripts)} reported quarter(s) available; scope was not padded")
    return {**result, "requests": ceiling.used}


def _publish_snapshot(ticker: str, issuer: str, sources: list[str], transcripts,
                      eight_ks: int, key: str, run_id: str, retrieved_at: str, rows: list[dict],
                      tables: dict[str, pd.DataFrame], provider_meta: dict[str, dict],
                      originals: dict[str, str]) -> dict:
    """Record coverage, and publish a snapshot when the run retrieved something.

    Every RETRIEVED dataset contributes a table, so an empty `tables` means the run
    acquired nothing: its coverage document is still written — the gaps are the
    record of what was attempted — but no snapshot is published. This cache has no
    TTL, so publishing an evidence-free run would answer every later plain pull with
    `CACHED` and zero network calls.

    `key` is the scope key the caller looked the cache up with, so the manifest can
    never record a key that differs from the one a later pull searches for.
    """
    cov_target = store.coverage_path(issuer, run_id)
    statuses = {r["dataset"]: r["acquisition"] for r in rows}
    gaps = [f"{r['dataset']}: {r.get('reason') or r['acquisition']}"
            for r in rows if r["acquisition"] != "RETRIEVED"]
    coverage_doc = {"issuer": issuer, "run_id": run_id, "recorded_at": retrieved_at,
                    "rows": rows}
    if not tables:
        # no snapshot_id: it names the snapshot that would carry this coverage, and
        # this run published none
        store.atomic_write_json(cov_target, coverage_doc)
        return {"ticker": ticker, "issuer": issuer, "status": "FAILED",
                "coverage_path": str(cov_target), "scope_key": key,
                "sources": sorted(sources), "transcripts": sorted(transcripts or []),
                "eight_ks": eight_ks,
                "table_hashes": {}, "statuses": statuses, "open_gaps": gaps}

    # --- stage the snapshot, then publish it atomically ---
    staging = store.staging_dir(issuer, run_id)
    table_hashes: dict[str, str] = {}
    for name, df in tables.items():
        df.to_parquet(staging / f"{name}.parquet")
        table_hashes[name] = store.sha256_file(staging / f"{name}.parquet")

    # A snapshot that drops what the previous version carried makes "the latest" a thinner
    # answer than it was. Say so where it can still be fixed by a full-source pull rather
    # than letting a reader discover it downstream.
    previous = store.latest_snapshot_dir(issuer)
    if previous is not None and previous.name != run_id:
        carried = set(json.loads((previous / "snapshot.json").read_text())
                      .get("table_hashes") or {})
        dropped = sorted(carried - set(table_hashes))
        if dropped:
            logger.warning(
                "this snapshot drops %d table(s) the previous latest %s carried: %s — the "
                "latest view is now thinner than it was; a full-source pull restores it",
                len(dropped), previous.name,
                ", ".join(dropped[:6]) + ("…" if len(dropped) > 6 else ""))

    manifest = {
        "issuer": issuer,
        "ticker": ticker,
        "run_id": run_id,
        "sources": sorted(sources),
        "transcripts": sorted(transcripts or []),
        "eight_ks": eight_ks,
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
    store.atomic_write_json(cov_target, {**coverage_doc, "snapshot_id": run_id})

    return {"ticker": ticker, "issuer": issuer, "status": "RETRIEVED",
            "snapshot_dir": str(snap_dir), "coverage_path": str(cov_target),
            "scope_key": key,
            "sources": sorted(sources), "transcripts": sorted(transcripts or []),
            "eight_ks": eight_ks,
            "table_hashes": table_hashes, "statuses": statuses, "open_gaps": gaps}


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


def export_csv(issuer: str, out_dir=None) -> dict[str, str]:
    """One CSV per table of the latest snapshot — the same view `read_table` reads.

    Every CSV in the directory comes from that single snapshot, so an export can never
    mix versions: a table the latest snapshot does not carry is missing from the export
    rather than quietly answered from an older run. Into this tool's own directory, a CSV
    left over from an earlier export of such a table is removed — the directory is derived
    and rewritable, and a stale file beside current ones invites exactly that misreading.
    A caller-supplied directory is left alone.

    Reads go through the same hash check as any other table read, and every table is
    verified before the first CSV is written: a store that lost a byte fails the export
    instead of leaving a half-written set behind. Returns {table: snapshot id}; no network.
    """
    store.safe_component(issuer, "issuer")
    snap = store.latest_snapshot_dir(issuer)
    if snap is None:
        return {}
    manifest = json.loads((snap / "snapshot.json").read_text())
    frames: dict[str, pd.DataFrame] = {}
    for table in sorted(manifest.get("table_hashes") or {}):
        try:
            frames[table] = contracts.read_verified_table(snap, table)
        except KeyError as exc:
            # The manifest names this table, so the only KeyError left is the file it
            # recorded having vanished — corruption, not a snapshot that lacks the table
            # (a snapshot that lacks it never enters this loop).
            raise ValueError(
                f"snapshot {snap.name} records the table {table!r} but its parquet "
                f"is missing — refusing to export a partial snapshot") from exc
    out = Path(out_dir) if out_dir else store.CSV / issuer
    out.mkdir(parents=True, exist_ok=True)
    exported = {table: snap.name for table in frames}
    for table, frame in frames.items():
        # A named index is data — the dates on prices, the period on the analyst frames —
        # so it becomes a column rather than being dropped by index=False.
        if not isinstance(frame.index, pd.RangeIndex):
            frame = frame.reset_index()
        frame.to_csv(out / f"{table}.csv", index=False, encoding="utf-8")
    if out_dir is None:
        for stale in sorted(out.glob("*.csv")):
            if stale.stem not in exported:
                stale.unlink()
    return exported
