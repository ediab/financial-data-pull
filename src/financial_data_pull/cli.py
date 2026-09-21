"""CLI — the single entry point (`financial-data-pull`). A thin wrapper over
`pull.pull`: one ticker, an optional source set, optional transcript quarters.
Nothing stands between the call and the network but the optional `--ceiling`."""
from __future__ import annotations

import argparse
import json
import sys

from .pull import SOURCES, pull

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
    p.add_argument("--refresh", action="store_true", help="add a new snapshot version")
    p.add_argument("--cache-only", action="store_true", help="zero network requests")
    p.add_argument("--ceiling", default=None,
                   help="per-provider request limits, for example alpha_vantage=10,sec=40; "
                        "transcripts count under their own alpha_vantage_transcripts key")
    args = p.parse_args(argv)

    try:
        result = pull(args.ticker, issuer=args.issuer,
                      sources=[s.strip() for s in args.sources.split(",") if s.strip()]
                      if args.sources else None,
                      transcripts=[q.strip() for q in (args.transcripts or "").split(",")
                                   if q.strip()],
                      cache_only=args.cache_only, refresh=args.refresh,
                      ceilings=_ceiling(args.ceiling) if args.ceiling else None)
    except (ValueError, PermissionError) as exc:
        print(exc, file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, default=str))
    return 1 if result.get("status") in UNPUBLISHED_STATUSES else 0


if __name__ == "__main__":
    raise SystemExit(main())
