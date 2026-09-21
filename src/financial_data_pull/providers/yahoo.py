"""Yahoo snapshots via yfinance.

Seven analyst datasets + prices, stored as separately-named tables per the
data contract. Snapshots only: provider, analyst count and as-of recorded.
Never labelled "consensus" without a declared consensus export.

Tables carry the `yahoo_` prefix: everything this provider returns is benchmark
material, acquired into the benchmark snapshot with its provider, analyst count
and as-of attribution. An estimate never fills a reported historical slot.

Every RETRIEVED dataset gets a table and a preserved payload; a dataset that is
absent or fails gets an explicit reason, and a failure also names the error, rather
than a bare status. yfinance exposes no raw response accessor, so the preserved
payload is the normalized frame and the manifest says so.
"""
from __future__ import annotations

import pandas as pd

from .. import store

DATASETS = (
    "yahoo_prices",
    "yahoo_earnings_estimate",
    "yahoo_revenue_estimate",
    "yahoo_eps_trend",
    "yahoo_eps_revisions",
    "yahoo_analyst_price_targets",
    "yahoo_recommendations",
    "yahoo_upgrades_downgrades",
)

_PAYLOAD_KIND = "normalized_frame (yfinance exposes no raw response accessor)"


def _meta(ticker: str, statuses: dict, reasons: dict, originals: dict,
          frames: dict | None = None, detail: str | None = None,
          failures: dict | None = None) -> dict:
    meta = {
        "ticker": ticker,
        "provider": "yahoo",
        "statuses": statuses,
        "reasons": reasons,
        "originals": originals,
        "originals_kind": _PAYLOAD_KIND,
        "retrieved_at": store.now_iso(),
        "label": "Yahoo snapshot — single provider, analyst counts per row",
        # period metadata the comparison needs rather than assuming: yfinance estimate
        # tables are indexed by *relative* labels (0q, +1q, 0y, +1y), which are only
        # resolvable against a stated as-of, never against the machine's current date
        "estimate_label_basis": ("provider-relative labels (0q/+1q/0y/+1y) unless a "
                                 "table states explicit fiscal periods"),
    }
    prices = (frames or {}).get("yahoo_prices")
    if prices is not None and len(prices.index):
        meta["price_as_of"] = str(prices.index[-1])[:10]
    if detail:
        meta["detail"] = detail
    if failures:
        # per-dataset error text, so a FAILED row says what broke and not just that
        # something did
        meta["failures"] = failures
    return meta


def fetch(ticker: str, issuer: str | None = None, ceiling=None) -> tuple[dict, dict]:
    """Fetch all datasets independently. Partial failure preserves successes;
    each dataset carries its own status and, when not retrieved, its reason."""
    from yfinance import Ticker
    frames: dict[str, pd.DataFrame] = {}
    statuses: dict[str, str] = {}
    reasons: dict[str, str] = {}
    originals: dict[str, str] = {}
    failures: dict[str, str] = {}
    try:
        t = Ticker(ticker)
    except Exception as e:  # noqa: BLE001 — constructor failure must not abort the snapshot
        statuses = {name: "FAILED" for name in DATASETS}
        reasons = {name: "NOT_RETRIEVABLE" for name in DATASETS}
        return frames, _meta(ticker, statuses, reasons, originals,
                             detail=store.clean_error(e))

    def grab(name, fn):
        try:
            if ceiling:
                ceiling.spend("yahoo")
            obj = fn()
            if obj is None:
                statuses[name], reasons[name] = "MISSING", "NOT_PUBLISHED"
                return
            if hasattr(obj, "empty"):
                if obj.empty:
                    statuses[name], reasons[name] = "MISSING", "NOT_PUBLISHED"
                    return
                frame = obj
            elif isinstance(obj, dict):
                # dict-valued results become a one-row table so a RETRIEVED
                # dataset always has a table behind it
                frame = pd.DataFrame([obj])
            else:
                frame = pd.DataFrame(obj)
            if frame.empty or frame.shape[1] == 0:
                # an empty frame is not evidence: RETRIEVED must mean values exist
                statuses[name], reasons[name] = "MISSING", "NOT_PUBLISHED"
                return
            statuses[name] = "RETRIEVED"
            frames[name] = frame
            if issuer:
                payload = frame.to_json(orient="split", date_format="iso").encode()
                originals[name] = str(store.save_raw(issuer, "yahoo", payload, suffix=".json"))
        except PermissionError:
            raise  # approved request ceiling breached — never swallowed
        except Exception as e:  # noqa: BLE001 — partial failure must not erase the rest
            statuses[name], reasons[name] = "FAILED", "NOT_RETRIEVABLE"
            failures[name] = store.clean_error(e)

    grab("yahoo_prices", lambda: t.history(period="1y", interval="1d"))
    grab("yahoo_earnings_estimate", lambda: t.earnings_estimate)
    grab("yahoo_revenue_estimate", lambda: t.revenue_estimate)
    grab("yahoo_eps_trend", lambda: t.eps_trend)
    grab("yahoo_eps_revisions", lambda: t.eps_revisions)
    grab("yahoo_analyst_price_targets", lambda: t.analyst_price_targets)
    grab("yahoo_recommendations", lambda: t.recommendations)
    grab("yahoo_upgrades_downgrades", lambda: t.upgrades_downgrades)

    return frames, _meta(ticker, statuses, reasons, originals, frames=frames,
                         failures=failures)
