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

# Item 2.02 is "Results of Operations and Financial Condition" — the earnings release.
EARNINGS_ITEM = "2.02"
# The press release of record; filers type the same exhibit as 99.1, 99.01 or 99.
EARNINGS_EXHIBIT_TYPES = ("EX-99.1", "EX-99.01", "EX-99")


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
    records that as MISSING, it is not an error. A statement that exists but cannot
    be converted is named in `meta["statements_unreadable"]` with its redacted
    reason, so the caller records a parse failure instead of blaming the filing.
    When `issuer` is given the filing's full-text submission is preserved as an
    immutable original.
    """
    from edgar import Company
    _identity()
    if ceiling:
        ceiling.spend("sec")
    c = Company(ticker)
    f = c.get_filings(form=form)[index]
    report = f.obj()
    frames: dict[str, pd.DataFrame] = {}
    absent: list[str] = []
    unreadable: dict[str, str] = {}
    for kind, attr in STATEMENT_KINDS.items():
        statement = getattr(report, attr, None)
        if statement is None:
            absent.append(kind)
            continue
        try:
            frames[kind] = statement.to_dataframe()
        except Exception as e:  # noqa: BLE001 — one unreadable statement, not a failed filing
            # the reason travels in the filing's meta so the caller can record a parse
            # failure instead of claiming the filing does not carry the statement
            unreadable[kind] = store.clean_error(e)
    meta = {
        "ticker": ticker, "form": str(f.form), "filing_date": str(f.filing_date),
        "period_of_report": str(getattr(f, "period_of_report", None)),
        "accession": str(getattr(f, "accession_no", None)),
        "statements": sorted(frames), "statements_absent": sorted(absent),
        "statements_unreadable": unreadable,
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


def _press_release(attachments):
    """The Exhibit 99.1 attachment of record, or None when the filing carries none."""
    for doc_type in EARNINGS_EXHIBIT_TYPES:
        matches = attachments.query(f"document_type == '{doc_type}'",
                                    include_data_files=False).documents
        if matches:
            return matches[0]
    return None


def earnings_8k(ticker: str, count: int, issuer: str | None = None,
                ceiling=None) -> tuple[pd.DataFrame | None, dict]:
    """The most recent Item 2.02 8-Ks, with their press-release exhibit preserved.

    The 2.02 filter reads SEC index metadata (`filing.items`), so it costs no
    requests; the full-text submission of each selected filing is fetched once and
    is also where the exhibit bytes come from. That is why an amendment is excluded
    from the query entirely — an 8-K/A restates a release rather than publishing one,
    and the original is the evidence worth keeping. A filing whose metadata carries
    no items cannot be classified, so it is recorded MISSING, never silently skipped.
    """
    from edgar import Company
    _identity()
    c = Company(ticker)
    filings = c.get_filings(form="8-K", amendments=False)
    records: list[dict] = []
    examined = 0
    selected = 0
    for filing in filings:
        if selected >= count:
            break  # the index is already in memory, so scanning costs no requests
        examined += 1
        record = {
            "ticker": ticker,
            "accession": str(getattr(filing, "accession_no", None)),
            "filing_date": str(getattr(filing, "filing_date", None)),
            "items": str(getattr(filing, "items", "") or "").strip(),
            "status": "RETRIEVED", "reason": None, "detail": None,
            "exhibit_file": None, "exhibit_path": None,
        }
        if not record["items"]:
            records.append({**record, "status": "MISSING", "reason": "NOT_PUBLISHED",
                            "detail": "SEC index metadata carries no items for this filing"})
            continue
        if EARNINGS_ITEM not in record["items"]:
            continue  # a different 8-K: not this acquisition's business
        selected += 1
        if ceiling:
            ceiling.spend("sec")
        try:
            exhibit = _press_release(filing.attachments)
        except Exception as e:  # noqa: BLE001 — one filing's failure is a coverage row
            records.append({**record, "status": "FAILED", "reason": "NOT_RETRIEVABLE",
                            "detail": store.clean_error(e)})
            continue
        if exhibit is None:
            records.append({**record, "status": "MISSING", "reason": "NOT_PUBLISHED",
                            "detail": "no Exhibit 99.1 in this filing"})
            continue
        content = exhibit.content
        if content is None or (isinstance(content, str) and not content.strip()):
            # an empty download is a retrieval failure, never a RETRIEVED "None" exhibit
            records.append({**record, "status": "FAILED", "reason": "NOT_RETRIEVABLE",
                            "detail": "the exhibit downloaded empty"})
            continue
        payload = content if isinstance(content, bytes) else str(content).encode()
        # the suffix is the exhibit's own: an EX-99.1 is usually HTML but can be a PDF,
        # and the preserved original must stay openable as what it is
        path = (store.save_raw(issuer, "sec", payload, suffix=exhibit.extension or "")
                if issuer else None)
        records.append({**record, "exhibit_file": str(exhibit.document),
                        "exhibit_path": str(path) if path else None})

    retrieved = [r for r in records if r["status"] == "RETRIEVED"]
    meta = {
        "provider": "sec", "dataset": "sec_8k", "ticker": ticker,
        "status": "RETRIEVED" if retrieved else "MISSING",
        "reason": None if retrieved else "NOT_PUBLISHED",
        "detail": None if retrieved else (
            f"no Item 2.02 8-K with a retrievable exhibit (examined {examined} filings)"),
        "examined": examined,
        "retrieved_at": store.now_iso(),
        "filings": records,
    }
    if not retrieved:
        return None, meta
    columns = ("ticker", "filing_date", "accession", "items", "exhibit_file", "exhibit_path")
    return pd.DataFrame([{k: r[k] for k in columns} for r in retrieved]), meta
