"""CLI — the single entry point (`financial-data-pull`). A thin wrapper over
`pull.pull`: one ticker, an optional source set, optional transcript quarters, an
optional 8-K depth. Nothing stands between the call and the network but the
optional `--ceiling` — except `--export-csv` and `--export-views`, which only
rewrite derived files from the snapshots already held."""
from __future__ import annotations

import argparse
import json
import sys

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
                        "(for example 2025Q1,2025Q2); needs alpha_vantage")
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
    p.add_argument("--ceiling", default=None,
                   help="per-provider request limits, for example alpha_vantage=10,sec=40; "
                        "transcripts count under their own alpha_vantage_transcripts key")
    args = p.parse_args(argv)

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
                      transcripts=[q.strip() for q in (args.transcripts or "").split(",")
                                   if q.strip()],
                      eight_ks=args.earnings_8k,
                      cache_only=args.cache_only, refresh=args.refresh,
                      ceilings=_ceiling(args.ceiling) if args.ceiling else None)
    except (ValueError, PermissionError) as exc:
        print(exc, file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, default=str))
    return 1 if result.get("status") in UNPUBLISHED_STATUSES else 0


if __name__ == "__main__":
    raise SystemExit(main())
