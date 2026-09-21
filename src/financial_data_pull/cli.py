"""CLI — the single entry point (`financial-data-pull`). A thin wrapper over
`pull.pull`: one ticker, an optional source set, optional transcript quarters, an
optional 8-K depth. Nothing stands between the call and the network but the
optional `--ceiling` — except `--export-csv` and `--export-views`, which only
rewrite derived files from the snapshots already held."""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from .pull import SOURCES, export_csv, pull
from .views import export_views

# A run that published nothing must not exit 0: a scheduled pull would report success
# while the store is unchanged. `MISSING: NOT_RETRIEVED` is an answer, not a failure —
# `--cache-only` asked what is held and got a truthful "nothing".
UNPUBLISHED_STATUSES = ("FAILED", "CONTRACT_VIOLATION")


def _ceiling(text: str) -> dict[str, int]:
    """`provider=n` pairs, e.g. alpha_vantage=10,sec=40."""
    limits: dict[str, int] = {}
    for pair in (p.strip() for p in text.split(",") if p.strip()):
        provider, sep, value = pair.partition("=")
        if not sep or not value.strip().isdigit():
            raise ValueError(f"--ceiling {pair!r} is not provider=n")
        limits[provider.strip()] = int(value)
    return limits


def _transcripts(text: str) -> list[str]:
    """`--transcripts` value: the literal `none` opts out with an empty list, anything
    else is a comma list of quarters."""
    labels = [q.strip() for q in (text or "").split(",") if q.strip()]
    if len(labels) == 1 and labels[0].lower() == "none":
        return []
    return labels


_stderr_handler: logging.Handler | None = None


def _setup_logging(quiet: bool) -> None:
    """Send our log records to stderr; leave an embedder's configuration alone.

    Three cases: a fresh root gets our stderr handler and the level; a root whose
    handlers are only ours (repeated `main()` calls in one process) gets the level
    re-decided from `quiet`; a root carrying anyone else's handlers is left entirely
    alone — handler and level — so an embedder's logging configuration stands.
    """
    global _stderr_handler
    root = logging.getLogger()
    if root.handlers and _stderr_handler not in root.handlers:
        return
    if _stderr_handler is None:
        _stderr_handler = logging.StreamHandler(sys.stderr)
        _stderr_handler.setFormatter(logging.Formatter("%(message)s"))
        root.addHandler(_stderr_handler)
    root.setLevel(logging.WARNING if quiet else logging.INFO)


def _status_line(result: dict) -> str | None:
    """One human line saying whether the run answered from cache or went to the network.

    The JSON on stdout is the machine-readable record; this is the same answer in a
    sentence. It reads only fields `pull` already returned — the snapshot basename it
    published or hit, and the provider request counts — so nothing is recomputed.
    """
    status = result.get("status")
    if status == "CACHED":
        return (f"CACHED — evidence already held, zero network "
                f"(snapshot {Path(result.get('snapshot_dir') or '').name})")
    if status == "MISSING: NOT_RETRIEVED":
        return f"no data held for {result.get('issuer')} (cache-only)"
    if status in UNPUBLISHED_STATUSES:
        return f"{status} — nothing published"
    if status == "RETRIEVED":
        spent = sum((result.get("requests") or {}).values())
        return (f"NEW SNAPSHOT {Path(result.get('snapshot_dir') or '').name} — "
                f"{spent} requests spent")
    return None


def _export(issuer: str) -> int:
    """Write the CSVs and print the snapshot each table came from; zero network."""
    try:
        exported = export_csv(issuer)
    except (ValueError, PermissionError) as exc:
        print(exc, file=sys.stderr)
        return 1
    for table, snapshot_id in sorted(exported.items()):
        print(f"{table} ← {snapshot_id}")
    if not exported:
        print(f"no snapshot for {issuer!r} — nothing exported", file=sys.stderr)
    return 0


def _export_views(issuer: str) -> int:
    """Write the derived views and print one summary line per group; zero network."""
    try:
        counts = export_views(issuer)
    except (ValueError, PermissionError) as exc:
        print(exc, file=sys.stderr)
        return 1
    if not any(counts.values()):
        print(f"no 8-K exhibits or transcripts held for {issuer!r} — nothing derived",
              file=sys.stderr)
        return 0
    for group, count in counts.items():
        unit = "rows" if group.endswith(".csv") else "files"
        print(f"{group}: {count:,} {unit}")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="financial-data-pull")
    p.add_argument("ticker")
    p.add_argument("--issuer", default=None,
                   help="store name when it differs from the ticker")
    p.add_argument("--sources", default=None,
                   help=f"comma-separated source set ({', '.join(SOURCES)}); default: all")
    p.add_argument("--transcripts", default=None,
                   help="earnings-call quarters to acquire, comma-separated YYYYQN labels "
                        "(for example 2025Q1,2025Q2); default: the last 4 completed "
                        "calendar quarters, which needs alpha_vantage — pass \"none\" to "
                        "opt out")
    p.add_argument("--earnings-8k", type=int, default=None, metavar="N",
                   help="archive the N most recent Item 2.02 earnings 8-Ks with their "
                        "Exhibit 99.1 press release; needs sec")
    p.add_argument("--export-csv", action="store_true",
                   help="write one CSV per table into data/csv/<issuer>/ from the snapshots "
                        "already held and print each table's snapshot; no network, and not "
                        "combinable with acquisition flags")
    p.add_argument("--export-views", action="store_true",
                   help="write the derived views into data/derived/<issuer>/ from the "
                        "snapshots already held: readable copies of the 8-K exhibits, one "
                        "Markdown file per call transcript, and 8k_cells.csv with one row per "
                        "cell of every release table; no network, and not combinable with "
                        "acquisition flags")
    p.add_argument("--refresh", action="store_true", help="add a new snapshot version")
    p.add_argument("--cache-only", action="store_true", help="zero network requests")
    p.add_argument("--quiet", action="store_true",
                   help="log warnings only; silences the per-dataset progress lines "
                        "and the run status line")
    p.add_argument("--ceiling", default=None,
                   help="per-provider request limits, for example alpha_vantage=10,sec=40; "
                        "transcripts count under their own alpha_vantage_transcripts key")
    args = p.parse_args(argv)

    _setup_logging(args.quiet)

    if args.export_csv or args.export_views:
        flags = [flag for flag, given in (("--export-csv", args.export_csv),
                                          ("--export-views", args.export_views)) if given]
        # an ignored acquisition flag would read as "refreshed" when nothing was fetched
        ignored = [name for name, given in (("--refresh", args.refresh),
                                           ("--sources", args.sources),
                                           ("--transcripts", args.transcripts),
                                           ("--earnings-8k", args.earnings_8k),
                                           ("--ceiling", args.ceiling)) if given]
        if ignored:
            print(f"{' and '.join(flags)} read only what is held and cannot be combined "
                  f"with {', '.join(ignored)} — run the pull first", file=sys.stderr)
            return 1
        failed = 0
        if args.export_csv:
            failed |= _export(args.issuer or args.ticker)
        if args.export_views:
            failed |= _export_views(args.issuer or args.ticker)
        return failed

    try:
        result = pull(args.ticker, issuer=args.issuer,
                      sources=[s.strip() for s in args.sources.split(",") if s.strip()]
                      if args.sources else None,
                      transcripts=_transcripts(args.transcripts)
                      if args.transcripts is not None else None,
                      eight_ks=args.earnings_8k,
                      cache_only=args.cache_only, refresh=args.refresh,
                      ceilings=_ceiling(args.ceiling) if args.ceiling else None)
    except (ValueError, PermissionError) as exc:
        print(exc, file=sys.stderr)
        return 1
    if not args.quiet:
        line = _status_line(result)
        if line:
            print(line, file=sys.stderr)
    print(json.dumps(result, indent=2, default=str))
    return 1 if result.get("status") in UNPUBLISHED_STATUSES else 0


if __name__ == "__main__":
    raise SystemExit(main())
