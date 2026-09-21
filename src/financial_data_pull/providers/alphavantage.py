"""Alpha Vantage EARNINGS_ESTIMATES.

Same storage/label rules as the Yahoo datasets.
Key from .env; never logged.
"""
from __future__ import annotations

import json
import urllib.request

from .. import config, store

BASE_URL = "https://www.alphavantage.co/query"


def earnings_estimates(symbol: str, horizon: str = "12month", issuer: str | None = None,
                       ceiling=None) -> tuple[dict | None, dict]:
    key = config.get("ALPHAVANTAGE_API_KEY")
    if not key:
        return None, {"provider": "alpha_vantage", "status": "MISSING",
                      "reason": "NOT_SUPPLIED", "detail": "ALPHAVANTAGE_API_KEY absent"}
    url = f"{BASE_URL}?function=EARNINGS_ESTIMATES&symbol={symbol}&horizon={horizon}&apikey={key}"
    if ceiling:
        ceiling.spend("alpha_vantage")
    try:
        with urllib.request.urlopen(url, timeout=30) as r:
            raw = r.read()
        data = json.loads(raw)
    except Exception as e:  # noqa: BLE001
        return None, {"provider": "alpha_vantage", "status": "FAILED",
                      "reason": "NOT_RETRIEVABLE", "detail": type(e).__name__}
    if not isinstance(data, dict):
        return None, {"provider": "alpha_vantage", "status": "PARSE_FAILED",
                      "reason": "NOT_RETRIEVABLE", "detail": "response is not a JSON object"}
    if "Information" in data or "Error Message" in data:
        return None, {"provider": "alpha_vantage", "status": "FAILED",
                      "reason": "NOT_RETRIEVABLE",
                      "detail": store.clean_text(str(data), 200)}
    preserved = store.save_raw(issuer, "alpha_vantage", raw, suffix=".json") if issuer else None
    est = data.get("estimates", [])
    if not isinstance(est, list):
        return None, {"provider": "alpha_vantage", "status": "PARSE_FAILED",
                      "reason": "NOT_RETRIEVABLE", "detail": "'estimates' is not a list"}
    meta = {
        "provider": "alpha_vantage",
        "status": "RETRIEVED" if est else "MISSING",
        "reason": None if est else "NOT_PUBLISHED",
        "entries": len(est),
        "horizon": horizon,
        "retrieved_at": store.now_iso(),
        "original_path": str(preserved) if preserved else None,
        "original_kind": "provider JSON response",
        # the provider states each estimate's fiscal period end, so the row is read
        # on an evidenced fiscal basis rather than a relative label
        "period_end_field": "date",
        "fiscal_period_ends": [entry.get("date") for entry in est],
        "label": ("Alpha Vantage EARNINGS_ESTIMATES snapshot — fiscal period ends explicit, "
                  "7/30/60/90-day revision history; never presented as consensus"),
    }
    return data, meta


def earnings_call_transcript(symbol: str, quarter: str, issuer: str | None = None,
                             ceiling=None) -> tuple[dict | None, dict]:
    """One company+quarter's earnings-call transcript.

    `quarter` is the provider's own `YYYYQN` label. Acquisition is deliberately one
    quarter per call so coverage is recorded per company+quarter and nothing is
    re-pulled once frozen. The raw JSON response is preserved and hash-checked like
    every other snapshot; a quarter the provider does not carry, or one its tier
    refuses, is reported MISSING with the provider's own message — never a fabricated
    or substituted transcript, and never silently skipped.
    """
    key = config.get("ALPHAVANTAGE_API_KEY")
    if not key:
        return None, _transcript_meta(quarter, "MISSING", "NOT_SUPPLIED",
                                      "ALPHAVANTAGE_API_KEY absent")
    url = (f"{BASE_URL}?function=EARNINGS_CALL_TRANSCRIPT&symbol={symbol}"
           f"&quarter={quarter}&apikey={key}")
    if ceiling:
        # transcripts have their own approved source scope: a plan approved for the
        # estimates snapshot never extends to a transcript call
        ceiling.spend("alpha_vantage_transcripts")
    try:
        with urllib.request.urlopen(url, timeout=30) as r:
            raw = r.read()
        data = json.loads(raw)
    except Exception as e:  # noqa: BLE001
        return None, _transcript_meta(quarter, "FAILED", "NOT_RETRIEVABLE",
                                      type(e).__name__)
    if not isinstance(data, dict):
        return None, _transcript_meta(quarter, "PARSE_FAILED", "NOT_RETRIEVABLE",
                                      "response is not a JSON object")
    # the provider signals an uncovered quarter, a premium-blocked endpoint or a
    # rate limit in a message field rather than an HTTP error
    message = (data.get("Information") or data.get("Note")
               or data.get("Error Message") or data.get("error"))
    if message:
        text = str(message)
        lowered = text.lower()
        # classification reads the provider's own words; only the copy that is stored
        # is redacted, so a URL or a key inside the message cannot reach a manifest
        detail = store.clean_text(text, 200)
        if ("spreading out your free api" in lowered or "rate limit" in lowered
                or "thank you for using alpha vantage" in lowered
                or "requests per second" in lowered or "requests per day" in lowered):
            # the free tier throttles bursts: this is a retryable transport limit, not
            # a coverage gap, and it must never be recorded as a missing quarter
            return None, _transcript_meta(quarter, "RATE_LIMITED", "NOT_RETRIEVABLE", detail)
        return None, _transcript_meta(quarter, "MISSING", "PREMIUM_OR_UNCOVERED", detail)
    preserved = store.save_raw(issuer, "alpha_vantage", raw, suffix=".json") if issuer else None
    transcript = data.get("transcript")
    if not isinstance(transcript, list):
        return None, _transcript_meta(quarter, "PARSE_FAILED", "NOT_RETRIEVABLE",
                                      "'transcript' is not a list")
    segments = [{"speaker": str(s.get("speaker") or ""),
                 "title": str(s.get("title") or ""),
                 "content": str(s.get("content") or "")}
                for s in transcript if isinstance(s, dict)]
    meta = _transcript_meta(quarter, "RETRIEVED" if segments else "MISSING",
                            None if segments else "NOT_PUBLISHED")
    meta.update({
        "segments": len(segments),
        "retrieved_at": store.now_iso(),
        "original_path": str(preserved) if preserved else None,
        "original_kind": "provider JSON response",
        "label": ("Alpha Vantage EARNINGS_CALL_TRANSCRIPT — prepared remarks and Q&A as "
                  "the provider segments them; statements in it are labelled management "
                  "guidance, never results"),
    })
    return {"symbol": symbol, "quarter": quarter, "segments": segments}, meta


def _transcript_meta(quarter: str, status: str, reason: str | None,
                     detail: str | None = None) -> dict:
    meta = {"provider": "alpha_vantage", "dataset": "av_transcript", "quarter": quarter,
            "status": status, "reason": reason}
    if detail:
        meta["detail"] = detail
    return meta
