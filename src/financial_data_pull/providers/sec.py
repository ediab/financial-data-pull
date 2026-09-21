"""SEC EDGAR via EdgarTools.

The only module that opens network connections to SEC. Needs EDGAR_IDENTITY
from .env.

One filing is fetched once and yields every statement it carries, because the
filing's full-text submission is the expensive part and the originals must be
preserved per filing before any normalisation.
"""
from __future__ import annotations

import pandas as pd

from .. import config, store

# statement attribute on an edgar company report -> our dataset kind
STATEMENT_KINDS = {
    "income": "income_statement",
    "balance": "balance_sheet",
    "cashflow": "cash_flow_statement",
}


def _identity() -> str:
    ident = config.get("EDGAR_IDENTITY")
    if not ident:
        raise RuntimeError("EDGAR_IDENTITY missing from .env — required by SEC policy")
    from edgar import set_identity
    set_identity(ident)
    return ident


def statements(ticker: str, form: str, index: int = 0, issuer: str | None = None,
               ceiling=None) -> tuple[dict[str, pd.DataFrame], dict]:
    """Every statement this filing carries, from a single fetch of the filing.

    Returns ({"income"|"balance"|"cashflow": frame}, meta). A statement the filing
    genuinely does not carry is simply absent from the mapping — the caller
    records that as MISSING, it is not an error. When `issuer` is given the
    filing's full-text submission is preserved as an immutable original.
    """
    from edgar import Company
    _identity()
    if ceiling:
        ceiling.spend("sec")
    c = Company(ticker)
    f = c.get_filings(form=form)[index]
    report = f.obj()
    frames: dict[str, pd.DataFrame] = {}
    missing: list[str] = []
    for kind, attr in STATEMENT_KINDS.items():
        statement = getattr(report, attr, None)
        if statement is None:
            missing.append(kind)
            continue
        try:
            frames[kind] = statement.to_dataframe()
        except Exception:  # noqa: BLE001 — an unreadable statement is absent, not fatal
            missing.append(kind)
    meta = {
        "ticker": ticker, "form": str(f.form), "filing_date": str(f.filing_date),
        "period_of_report": str(getattr(f, "period_of_report", None)),
        "accession": str(getattr(f, "accession_no", None)),
        "statements": sorted(frames), "statements_absent": sorted(missing),
        "retrieved_at": store.now_iso(),
        "as_of": str(getattr(f, "period_of_report", None)),
    }
    if issuer:
        if ceiling:
            ceiling.spend("sec")
        text = f.full_text_submission()
        preserved = store.save_raw(issuer, "sec", str(text).encode(), suffix=".txt")
        meta["original_path"] = str(preserved)
        meta["original_kind"] = "filing full-text submission"
    return frames, meta



