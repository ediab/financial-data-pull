"""As-filed arithmetic and release checks for a served evidence bundle."""
from __future__ import annotations

import re
from pathlib import Path

import pandas as pd

PERIOD = re.compile(r"^\d{4}-\d{2}-\d{2}")
REVENUE = "RevenueFromContractWithCustomerExcludingAssessedTax"
REVENUE_LABEL = r"revenue|net sales"
EQUITY = ("StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
          "StockholdersEquity")


def _cols(frame):
    return [str(c) for c in frame.columns if PERIOD.match(str(c))]


def _pick(frame, suffix, col, label=None):
    if col is None or col not in frame or "concept" not in frame.columns:
        return None
    rows = frame[frame.concept.astype(str).str.endswith(suffix, na=False)]
    if "dimension" in rows:
        rows = rows[~rows.dimension.astype(bool)]
    if label:
        if "label" not in rows.columns:
            return None
        rows = rows[rows.label.astype(str).str.contains(label, case=False, na=False, regex=True)]
    for value in pd.to_numeric(rows[col], errors="coerce"):
        if pd.notna(value):
            return float(value)
    return None


def _pick_revenue(frame, col):
    value = _pick(frame, REVENUE, col, REVENUE_LABEL)
    if value is None:
        # Some issuers use a different concept; identify the consolidated line by label.
        if "label" not in frame.columns or "concept" not in frame.columns:
            return None
        rows = frame[frame.label.astype(str).str.contains(REVENUE_LABEL, case=False,
                                                            na=False, regex=True)]
        if "dimension" in rows:
            rows = rows[~rows.dimension.astype(bool)]
        for row_value in pd.to_numeric(rows[col], errors="coerce"):
            if pd.notna(row_value):
                return float(row_value)
    return value


def _family(tables, prefix):
    return sorted((n for n in tables if n.startswith(prefix)),
                  key=lambda n: int(n.rsplit("_", 1)[-1]) if n.rsplit("_", 1)[-1].isdigit() else 999)


def _quarter(col):
    date = str(col)[:10]
    match = re.search(r"\((Q[1-4])\)", str(col))
    q = match.group(1) if match else f"Q{(int(date[5:7])-1)//3+1}"
    return f"{date[:4]}{q}", date


def _own_revenue(tables):
    """Revenue by reported quarter, own-filing evidence first.

    The prior-year quarter a 10-Q also states is not part of the window, but its YTD
    is the 9M a fiscal year whose own Q3 10-Q has dropped out of the window derives
    its Q4 from — the same evidence `views.build_history` uses, so the report and the
    history agree on how many quarters the record covers.
    """
    result = {}
    prior_ytd = {}
    for name in _family(tables, "income_quarterly_"):
        frame = tables[name]
        dated = sorted(c for c in _cols(frame) if "(YTD)" not in c)
        if not dated:
            continue
        col = dated[-1]
        q, end = _quarter(col)
        ytd = next((c for c in _cols(frame) if "(YTD)" in c and c[:10] == end), None)
        value = _pick_revenue(frame, col)
        result[q] = {"value": value, "end": end,
                     "ytd": _pick(frame, REVENUE, ytd, REVENUE_LABEL) if ytd else None}
        for prior in dated[:-1]:
            prior_q, prior_end = _quarter(prior)
            prior_col = next((c for c in _cols(frame) if "(YTD)" in c and c[:10] == prior_end), None)
            amount = _pick(frame, REVENUE, prior_col, REVENUE_LABEL) if prior_col else None
            if amount is not None:
                prior_ytd.setdefault(prior_q, amount)
    for name in _family(tables, "income_annual_"):
        frame = tables[name]
        cols = sorted(c for c in _cols(frame) if "(FY)" in c)
        if not cols:
            continue
        col = cols[-1]
        value = _pick_revenue(frame, col)
        q = f"{col[:4]}Q4"
        prior = result.get(f"{col[:4]}Q3", {}).get("ytd")
        if prior is None:
            prior = prior_ytd.get(f"{col[:4]}Q3")
        if value is not None and prior is not None:
            result[q] = {"value": value-prior, "end": col[:10], "ytd": None, "derived": True}
    return result


def _exhibits(issuer, exhibits):
    from . import views
    verified = list(views._verified_exhibits(issuer))
    seen = {e.get("accession") for e, _, _ in verified}
    for item in exhibits or ():
        if isinstance(item, tuple) and len(item) == 3:
            exhibit, payload, digest = item
        elif isinstance(item, dict):
            exhibit = item
            path = Path(item["path"])
            payload, digest = views._verified_bytes(path)
        else:
            path = Path(item)
            payload, digest = views._verified_bytes(path)
            exhibit = {"path": path, "accession": path.stem, "filing_date": ""}
        if exhibit.get("accession") not in seen:
            verified.append((exhibit, payload, digest))
            seen.add(exhibit.get("accession"))
    rows = []
    for exhibit, payload, digest in verified:
        try:
            rows.extend(views._exhibit_rows(payload, exhibit, digest))
        except (ValueError, TypeError):
            continue
    return rows


def verify_bundle(tables, issuer, exhibits=()) -> dict:
    """Check held statement values without modifying the evidence."""
    checks, gaps, identified = [], [], {}
    applicability = {}

    def add(name, tolerance, ok, detail, applicable=True):
        checks.append({"name": name, "ok": bool(ok), "tolerance": tolerance, "detail": detail})
        applicability[name] = applicable
        if not applicable:
            gaps.append(f"{name}: {detail}")

    revenue = _own_revenue(tables)
    inc_q = _family(tables, "income_quarterly_")
    identified["revenue"] = REVENUE if any(
        "concept" in tables[n] and any(str(c).endswith(REVENUE) for c in tables[n].concept)
        for n in inc_q) else "revenue|net sales label"

    balance_gaps, balance_n = [], 0
    equity_concept = None
    for name in _family(tables, "balance_annual_") + _family(tables, "balance_quarterly_"):
        frame = tables[name]
        for col in _cols(frame):
            a, l = _pick(frame, "Assets", col, r"^total"), _pick(frame, "Liabilities", col, r"^total")
            e = None
            for concept in EQUITY:
                e = _pick(frame, concept, col, r"total.*equity")
                if e is not None:
                    equity_concept = concept
                    break
            if None not in (a, l, e):
                balance_n += 1
                delta = abs(a-l-e)
                if delta >= 1000:
                    balance_gaps.append(f"{col[:10]} gap ${delta:,.0f}")
    identified["equity"] = equity_concept or list(EQUITY)
    add("balance", "$1,000", balance_n > 0 and not balance_gaps,
        "; ".join(balance_gaps) if balance_gaps else f"{balance_n} quarter-end checks" if balance_n else "no comparable balance values",
        bool(balance_n))

    ytd_gaps, ytd_n = [], 0
    for q, record in revenue.items():
        if not q.endswith(("Q2", "Q3")) or record.get("ytd") is None:
            continue
        parts = [revenue.get(f"{q[:4]}Q{i}", {}).get("value") for i in range(1, int(q[5])+1)]
        if all(v is not None for v in parts):
            ytd_n += 1
            delta = abs(record["ytd"] - sum(parts))
            if delta >= 1000:
                ytd_gaps.append(f"{q} gap ${delta:,.0f}")
    add("ytd_sum", "$1,000", ytd_n > 0 and not ytd_gaps,
        "; ".join(ytd_gaps) if ytd_gaps else f"{ytd_n} YTD checks" if ytd_n else "no comparable YTD values", bool(ytd_n))

    full_years = {str(c)[:4] for n in _family(tables, "income_annual_") for c in _cols(tables[n]) if "(FY)" in c}
    derivable_years = {year for year in full_years
                       if revenue.get(f"{year}Q3", {}).get("ytd") is not None}
    missing_q4 = [f"{year}Q4" for year in sorted(derivable_years) if f"{year}Q4" not in revenue]
    add("q4_fy", "derived Q4 present for each full year", not missing_q4,
        ", ".join(missing_q4) if missing_q4 else f"{len(derivable_years)} derivable full year(s)" ,
        bool(derivable_years))

    release_rows = _exhibits(issuer, exhibits)
    release_values = []
    for row in release_rows:
        label = str(row.get("row_label") or "")
        val = row.get("value")
        if val is not None and re.search(REVENUE_LABEL, label, re.I):
            try:
                release_values.append((str(row.get("filing_date") or ""), float(val)))
            except (TypeError, ValueError):
                continue
    release_failures, release_checked = [], 0
    for q, record in sorted(revenue.items()):
        value = record.get("value")
        if value is None:
            continue
        end = pd.Timestamp(record["end"])
        choices = []
        for filing_date, amount in release_values:
            try:
                days = (pd.Timestamp(filing_date)-end).days
            except (ValueError, TypeError):
                continue
            if 0 <= days <= 75:
                choices.append(amount)
        if not choices:
            gaps.append(f"revenue_release: {q} has no held release")
            continue
        release_checked += 1
        if not any(abs(amount*scale-value) <= .5*scale for amount in choices for scale in (1e6, 1e3)):
            release_failures.append(f"{q} gap: no release cell matches ${value:,.0f}")
    add("revenue_release", "0.5 × stated scale (1e6, then 1e3)",
        not release_failures,
        "; ".join(release_failures) if release_failures else f"{release_checked} matched; missing releases are gaps",
        True)

    cash_gaps, cash_n = [], 0
    for cf_name, bal_name in zip(_family(tables, "cashflow_quarterly_"), _family(tables, "balance_quarterly_")):
        cf, bal = tables[cf_name], tables[bal_name]
        for col in _cols(cf):
            end_cash = _pick(cf, "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents", col, r"end")
            bcol = next((c for c in _cols(bal) if c.startswith(col[:10])), None)
            cash = _pick(bal, "CashAndCashEquivalentsAtCarryingValue", bcol, r"^cash and cash equiv")
            if end_cash is not None and cash is not None:
                cash_n += 1
                delta = abs(end_cash-cash)
                if delta >= .05*5e9:
                    cash_gaps.append(f"{col[:10]} gap ${delta:,.0f}")
    add("cash_reconcile", "0.05 × 5e9", cash_n > 0 and not cash_gaps,
        "; ".join(cash_gaps) if cash_gaps else f"{cash_n} filing checks" if cash_n else "no comparable cash values", bool(cash_n))

    dates = sorted({record["end"] for record in revenue.values()})
    quarters = sorted(revenue)
    ords = [int(q[:4])*4+int(q[5])-1 for q in quarters]
    jumps = [f"{a}..{b}" for a, b, x, y in zip(quarters, quarters[1:], ords, ords[1:]) if y - x != 1]
    window = dates[-12:]
    day_gaps = [(pd.Timestamp(b)-pd.Timestamp(a)).days for a, b in zip(window, window[1:])]
    bad = [f"{a}..{b} ({d}d)" for a, b, d in zip(window, window[1:], day_gaps)
           if not 80 <= d <= 105]
    window_ok = len(dates) >= 11 and not jumps and not bad
    if jumps:
        window_note = f"missing quarter(s) between {jumps[0]}"
    elif bad:
        window_note = f"out-of-tolerance gap {bad[0]}"
    elif len(dates) == 11:
        window_note = "11 contiguous quarters noted"
    else:
        window_note = f"{len(dates)} quarter-ends"
    add("quarter_window", "12 contiguous quarter-ends; 80–105 day gaps (11 noted)", window_ok, window_note, bool(dates))

    failures = any(not check["ok"] and applicability[check["name"]]
                   for check in checks)
    required = ("balance", "ytd_sum", "q4_fy", "cash_reconcile", "quarter_window")
    complete = all(applicability[name] for name in required)
    verdict = ("DISCREPANCY" if failures else
               "CHECKED" if complete and all(check["ok"] for check in checks
                                              if applicability[check["name"]]) else
               "UNCHECKED")
    return {"verdict": verdict, "checks": checks, "identified": identified, "gaps": gaps}
