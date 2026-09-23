#!/usr/bin/env python3
"""Pull orchestration, offline: caching, per-source snapshots, honest gaps.

No network: two providers are replaced by doubles and the Alpha Vantage path
reads a patched HTTP response through the real provider code. The three success
examples from the brief are covered — a repeat pull is CACHED with zero provider
calls, a source set publishes its own snapshot and is not a cache hit for
another, and a failing provider degrades to coverage rows instead of raising.

The SEC double serves a configurable filing history: 12 reported quarters,
AVGO-shaped (a fiscal year ending early November) and VRT-shaped (a calendar year),
since otherwise every pull test can only ever see one filing per form.
"""
from __future__ import annotations

import io
import itertools
import json
import logging
import sys
from contextlib import ExitStack
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import pandas as pd

from financial_data_pull import contracts, store
from financial_data_pull.cli import main
from financial_data_pull.providers import alphavantage, sec, yahoo
from financial_data_pull.pull import (QUARTER_LABEL, _statements_from_sec, export_csv,
                                      manifest, pull, read_table, reported_quarters,
                                      scope_key)
from financial_data_pull.pull import logger as pull_logger

from test_store import _TempPlane


def _http(payload: dict) -> mock.MagicMock:
    """A urlopen replacement: one fresh, readable response per call.

    The stream must be new per call — a BytesIO left open by the caller is closed by
    its own `__exit__` (monkeypatching `__exit__` on the instance does not affect the
    implicit dunder lookup), so a shared one serves exactly one request.
    """
    body = json.dumps(payload).encode()
    return mock.MagicMock(side_effect=lambda *args, **kwargs: io.BytesIO(body))


# --- the filing histories the tests model -----------------------------------------
# A shape is a company's filing history plus the two lines that are not uniform across
# issuers: the consolidated revenue label and the equity total's concept. Each form's
# period ends are newest first, the order the acquisition loop asks its filings in.
# Twelve reported quarters = 3 annual 10-Ks + 9 quarterly 10-Qs, which for AVGO (a
# fiscal year ending early November) run 2023Q4..2026Q3 and for VRT (a calendar year)
# 2023Q3..2026Q2.
_AVGO_EQUITY = ("us-gaap_StockholdersEquity"
                "IncludingPortionAttributableToNoncontrollingInterest")
_REVENUE_CONCEPT = "us-gaap_RevenueFromContractWithCustomerExcludingAssessedTax"
AVGO_SHAPE = {
    "periods": {
        "10-K": ["2025-11-02", "2024-11-03", "2023-10-29"],
        "10-Q": ["2026-08-02", "2026-05-03", "2026-02-01",
                 "2025-08-03", "2025-05-04", "2025-02-02",
                 "2024-08-04", "2024-05-05", "2024-02-04"],
    },
    "revenue_label": "Total net revenue",
    "equity_concept": _AVGO_EQUITY,
}
VRT_SHAPE = {
    "periods": {
        "10-K": ["2025-12-31", "2024-12-31", "2023-12-31"],
        "10-Q": ["2026-06-30", "2026-03-31",
                 "2025-09-30", "2025-06-30", "2025-03-31",
                 "2024-09-30", "2024-06-30", "2024-03-31",
                 "2023-09-30"],
    },
    "revenue_label": "Net sales",
    "equity_concept": "us-gaap_StockholdersEquity",
}
# the one-filing-per-form shape every pre-existing test was written against
_DEFAULT_SHAPE = {"periods": {"10-K": ["2025-01-31"], "10-Q": ["2025-01-31"]},
                  "revenue_label": "Total revenue",
                  "equity_concept": "us-gaap_StockholdersEquity"}
# 2023Q3: the first quarter either named shape reports
_ORDINAL_ZERO = 2023 * 4 + 2
_FILING_LAG_DAYS = {"10-K": 60, "10-Q": 35}
_RESTRICTED_CASH = 25_000_000


def _reported_label(period_end: str) -> str:
    """The `YYYYQN` a period end reports as: the calendar quarter containing it.

    Both shapes name their fiscal quarters the calendar way (AVGO's August-end is
    fiscal Q3, VRT's June-end Q2), which is the coincidence the brief's footnote
    flags: the fixture does not model a January-year-end issuer.
    """
    return f"{period_end[:4]}Q{(int(period_end[5:7]) - 1) // 3 + 1}"


def _quarter_index(period_end: str) -> int:
    """A quarter's position on the calendar grid, so arithmetic never reads a label."""
    return int(period_end[:4]) * 4 + (int(period_end[5:7]) - 1) // 3


def _end_of(ends: dict, year: int, quarter: int) -> str:
    """The period end of one calendar quarter.

    The shape's own date when its history holds that filing; otherwise the same
    quarter shifted off any date the shape does carry — a comparative column older
    than the acquired window, whose date only has to land in the right quarter.
    """
    known = ends.get((year, quarter))
    if known:
        return known
    anchor = next(iter(ends.values()))
    months = 3 * (year * 4 + quarter - 1 - _quarter_index(anchor))
    return (pd.Timestamp(anchor) + pd.DateOffset(months=months)).date().isoformat()


def _model(period_end: str) -> dict[str, int]:
    """One quarter of the modelled company in whole dollars — a pure function of the
    quarter, so a comparative column and an own-period column agree by construction
    and the checks' arithmetic (Assets = Liabilities + Equity, YTD = Σ Qn) holds."""
    step = _quarter_index(period_end) - _ORDINAL_ZERO
    equity = 20_000_000_000 + 500_000_000 * step
    liabilities = 30_000_000_000 + 400_000_000 * step
    return {"revenue": 6_000_000_000 + 250_000_000 * step,
            "equity": equity, "liabilities": liabilities,
            "assets": equity + liabilities,
            "cash": 3_000_000_000 + 100_000_000 * step}


def _ytd(ends: dict, period_end: str) -> int:
    """Revenue for the fiscal year through this period: what a 10-Q's `(YTD)` states."""
    year, quarter = int(period_end[:4]), (int(period_end[5:7]) - 1) // 3 + 1
    return sum(_model(_end_of(ends, year, q))["revenue"] for q in range(1, quarter + 1))


def _fy(ends: dict, year: int) -> int:
    """Revenue for one full fiscal year: what a 10-K's `(FY)` column states."""
    return sum(_model(_end_of(ends, year, q))["revenue"] for q in (1, 2, 3, 4))


def _statement_row(concept: str, label: str, columns: list[str], values: list[int],
                   dimension: bool = False) -> dict:
    return {"concept": concept, "label": label, "dimension": dimension,
            **dict(zip(columns, values))}


def _frame(rows: list[dict], columns: list[str]) -> pd.DataFrame:
    """A statement in the provider's shape: the reserved columns, then the filing's own
    period columns in document order."""
    return pd.DataFrame(rows, columns=["concept", "label", "dimension", *columns])


def _statement_frames(shape: dict, ends: dict, form: str, period_end: str) -> dict:
    """The three statements one filing carries, as edgartools hands them over.

    A 10-Q states its own quarter against the prior-year comparative and the year to
    date; a 10-K states the full year and its two comparatives. Balance sheets are
    point-in-time (bare dates, own plus prior fiscal year end) and cash flow is
    year-to-date only — the shapes the real frames carry, so the later checks are
    exercised against what the providers actually return.
    """
    year, quarter = int(period_end[:4]), (int(period_end[5:7]) - 1) // 3 + 1
    if form == "10-K":
        fy_ends = [_end_of(ends, year - k, 4) for k in (0, 1, 2)]
        income_cols = [f"{end} (FY)" for end in fy_ends]
        income_values = [_fy(ends, year - k) for k in (0, 1, 2)]
        cashflow_cols = income_cols
        cashflow_values = [_model(end)["cash"] + _RESTRICTED_CASH for end in fy_ends]
    else:
        prior = _end_of(ends, year - 1, quarter)
        income_cols = [f"{period_end} (Q{quarter})", f"{prior} (Q{quarter})",
                       f"{period_end} (YTD)", f"{prior} (YTD)"]
        income_values = [_model(period_end)["revenue"], _model(prior)["revenue"],
                         _ytd(ends, period_end), _ytd(ends, prior)]
        cashflow_cols = [f"{period_end} (YTD)", f"{prior} (YTD)"]
        cashflow_values = [_model(period_end)["cash"] + _RESTRICTED_CASH,
                           _model(prior)["cash"] + _RESTRICTED_CASH]
    balance_cols = [period_end, _end_of(ends, year - 1, 4)]
    snapshots = [_model(end) for end in balance_cols]
    income = _frame(
        [_statement_row(_REVENUE_CONCEPT, shape["revenue_label"], income_cols, income_values),
         # a segment row shares the concept and is not the company total: `dimension`
         _statement_row(_REVENUE_CONCEPT, "Products - Asia Pacific", income_cols,
                        [v // 4 for v in income_values], dimension=True)],
        income_cols)
    balance = _frame(
        [_statement_row("us-gaap_Assets", "Total assets", balance_cols,
                        [m["assets"] for m in snapshots]),
         _statement_row("us-gaap_Liabilities", "Total liabilities", balance_cols,
                        [m["liabilities"] for m in snapshots]),
         _statement_row(shape["equity_concept"], "Total stockholders' equity",
                        balance_cols, [m["equity"] for m in snapshots]),
         _statement_row("us-gaap_CashAndCashEquivalentsAtCarryingValue",
                        "Cash and cash equivalents", balance_cols,
                        [m["cash"] for m in snapshots])],
        balance_cols)
    cashflow = _frame(
        [_statement_row("us-gaap_CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",
                        "Cash, cash equivalents and restricted cash at end of period",
                        cashflow_cols, cashflow_values)],
        cashflow_cols)
    return {"income": income, "balance": balance, "cashflow": cashflow}


def _filing_date(period_end: str, form: str) -> str:
    """A filing lands after its period ends: ~5 weeks for a 10-Q, ~2 months for a 10-K."""
    lag = pd.DateOffset(days=_FILING_LAG_DAYS.get(form, 60))
    return (pd.Timestamp(period_end) + lag).date().isoformat()


def _accession(form: str, index: int, period_end: str) -> str:
    """A distinct accession per filing, in EDGAR's own shape."""
    base = 100000 if form == "10-K" else 500000
    return f"0000000000-{period_end[2:4]}-{base + index:06d}"


class _Sec:
    """SEC statements as the provider serves them: a configurable run of filings per
    form, one filing per form by default.

    `shape` names each form's period ends, newest first, with the labels that differ
    across issuers; every filing then has its own period end, filing date, accession
    and statements, so a 12-quarter shape is a realistic company record rather than one
    filing repeated. `fail=True` raises for every filing, and an index past the shape's
    history raises as upstream reads a filing that is not there.
    """

    def __init__(self, fail: bool = False, shape: dict | None = None):
        self.calls = 0
        self.fail = fail
        self.shape = shape or _DEFAULT_SHAPE
        self.periods = {form: list(ends)
                        for form, ends in self.shape["periods"].items()}
        # every quarter-end the history carries, so a comparative column names the date
        # the filing that owns that quarter states rather than a recomputed one
        self.ends = {(int(end[:4]), (int(end[5:7]) - 1) // 3 + 1): end
                     for ends in self.periods.values() for end in ends}
        self.frames = {form: [_statement_frames(self.shape, self.ends, form, end)
                              for end in ends]
                       for form, ends in self.periods.items()}

    def __call__(self, ticker, form, index=0, issuer=None, ceiling=None):
        self.calls += 1
        history = self.periods.get(form) or []
        if self.fail or index >= len(history):
            raise RuntimeError("no such filing")
        if ceiling:
            ceiling.spend("sec")
        period_end = history[index]
        frames = self.frames[form][index]
        meta = {"ticker": ticker, "form": form,
                "filing_date": _filing_date(period_end, form),
                "period_of_report": period_end,
                "accession": _accession(form, index, period_end),
                "statements": sorted(frames),
                "retrieved_at": store.now_iso(), "as_of": period_end}
        if issuer:
            meta["original_path"] = str(store.save_raw(issuer, "sec", b"<filing/>", suffix=".txt"))
        return dict(frames), meta


class _Yahoo:
    """The eight datasets as tiny frames, each with a preserved payload."""

    def __init__(self, fail: bool = False):
        self.calls = 0
        self.fail = fail

    def __call__(self, ticker, issuer=None, ceiling=None):
        self.calls += 1
        if self.fail:
            raise RuntimeError("yahoo unavailable")
        frames, statuses, reasons, originals = {}, {}, {}, {}
        for name in yahoo.DATASETS:
            if ceiling:
                ceiling.spend("yahoo")
            frame = pd.DataFrame({"Close": [1.0, 2.0]})
            frames[name] = frame
            statuses[name] = "RETRIEVED"
            if issuer:
                payload = frame.to_json(orient="split", date_format="iso").encode()
                originals[name] = str(store.save_raw(issuer, "yahoo", payload, suffix=".json"))
        return frames, {"ticker": ticker, "provider": "yahoo", "statuses": statuses,
                        "reasons": reasons, "originals": originals,
                        "retrieved_at": store.now_iso()}


class _Estimates:
    """Alpha Vantage EARNINGS_ESTIMATES: one entry, or an explicit failure."""

    def __init__(self, fail: bool = False):
        self.calls = 0
        self.fail = fail

    def __call__(self, symbol, horizon="12month", issuer=None, ceiling=None):
        self.calls += 1
        if ceiling:
            ceiling.spend("alpha_vantage")
        if self.fail:
            raise RuntimeError("alpha vantage unavailable")
        data = {"estimates": [{"fiscal_date_ending": "2025-01-31",
                               "estimated_eps_avg": "1.20"}]}
        meta = {"provider": "alpha_vantage", "status": "RETRIEVED", "reason": None,
                "retrieved_at": store.now_iso(), "original_path": None}
        if issuer:
            meta["original_path"] = str(store.save_raw(
                issuer, "alpha_vantage", json.dumps(data).encode(), suffix=".json"))
        return data, meta


class _EightK:
    """Item 2.02 8-Ks as filed: a press release each, or metadata that cannot be read."""

    def __init__(self, filings=None, fail: bool = False):
        self.calls = 0
        self.counts: list[int] = []
        self.fail = fail
        self.filings = filings if filings is not None else [
            {"accession": "0000000000-25-000010", "filing_date": "2025-02-06",
             "items": "2.02,9.01"},
            {"accession": "0000000000-24-000011", "filing_date": "2024-10-30",
             "items": "2.02,9.01"},
        ]

    def __call__(self, ticker, count, issuer=None, ceiling=None):
        self.calls += 1
        self.counts.append(count)
        if self.fail:
            raise RuntimeError("8-K index unavailable")
        records = []
        for filing in self.filings[:count]:
            if ceiling:
                ceiling.spend("sec")
            record = {**filing, "ticker": ticker, "status": "RETRIEVED", "reason": None,
                      "detail": None, "exhibit_file": None, "exhibit_path": None}
            if not filing.get("items"):
                record.update(status="MISSING", reason="NOT_PUBLISHED",
                              detail="SEC index metadata carries no items for this filing")
            else:
                payload = f"<html>release {filing['accession']}</html>".encode()
                path = (store.save_raw(issuer, "sec", payload, suffix=".htm")
                        if issuer else None)
                record["exhibit_file"] = f"ex99-{filing['accession']}.htm"
                record["exhibit_path"] = str(path) if path else None
            records.append(record)
        retrieved = [r for r in records if r["status"] == "RETRIEVED"]
        meta = {"provider": "sec", "dataset": "sec_8k", "ticker": ticker,
                "status": "RETRIEVED" if retrieved else "MISSING",
                "reason": None if retrieved else "NOT_PUBLISHED",
                "detail": None if retrieved else "no Item 2.02 8-K with a retrievable exhibit",
                "retrieved_at": store.now_iso(), "filings": records}
        if not retrieved:
            return None, meta
        columns = ("ticker", "filing_date", "accession", "items", "exhibit_file",
                   "exhibit_path")
        return pd.DataFrame([{k: r[k] for k in columns} for r in retrieved]), meta


def _ageing_clock():
    """A strictly increasing timestamp per call: two pulls in one test would otherwise
    share a second, and which snapshot is newest would come down to its random suffix."""
    ticks = itertools.count()
    return mock.Mock(side_effect=lambda: f"2026-09-21T14:{next(ticks):02d}:00+0000")


class _Transcripts:
    """Earnings-call transcripts as Alpha Vantage serves them: one segment per quarter."""

    def __init__(self, fail: bool = False):
        self.calls = 0
        self.quarters: list[str] = []
        self.fail = fail

    def __call__(self, symbol, quarter, issuer=None, ceiling=None):
        self.calls += 1
        self.quarters.append(quarter)
        if self.fail:
            return None, {"provider": "alpha_vantage", "dataset": "av_transcript",
                          "quarter": quarter, "status": "FAILED",
                          "reason": "NOT_RETRIEVABLE", "detail": "transcripts unavailable"}
        if ceiling:
            ceiling.spend("alpha_vantage_transcripts")
        data = {"symbol": symbol, "quarter": quarter,
                "segments": [{"speaker": "Operator", "title": "Operator",
                              "content": "Welcome to the call."}]}
        meta = {"provider": "alpha_vantage", "dataset": "av_transcript",
                "quarter": quarter, "status": "RETRIEVED", "reason": None,
                "segments": 1, "retrieved_at": store.now_iso(), "original_path": None}
        if issuer:
            meta["original_path"] = str(store.save_raw(
                issuer, "alpha_vantage", json.dumps(data).encode(), suffix=".json"))
        return data, meta


_real_transcript = alphavantage.earnings_call_transcript


def _patched(s, y, a, e=None, transcripts=None) -> ExitStack:
    """All providers doubled: no test in this file can reach the network.

    The earnings-call transcript provider is doubled too — a plain pull now derives
    the last four quarters, so a real call would spend quota. The few tests that
    exercise the real provider through a patched `urlopen` pass
    `transcripts=_real_transcript`.
    """
    stack = ExitStack()
    stack.enter_context(mock.patch.object(sec, "statements", s))
    stack.enter_context(mock.patch.object(yahoo, "fetch", y))
    stack.enter_context(mock.patch.object(alphavantage, "earnings_estimates", a))
    stack.enter_context(mock.patch.object(sec, "earnings_8k", e or _EightK()))
    stack.enter_context(mock.patch.object(alphavantage, "earnings_call_transcript",
                                          transcripts or _Transcripts()))
    return stack


def _coverage(result: dict) -> dict:
    return json.loads(Path(result["coverage_path"]).read_text())


def _tree(root: Path) -> dict[str, str]:
    """Every file of a snapshot with its hash — the whole published version."""
    return {str(p.relative_to(root)): store.sha256_file(p)
            for p in sorted(root.rglob("*")) if p.is_file()}


class _LogCapture(logging.Handler):
    """Collect the library logger's records without touching the root configuration.

    The library adds no output handler, so a test reads the records at the logger it
    emits from (the same seam a caller configuring logging would use) rather than
    relying on a global handler that a passing suite would otherwise have to clean up.
    """

    def __init__(self, logger: logging.Logger):
        super().__init__(level=logging.NOTSET)
        self.records: list[logging.LogRecord] = []
        self._logger = logger

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)

    def __enter__(self) -> "_LogCapture":
        self._logger.addHandler(self)
        return self

    def __exit__(self, *exc) -> None:
        self._logger.removeHandler(self)

    def messages(self, level: int) -> list[str]:
        return [r.getMessage() for r in self.records if r.levelno == level]


def test_the_sec_fixture_serves_a_twelve_quarter_avgo_history():
    """The AVGO shape: 12 distinct reported quarters, 2023Q4..2026Q3, each filing its
    own dates and statements, and arithmetic the later checks can rely on."""
    with _TempPlane():
        with mock.patch.object(sec, "statements", _Sec(shape=AVGO_SHAPE)):
            tables, meta, _ = _statements_from_sec("AVGO", "AVGO", None, store.now_iso())
    periods = [m["period_of_report"] for m in meta.values()]
    assert len(periods) == len(set(periods)) == 12, periods
    assert len({m["filing_date"] for m in meta.values()}) == 12, meta
    assert len({m["accession"] for m in meta.values()}) == 12, meta
    assert sorted({_reported_label(period) for period in periods}) == [
        "2023Q4", "2024Q1", "2024Q2", "2024Q3", "2024Q4", "2025Q1",
        "2025Q2", "2025Q3", "2025Q4", "2026Q1", "2026Q2", "2026Q3"], periods
    # an AVGO quarter ends in early Feb/May/Aug/Nov, and the frame labels it so
    assert "2026-08-02 (Q3)" in tables["income_quarterly_0"].columns
    assert "2026-08-02 (YTD)" in tables["income_quarterly_0"].columns
    assert "2025-11-02 (FY)" in tables["income_annual_0"].columns
    assert "2026-08-02" in tables["balance_quarterly_0"].columns
    # the ties the checks read: YTD = Σ quarters of its fiscal year, Assets = L + E
    income = tables["income_quarterly_0"]
    quarters = [tables[f"income_quarterly_{i}"].loc[0, column]
                for i, column in ((2, "2026-02-01 (Q1)"), (1, "2026-05-03 (Q2)"),
                                  (0, "2026-08-02 (Q3)"))]
    assert income.loc[0, "2026-08-02 (YTD)"] == sum(quarters), quarters
    balance = tables["balance_quarterly_0"]
    assert balance.loc[0, "2026-08-02"] == (balance.loc[1, "2026-08-02"]
                                             + balance.loc[2, "2026-08-02"])
    # the segment row shares the concept with the consolidated one, marked as a segment
    assert list(income["dimension"]) == [False, True], income
    print("  the SEC fixture serves a twelve-quarter AVGO history ✓")


def test_the_sec_fixture_serves_a_calendar_year_vrt_history():
    """The VRT shape: calendar quarter ends, so the newest four reported quarters are
    exactly the labels the old calendar rule derived."""
    with _TempPlane():
        with mock.patch.object(sec, "statements", _Sec(shape=VRT_SHAPE)):
            tables, meta, _ = _statements_from_sec("VRT", "VRT", None, store.now_iso())
    periods = [m["period_of_report"] for m in meta.values()]
    assert len(periods) == len(set(periods)) == 12, periods
    labels = sorted({_reported_label(period) for period in periods})
    assert labels == ["2023Q3", "2023Q4", "2024Q1", "2024Q2", "2024Q3", "2024Q4",
                      "2025Q1", "2025Q2", "2025Q3", "2025Q4", "2026Q1", "2026Q2"], labels
    assert labels[-4:] == ["2025Q3", "2025Q4", "2026Q1", "2026Q2"], labels
    assert "2026-06-30 (Q2)" in tables["income_quarterly_0"].columns
    assert "2025-12-31" in tables["balance_quarterly_0"].columns
    print("  the SEC fixture serves a calendar-year VRT history ✓")


def test_the_sec_fixture_still_raises_for_a_filing_it_does_not_hold():
    """The default stays one filing per form, a later index is absent as upstream reads
    it, and a failing run claims no periods it never retrieved."""
    with _TempPlane():
        default = _Sec()
        _, meta = default("NVDA", "10-K", 0)
        assert meta["period_of_report"] == "2025-01-31", meta
        for double, form, index in ((default, "10-K", 1), (default, "10-Q", 8),
                                    (_Sec(fail=True, shape=AVGO_SHAPE), "10-K", 0),
                                    (_Sec(shape={"periods": {"10-K": []},
                                                "revenue_label": "Total revenue",
                                                "equity_concept": "us-gaap_StockholdersEquity"}),
                                     "10-K", 0)):
            try:
                double("NVDA", form, index)
            except RuntimeError as exc:
                assert "no such filing" in str(exc), exc
            else:
                raise AssertionError(f"{form} index {index} should be absent")
        with mock.patch.object(sec, "statements", _Sec(fail=True, shape=AVGO_SHAPE)):
            tables, meta, _ = _statements_from_sec("AVGO", "AVGO", None, store.now_iso())
        assert tables == {}, tables
        assert not any(m.get("period_of_report") for m in meta.values()), meta
    print("  the SEC fixture still raises for a filing it does not hold ✓")


def test_estimate_metadata_names_a_period_field_the_payload_carries():
    """A period-end field the response does not carry records nulls forever."""
    payload = {"estimates": [{"date": "2027-12-31", "horizon": "fiscal year",
                              "eps_estimate_average": "9.1224"},
                             {"date": "2027-09-30", "horizon": "fiscal quarter",
                              "eps_estimate_average": "1.9138"}]}
    with mock.patch.object(alphavantage, "config") as cfg:
        cfg.get.return_value = "key"
        with mock.patch.object(alphavantage.urllib.request, "urlopen", _http(payload)):
            data, meta = alphavantage.earnings_estimates("VRT")
    assert meta["status"] == "RETRIEVED", meta
    assert meta["period_end_field"] in payload["estimates"][0], meta["period_end_field"]
    assert meta["fiscal_period_ends"] == ["2027-12-31", "2027-09-30"], meta["fiscal_period_ends"]
    assert [entry["date"] for entry in data["estimates"]] == meta["fiscal_period_ends"]
    print("  estimate metadata names a period field the payload carries ✓")


def test_reported_quarters_uses_filing_periods_and_skips_missing_entries():
    periods = [period for history in AVGO_SHAPE["periods"].values() for period in history]
    meta = {f"filing_{i}": {"period_of_report": period} for i, period in enumerate(periods)}
    meta.update(failed={"status": "FAILED", "period_of_report": None},
                absent={"status": "FAILED"}, nan={"period_of_report": float("nan")})
    expected = ["2023Q4", "2024Q1", "2024Q2", "2024Q3", "2024Q4", "2025Q1",
                "2025Q2", "2025Q3", "2025Q4", "2026Q1", "2026Q2", "2026Q3"]
    assert reported_quarters(meta) == expected
    assert reported_quarters({"a": {"period_of_report": "2025-12-31"},
                              "b": {"period_of_report": "2025-03-31"},
                              "duplicate": {"period_of_report": "2025-12-31"}},
                             count=12) == ["2025Q1", "2025Q4"]
    assert reported_quarters(meta, count=4) == ["2025Q4", "2026Q1", "2026Q2", "2026Q3"]
    vrt = {f"filing_{i}": {"period_of_report": period}
           for i, period in enumerate([*VRT_SHAPE["periods"]["10-K"],
                                       *VRT_SHAPE["periods"]["10-Q"]])}
    assert reported_quarters(vrt)[-4:] == ["2025Q3", "2025Q4", "2026Q1", "2026Q2"]


def test_default_transcripts_without_sec_evidence_require_explicit_labels():
    with _TempPlane():
        with _patched(_Sec(), _Yahoo(), _Estimates()):
            try:
                pull("NVDA", sources=["alpha_vantage"])
            except ValueError as exc:
                assert str(exc) == (
                    "transcripts=[…] required: no SEC evidence to derive reported quarters")
            else:
                raise AssertionError("a transcript scope without SEC evidence must be explicit")


def test_a_first_pull_derives_twelve_transcript_quarters_from_its_sec_fetch():
    with _TempPlane():
        t, e = _Transcripts(), _EightK()
        with _patched(_Sec(shape=AVGO_SHAPE), _Yahoo(), _Estimates(), e, transcripts=t):
            result = pull("AVGO")
        expected = ["2023Q4", "2024Q1", "2024Q2", "2024Q3", "2024Q4", "2025Q1",
                    "2025Q2", "2025Q3", "2025Q4", "2026Q1", "2026Q2", "2026Q3"]
        assert result["status"] == "RETRIEVED", result
        assert t.quarters == expected, (t.quarters, expected)
        assert result["scope_key"] == scope_key(
            "AVGO", "AVGO", ["sec", "alpha_vantage", "yahoo"], expected, 12)
        assert result["eight_ks"] == 12 and e.counts == [12], (result, e.counts)
        assert result["reported_quarters"] == expected
        assert "av_transcript" in result["table_hashes"], result["table_hashes"]
        print("  a first pull derives 12 quarters from its own SEC fetch ✓")


def test_held_ticker_without_transcripts_serves_and_names_the_absence():
    with _TempPlane():
        sec_double, transcripts = _Sec(shape=AVGO_SHAPE), _Transcripts()
        with _patched(sec_double, _Yahoo(), _Estimates(), transcripts=transcripts):
            held = pull("AVGO", sources=["sec"], eight_ks=0)
            sec_calls = sec_double.calls
            acquired = pull("AVGO", sources=["alpha_vantage"])
        assert held["status"] == "RETRIEVED" and acquired["status"] == "CACHED"
        assert transcripts.quarters == [], "serving held SEC evidence must not acquire transcripts"
        assert acquired["tables"] == {}
        assert "no Alpha Vantage evidence held" in acquired["absent"]
        assert sec_double.calls == sec_calls, "held filing metadata should be read offline"


def test_held_vrt_filing_history_is_served_without_transcript_acquisition():
    with _TempPlane():
        transcripts = _Transcripts()
        with _patched(_Sec(shape=VRT_SHAPE), _Yahoo(), _Estimates(),
                      transcripts=transcripts):
            held = pull("VRT", sources=["sec"], eight_ks=0)
            result = pull("VRT", sources=["alpha_vantage"])
        assert held["status"] == "RETRIEVED" and result["status"] == "CACHED"
        assert transcripts.quarters == [], "a held ticker serves instead of acquiring transcripts"
        assert result["tables"] == {} and "no transcripts held" in result["absent"]


def test_short_sec_history_is_not_padded_and_is_reported():
    shape = {"periods": {"10-K": ["2025-12-31", "2024-12-31"]},
             "revenue_label": "Net sales", "equity_concept": "us-gaap_StockholdersEquity"}
    with _TempPlane():
        transcripts = _Transcripts()
        with _patched(_Sec(shape=shape), _Yahoo(), _Estimates(), transcripts=transcripts):
            result = pull("VRT", sources=["sec", "alpha_vantage"], eight_ks=0)
        assert result["status"] == "RETRIEVED", result
        assert transcripts.quarters == ["2024Q4", "2025Q4"], transcripts.quarters
        assert result["reported_quarters_note"] == (
            "2 reported quarter(s) available; scope was not padded"), result


def test_held_evidence_serves_transcript_union_without_provider_calls():
    quarters = ["2024Q2", "2024Q3", "2024Q4"] + [
        f"2025Q{i}" for i in range(1, 5)] + ["2026Q1", "2026Q2", "2026Q3"]
    newest = ["2025Q4", "2026Q1", "2026Q2", "2026Q3"]
    with _TempPlane():
        providers = (_Sec(), _Yahoo(), _Estimates(), _EightK(), _Transcripts())
        newer_run = False
        original_transcript = providers[4].__call__

        def transcript_with_newer_collision(symbol, quarter, issuer=None, ceiling=None):
            data, meta = original_transcript(symbol, quarter, issuer, ceiling)
            if newer_run and quarter == "2025Q4":
                data["segments"][0]["content"] = "Newer snapshot wins this shared quarter."
            return data, meta

        with mock.patch.object(store, "now_iso", _ageing_clock()):
            with _patched(*providers[:3], providers[3], transcripts=transcript_with_newer_collision):
                wide = pull("AVGO", sources=["alpha_vantage"], transcripts=quarters)
                newer_run = True
                # Refresh the overlapping provider evidence so the fixture can make
                # newest-wins collisions observable instead of reusing held segments.
                with mock.patch.object(sys.modules["financial_data_pull.pull"],
                                        "held_transcript_quarters", return_value={}):
                    narrow = pull("AVGO", sources=["alpha_vantage"], transcripts=newest,
                                  refresh=True)
                snapshots = store.snapshot_dirs("AVGO")
                assert len(snapshots) == 2, snapshots
                older_id, newer_id = (path.name for path in snapshots)
                assert older_id == Path(wide["snapshot_dir"]).name
                assert newer_id == Path(narrow["snapshot_dir"]).name
                older_table = read_table("AVGO", "av_transcript", run_id=older_id)
                newer_table = read_table("AVGO", "av_transcript", run_id=newer_id)
                assert set(older_table["quarter"]) == set(quarters)
                assert len(older_table["quarter"].unique()) == 10
                assert set(newer_table["quarter"]) == set(newest)
                assert len(newer_table["quarter"].unique()) == 4
                calls = tuple(provider.calls for provider in (providers[0], providers[1], providers[2],
                                                               providers[3], providers[4]))
                served = pull("AVGO", sources=["alpha_vantage"])
        assert served["status"] == "CACHED", served
        assert served["tables"]["av_transcript"]["quarter"].nunique() == 10
        assert set(served["tables"]["av_transcript"]["quarter"]) == set(quarters)
        assert tuple(provider.calls for provider in (providers[0], providers[1], providers[2],
                                                     providers[3], providers[4])) == calls
        assert served["provenance"]["av_transcript"]["snapshot"] == newer_id
        served_collision = served["tables"]["av_transcript"].set_index("quarter").loc["2025Q4"]
        newer_collision = newer_table.set_index("quarter").loc["2025Q4"]
        older_collision = older_table.set_index("quarter").loc["2025Q4"]
        assert served_collision["content"] == newer_collision["content"]
        assert served_collision["content"] != older_collision["content"]
        latest_manifest = manifest("AVGO", run_id=
                                   served["provenance"]["av_transcript"]["snapshot"])
        assert served["provenance"]["av_transcript"]["sha256"] == \
            latest_manifest["table_hashes"]["av_transcript"]
        assert served["absent"] == []
        assert {"status", "issuer", "tables", "provenance", "absent", "snapshot_dirs",
                "report", "note"} <= served.keys()
    print("  held transcript union serves offline with provenance ✓")


def test_held_evidence_filters_sources_transcripts_and_releases():
    with _TempPlane():
        with _patched(_Sec(), _Yahoo(), _Estimates(), _EightK(), transcripts=_Transcripts()):
            pull("NVDA", sources=["sec"], eight_ks=2)
            pull("NVDA", sources=["alpha_vantage"], transcripts=["2026Q1", "2026Q2"],
                 refresh=True)
            result = pull("NVDA", sources=["yahoo"])
            assert result["status"] == "CACHED", result
            assert all(name.startswith("yahoo_") for name in result["tables"])
            quarter = pull("NVDA", sources=["alpha_vantage"], transcripts=["2026Q2"],
                           eight_ks=0)
            assert set(quarter["tables"]["av_transcript"]["quarter"]) == {"2026Q2"}
            filtered = pull("NVDA", sources=["sec", "alpha_vantage"], transcripts=[],
                            eight_ks=1)
        assert filtered["status"] == "CACHED", filtered
        assert "sec_8k" in filtered["tables"]
        assert len(filtered["tables"]["sec_8k"]) == 1
        assert "av_transcript" not in filtered["tables"]
    print("  held evidence filters families and release depth ✓")


def test_a_restricted_default_pull_stays_transcript_free_and_does_not_raise():
    with _TempPlane():
        with _patched(_Sec(), _Yahoo(), _Estimates()):
            result = pull("NVDA", sources=["sec"], eight_ks=0)
        assert result["status"] == "RETRIEVED", result
        assert not any(str(r["dataset"]).startswith("av_transcript_")
                       for r in _coverage(result)["rows"])
        assert "av_transcript" not in result["table_hashes"]
        assert result["requests"] == {"sec": 2}, result["requests"]
        print("  a restricted default pull is transcript-free and does not raise ✓")


def test_a_repeat_pull_is_cached_with_zero_provider_calls():
    with _TempPlane():
        s, y, a = _Sec(), _Yahoo(), _Estimates()
        t, e = _Transcripts(), _EightK()
        with _patched(s, y, a, e, transcripts=t):
            first = pull("NVDA")
            asked = (s.calls, y.calls, a.calls, t.calls)
            second = pull("NVDA")
        assert first["status"] == "RETRIEVED", first
        assert first["requests"] == {"sec": 4, "yahoo": 8, "alpha_vantage": 1,
                                     "alpha_vantage_transcripts": 1}, first["requests"]
        assert e.counts == [12] and first["eight_ks"] == 12
        assert second["status"] == "CACHED", second
        assert (s.calls, y.calls, a.calls, t.calls) == asked, \
            "a cached pull must ask no provider"
        assert len(store.snapshot_dirs("NVDA")) == 1
        assert len(list((store.COVERAGE / "NVDA").glob("*.json"))) == 1
        cached_only = pull("NVDA", cache_only=True)
        assert cached_only["status"] == "CACHED" and cached_only["cache_only"] is True
        assert cached_only["snapshot"]["run_id"] == Path(first["snapshot_dir"]).name
        print("  a repeat pull is CACHED with zero provider calls ✓")


def test_held_ticker_filters_to_requested_source_family():
    with _TempPlane():
        s, y, a = _Sec(), _Yahoo(), _Estimates()
        with _patched(s, y, a):
            yahoo_only = pull("NVDA", sources=["yahoo"])
            sec_only = pull("NVDA", sources=["sec"], eight_ks=0)
            again = pull("NVDA", sources=["yahoo"])
            default = pull("NVDA")
        assert len(store.snapshot_dirs("NVDA")) == 1, "a held ticker serves its union"
        assert set(yahoo_only["table_hashes"]) == set(yahoo.DATASETS), yahoo_only["table_hashes"]
        assert all(k.startswith(("income_", "balance_", "cashflow_"))
                   for k in sec_only["tables"]), sec_only["tables"]
        assert set(yahoo_only["statuses"]) == set(yahoo.DATASETS), yahoo_only["statuses"]
        # the source filter returns no network-backed missing family on this held ticker
        assert "no SEC evidence held" in sec_only["absent"]
        assert sec_only["status"] == "CACHED" and again["status"] == "CACHED"
        assert default["status"] == "CACHED" and "no SEC evidence held" in default["absent"]
        assert y.calls == 1 and s.calls == 0
        assert yahoo_only["scope_key"] != sec_only["scope_key"]
        print("  held source-set filters serve only requested evidence ✓")


def test_a_failing_provider_becomes_coverage_rows_not_an_exception():
    with _TempPlane():
        s, y, a = _Sec(), _Yahoo(fail=True), _Estimates()
        with _patched(s, y, a):
            result = pull("NVDA")
        assert result["status"] == "RETRIEVED", result
        for name in yahoo.DATASETS:
            assert result["statuses"][name] == "FAILED", (name, result["statuses"])
        assert result["statuses"]["av_earnings_estimates"] == "RETRIEVED"
        assert result["statuses"]["income_annual_0"] == "RETRIEVED"
        doc = _coverage(result)
        assert contracts.validate_against(doc, "coverage") == []
        reasons = {row["dataset"]: row.get("reason") for row in doc["rows"]}
        assert reasons["yahoo_prices"] == "NOT_RETRIEVABLE", reasons["yahoo_prices"]
        assert reasons["av_earnings_estimates"] is None
        assert all(row.get("reason") for row in doc["rows"]
                   if row["acquisition"] != "RETRIEVED"), doc["rows"]
        print("  a provider failure is FAILED rows with a reason, not an exception ✓")


class _UnreadableFiling:
    """One filing whose income statement exists but raises when converted to a frame."""

    form = "10-K"
    filing_date = "2025-03-01"
    period_of_report = "2025-01-31"
    accession_no = "0000000000-25-000001"

    class _Statement:
        def to_dataframe(self):
            raise ValueError("XBRL frame is missing a context")

    income_statement = _Statement()
    balance_sheet = None
    cash_flow_statement = None

    def obj(self):
        return self

    def full_text_submission(self):
        return "<filing/>"


def test_a_statement_that_fails_to_convert_is_parse_failed_not_absent():
    """Exercises the real sec provider with edgartools stubbed out: a statement we
    cannot read is our failure, not evidence that the filing never carried it."""
    company = mock.MagicMock()
    company.get_filings.return_value.__getitem__.return_value = _UnreadableFiling()
    with _TempPlane():
        with mock.patch("edgar.Company", return_value=company), \
                mock.patch.object(sec, "_identity", return_value="me"):
            result = pull("NVDA", sources=["sec"])
        doc = _coverage(result)
        assert contracts.validate_against(doc, "coverage") == []
        rows = {r["dataset"]: r for r in doc["rows"]}
        assert rows["income_annual_0"]["acquisition"] == "PARSE_FAILED", rows["income_annual_0"]
        assert "missing a context" in rows["income_annual_0"]["label"], rows["income_annual_0"]
        assert rows["balance_annual_0"]["acquisition"] == "MISSING", rows["balance_annual_0"]
        print("  an unreadable statement is PARSE_FAILED, an absent one is MISSING ✓")


def test_a_yahoo_dataset_failure_keeps_the_error_text():
    """yfinance changing under us has to be legible in the coverage row."""

    class _BrokenTicker:
        def __getattr__(self, name):
            def boom(*args, **kwargs):
                raise RuntimeError("yfinance changed its column names")
            return boom

    with _TempPlane():
        with mock.patch.dict(sys.modules, {"yfinance": mock.MagicMock(
                Ticker=lambda ticker: _BrokenTicker())}):
            result = pull("NVDA", sources=["yahoo"])
        assert result["status"] == "FAILED", result
        row = {r["dataset"]: r for r in _coverage(result)["rows"]}["yahoo_prices"]
        assert row["acquisition"] == "FAILED", row
        assert "yfinance changed its column names" in row["label"], row
        print("  a failing Yahoo dataset names its error in the coverage row ✓")


def test_a_foreign_private_issuer_still_gets_a_row_per_statement():
    """The 20-F branch must speak for every statement kind too: a statement the filing
    lacks is MISSING and one that will not convert is PARSE_FAILED, never a gap."""

    class _Frame:
        def __init__(self, frame):
            self._frame = frame

        def to_dataframe(self):
            return self._frame

    class _UnreadableFrame:
        def to_dataframe(self):
            raise ValueError("XBRL frame is missing a context")

    class _TwentyF:
        form = "20-F"
        filing_date = "2025-03-01"
        period_of_report = "2024-12-31"
        accession_no = "0000000000-25-000002"
        income_statement = _Frame(pd.DataFrame({"concept": ["us-gaap_Revenues"],
                                                "2024-12-31 (FY)": [1.0]}))
        balance_sheet = _UnreadableFrame()
        cash_flow_statement = None

        def obj(self):
            return self

        def full_text_submission(self):
            return "<filing/>"

        def __getitem__(self, index):
            if index:  # one 20-F in the history
                raise IndexError("no more 20-F filings")
            return self

    class _NoTenK:
        def __getitem__(self, index):
            raise IndexError("no 10-K filings")

    company = mock.MagicMock()
    company.get_filings.side_effect = (
        lambda form=None, **kwargs: _TwentyF() if form == "20-F" else _NoTenK())
    with _TempPlane():
        with mock.patch("edgar.Company", return_value=company), \
                mock.patch.object(sec, "_identity", return_value="me"):
            result = pull("NVDA", sources=["sec"])
        doc = _coverage(result)
        assert contracts.validate_against(doc, "coverage") == []
        rows = {r["dataset"]: r for r in doc["rows"]}
        assert rows["income_annual_0"]["acquisition"] == "RETRIEVED", rows["income_annual_0"]
        assert rows["income_annual_0"]["label"] == ("20-F annual; interims are documents"), \
            rows["income_annual_0"]
        assert rows["balance_annual_0"]["acquisition"] == "PARSE_FAILED", rows["balance_annual_0"]
        assert "missing a context" in rows["balance_annual_0"]["label"], rows["balance_annual_0"]
        assert rows["cashflow_annual_0"]["acquisition"] == "MISSING", rows.get("cashflow_annual_0")
        print("  a 20-F filing reports every statement: retrieved, unreadable or MISSING ✓")


def test_an_alpha_vantage_message_is_redacted_before_it_reaches_an_artifact():
    """A provider's own message is external input: neither a key nor a full URL from
    it may be stored, in the row's label or in the manifest."""
    message = ("This endpoint is on premium plans: https://www.alphavantage.co/premium/ "
               "(apikey=SECRET123).")
    with _TempPlane():
        with mock.patch.object(alphavantage, "config") as cfg:
            cfg.get.return_value = "key"
            # no `_patched` here: both alpha_vantage calls must be the real provider
            # code, reading a patched response, or the redaction is never exercised
            with mock.patch.object(alphavantage.urllib.request, "urlopen",
                                   _http({"Information": message})):
                estimates = pull("NVDA", sources=["alpha_vantage"], transcripts=[])
                transcripts = pull("NVDA", sources=["alpha_vantage"],
                                   transcripts=["2025Q1"])
        for result, dataset in ((estimates, "av_earnings_estimates"),
                                (transcripts, "av_transcript_2025Q1")):
            label = next(r for r in _coverage(result)["rows"]
                         if r["dataset"] == dataset)["label"]
            assert "SECRET123" not in label and "alphavantage.co" not in label, label
            assert "<url>" in label, label
        print("  an Alpha Vantage message is redacted before it reaches an artifact ✓")


def test_a_bogus_ticker_records_gaps_and_publishes_no_snapshot():
    with _TempPlane():
        s, y, a = _Sec(fail=True), _Yahoo(fail=True), _Estimates(fail=True)
        with _patched(s, y, a, _EightK(fail=True), transcripts=_Transcripts(fail=True)):
            result = pull("ZZZZ")
        assert result["status"] == "FAILED", result
        doc = _coverage(result)
        assert contracts.validate_against(doc, "coverage") == []
        assert doc["rows"], "a failed run still records what it could not acquire"
        for row in doc["rows"]:
            assert row["acquisition"] in ("FAILED", "MISSING"), row
            assert row.get("reason"), row
        assert result["table_hashes"] == {}
        assert result["open_gaps"], result
        assert "snapshot_id" not in doc, "coverage must not name a snapshot that was never published"
        assert Path(result["coverage_path"]).is_file()
        assert store.snapshot_dirs("ZZZZ") == [], \
            "a run holding no evidence must not publish a snapshot a later pull can hit"
        print("  a bogus ticker records valid coverage and publishes no snapshot ✓")


def test_a_failed_run_is_not_cached_and_the_scope_recovers():
    """The cache has no TTL: a run that retrieved nothing must not read back CACHED."""
    with _TempPlane():
        s, y, a = _Sec(fail=True), _Yahoo(fail=True), _Estimates(fail=True)
        with _patched(s, y, a, _EightK(fail=True), transcripts=_Transcripts(fail=True)):
            failed = pull("NVDA")
            asked = (s.calls, y.calls, a.calls)
            again = pull("NVDA")
        assert failed["status"] == "FAILED", failed
        assert again["status"] == "FAILED", again
        assert all(now > before for now, before in
                   zip((s.calls, y.calls, a.calls), asked)), \
            "a failed run must be re-attempted, not answered from the cache"
        # once the providers answer, the same plain pull acquires without a refresh
        with _patched(_Sec(), _Yahoo(), _Estimates()):
            recovered = pull("NVDA")
        assert recovered["status"] == "RETRIEVED", recovered
        assert recovered["table_hashes"], recovered
        print("  a fully failed run is never a cache hit ✓")


def test_one_issuer_two_tickers_do_not_share_a_snapshot():
    with _TempPlane():
        s, y, a = _Sec(), _Yahoo(), _Estimates()
        with _patched(s, y, a):
            adr = pull("NSRGY", issuer="NESTLE", sources=["yahoo"])
            line = pull("NESN.SW", issuer="NESTLE", sources=["yahoo"])
        assert adr["status"] == "RETRIEVED" and line["status"] == "RETRIEVED", (adr, line)
        assert adr["scope_key"] != line["scope_key"], "the ticker belongs in the scope"
        assert line["snapshot_dir"] != adr["snapshot_dir"]
        assert y.calls == 2, "a second listing is its own acquisition, not a cache hit"
        assert manifest("NESTLE", run_id=Path(adr["snapshot_dir"]).name)["ticker"] == "NSRGY"
        print("  one issuer's two tickers hold their own snapshots ✓")


def test_serving_one_listing_does_not_mix_the_other_listings_tables():
    with _TempPlane():
        y = _Yahoo()
        original_fetch = y.__call__

        def ticker_frames(ticker, issuer=None, ceiling=None):
            frames, meta = original_fetch(ticker, issuer, ceiling)
            frames["yahoo_prices"].loc[0, "Close"] = 11 if ticker == "NSRGY" else 22
            return frames, meta

        with _patched(_Sec(), ticker_frames, _Estimates()):
            adr = pull("NSRGY", issuer="NESTLE", sources=["yahoo"])
            pull("NESN.SW", issuer="NESTLE", sources=["yahoo"])
            calls = y.calls
            served = pull("NSRGY", issuer="NESTLE", sources=["yahoo"])
        assert served["status"] == "CACHED" and y.calls == calls == 2
        assert served["tables"]["yahoo_prices"].loc[0, "Close"] == 11
        assert served["provenance"]["yahoo_prices"]["snapshot"] == \
            Path(adr["snapshot_dir"]).name
        print("  serving one listing excludes the other listing's tables ✓")


def test_refresh_adds_a_version_and_leaves_the_first_untouched():
    with _TempPlane():
        s, y, a = _Sec(), _Yahoo(), _Estimates()
        with _patched(s, y, a):
            first = pull("NVDA", sources=["yahoo"])
            before = _tree(Path(first["snapshot_dir"]))
            refreshed = pull("NVDA", sources=["yahoo"], refresh=True)
            after = _tree(Path(first["snapshot_dir"]))
        assert refreshed["status"] == "RETRIEVED", refreshed
        assert refreshed["snapshot_dir"] != first["snapshot_dir"]
        assert refreshed["scope_key"] == first["scope_key"], "a refresh is the same scope"
        assert len(store.snapshot_dirs("NVDA")) == 2
        assert before == after, "a refresh must leave the published version byte-identical"
        assert {d.name for d in store.snapshot_dirs("NVDA")} == {
            Path(first["snapshot_dir"]).name, Path(refreshed["snapshot_dir"]).name}
        # both versions stay readable by their own run id
        first_run = Path(first["snapshot_dir"]).name
        assert manifest("NVDA", run_id=first_run)["run_id"] == first_run
        print("  refresh adds a version and leaves the first byte-identical ✓")


def test_a_transcript_quarter_reports_the_providers_own_reason():
    with _TempPlane():
        with mock.patch.object(alphavantage, "config") as cfg:
            cfg.get.return_value = "key"
            with mock.patch.object(alphavantage.urllib.request, "urlopen",
                                   _http({"Information": "premium endpoint"})):
                with _patched(_Sec(), _Yahoo(), _Estimates(),
                              transcripts=_real_transcript):
                    result = pull("NVDA", sources=["alpha_vantage"],
                                       transcripts=["2025Q1"])
        doc = _coverage(result)
        assert contracts.validate_against(doc, "coverage") == []
        row = next(r for r in doc["rows"] if r["dataset"] == "av_transcript_2025Q1")
        assert row["acquisition"] == "MISSING", row
        assert row["reason"] == "PREMIUM_OR_UNCOVERED", row
        assert "av_transcript" not in result["table_hashes"]
        print("  an uncovered quarter is MISSING with the provider's own reason ✓")


def test_an_empty_transcript_is_missing_not_a_silent_success():
    with _TempPlane():
        with mock.patch.object(alphavantage, "config") as cfg:
            cfg.get.return_value = "key"
            with mock.patch.object(alphavantage.urllib.request, "urlopen",
                                   _http({"symbol": "NVDA", "quarter": "2025Q1",
                                          "transcript": []})):
                with _patched(_Sec(), _Yahoo(), _Estimates(),
                              transcripts=_real_transcript):
                    result = pull("NVDA", sources=["alpha_vantage"],
                                       transcripts=["2025Q1"])
        row = next(r for r in _coverage(result)["rows"]
                   if r["dataset"] == "av_transcript_2025Q1")
        assert row["acquisition"] == "MISSING", row
        assert row["reason"] == "NOT_PUBLISHED", row
        assert "av_transcript" not in result["table_hashes"]
        print("  an empty transcript is MISSING, never a silent success ✓")


def test_an_empty_source_set_is_refused_not_read_as_every_source():
    try:
        pull("NVDA", sources=[])
    except ValueError as exc:
        assert "at least one" in str(exc), exc
    else:
        raise AssertionError("an explicitly empty source set must not acquire everything")
    print("  an empty source set is refused rather than read as every source ✓")


def test_a_repeated_quarter_is_asked_for_once():
    with _TempPlane():
        with mock.patch.object(alphavantage, "config") as cfg:
            cfg.get.return_value = "key"
            with mock.patch.object(alphavantage.urllib.request, "urlopen",
                                   _http({"symbol": "NVDA", "quarter": "2025Q1",
                                          "transcript": [{"speaker": "Operator",
                                                          "title": "Operator",
                                                          "content": "Welcome."}]})) as provider:
                with _patched(_Sec(), _Yahoo(), _Estimates(),
                              transcripts=_real_transcript):
                    result = pull("NVDA", sources=["alpha_vantage"],
                                  transcripts=["2025Q1", "2025Q1"])
        assert provider.call_count == 1, "a repeated quarter must not spend a second request"
        rows = [r for r in _coverage(result)["rows"] if r["dataset"] == "av_transcript_2025Q1"]
        assert len(rows) == 1, rows
        print("  a repeated quarter is acquired once and recorded once ✓")


def test_a_cached_quarter_is_reused_and_the_rest_is_acquired():
    segment = {"speaker": "Operator", "title": "Operator", "content": "Welcome."}
    with _TempPlane():
        with mock.patch.object(alphavantage, "config") as cfg:
            cfg.get.return_value = "key"
            with mock.patch.object(alphavantage.urllib.request, "urlopen",
                                   _http({"symbol": "NVDA", "quarter": "2025Q1",
                                          "transcript": [segment]})):
                with _patched(_Sec(), _Yahoo(), _Estimates(),
                              transcripts=_real_transcript):
                    first = pull("NVDA", sources=["alpha_vantage"],
                                      transcripts=["2025Q1"])
            assert "av_transcript" in first["table_hashes"], first["table_hashes"]
            # a widened scope reuses the held quarter and asks only for the new one
            with mock.patch.object(alphavantage.urllib.request, "urlopen",
                                   _http({"symbol": "NVDA", "quarter": "2025Q2",
                                          "transcript": [segment]})) as provider:
                with _patched(_Sec(), _Yahoo(), _Estimates(),
                              transcripts=_real_transcript):
                    second = pull("NVDA", sources=["alpha_vantage"],
                                       transcripts=["2025Q1", "2025Q2"], refresh=True)
        assert second["status"] == "RETRIEVED", second
        assert provider.call_count == 1, "a held quarter must not be re-pulled"
        rows = {r["dataset"]: r for r in _coverage(second)["rows"]}
        assert rows["av_transcript_2025Q1"]["acquisition"] == "RETRIEVED", rows
        assert rows["av_transcript_2025Q2"]["acquisition"] == "RETRIEVED", rows
        # the held quarter's segments are carried into the new snapshot too
        frame = read_table("NVDA", "av_transcript",
                                run_id=Path(second["snapshot_dir"]).name)
        assert sorted(frame["quarter"].unique()) == ["2025Q1", "2025Q2"], frame
        # …and so is its preserved original, or the new snapshot would cite a payload
        # it cannot reach. Read the pinned snapshot's own manifest: `manifest("NVDA")`
        # resolves to the newest run, not the one this assertion is about.
        first_manifest = json.loads((Path(first["snapshot_dir"]) / "snapshot.json").read_text())
        second_manifest = json.loads((Path(second["snapshot_dir"]) / "snapshot.json").read_text())
        carried = second_manifest["originals"]["av_transcript_2025Q1"]
        assert carried == first_manifest["originals"]["av_transcript_2025Q1"], carried
        assert Path(carried).is_file(), carried
        print("  a held quarter is reused with zero calls and carried into the new snapshot ✓")


def test_the_cli_exits_non_zero_when_nothing_was_published():
    with _TempPlane():
        err = io.StringIO()
        with _patched(_Sec(fail=True), _Yahoo(fail=True), _Estimates(fail=True),
                      _EightK(fail=True), transcripts=_Transcripts(fail=True)), \
                mock.patch("sys.stdout", new_callable=io.StringIO), mock.patch("sys.stderr", err):
            assert main(["ZZZZ"]) == 1, "a run that acquired nothing must not exit 0"
        assert "FAILED — nothing published" in err.getvalue().splitlines(), err.getvalue()
        with _patched(_Sec(), _Yahoo(), _Estimates()), \
                mock.patch("sys.stdout", new_callable=io.StringIO), \
                mock.patch("sys.stderr", new_callable=io.StringIO):
            assert main(["NVDA"]) == 0, "a published snapshot exits 0"
    print("  the CLI exits non-zero when nothing was published ✓")


def test_the_cli_prints_the_run_status_to_stderr():
    """One human line says cached or new; stdout keeps carrying only the JSON."""
    with _TempPlane():
        out, err = io.StringIO(), io.StringIO()
        with _patched(_Sec(), _Yahoo(), _Estimates(), transcripts=_Transcripts()), \
                mock.patch("sys.stdout", out), mock.patch("sys.stderr", err):
            assert main(["NVDA"]) == 0, "the first run publishes"
        result = json.loads(out.getvalue())
        run_id = Path(result["snapshot_dir"]).name
        spent = sum(result["requests"].values())
        assert f"NEW SNAPSHOT {run_id} — {spent} requests spent" in err.getvalue().splitlines(), \
            err.getvalue()
        # the status line is a stderr aside: stdout still parses as the same JSON
        assert result["status"] == "RETRIEVED"

        out, err = io.StringIO(), io.StringIO()
        with _patched(_Sec(fail=True), _Yahoo(fail=True), _Estimates(fail=True),
                      _EightK(fail=True), transcripts=_Transcripts(fail=True)), \
                mock.patch("sys.stdout", out), mock.patch("sys.stderr", err):
            assert main(["NVDA"]) == 0, "the held scope is a cache hit"
        assert f"CACHED — evidence already held, zero network (snapshot {run_id})" \
            in err.getvalue().splitlines(), err.getvalue()
        assert json.loads(out.getvalue())["status"] == "CACHED"

        # a cache-only miss names what is held — nothing — rather than an empty answer
        out, err = io.StringIO(), io.StringIO()
        with mock.patch("sys.stdout", out), mock.patch("sys.stderr", err):
            assert main(["ZZZZ", "--cache-only"]) == 0
        assert "no data held for ZZZZ (cache-only)" in err.getvalue().splitlines(), err.getvalue()
        assert json.loads(out.getvalue())["status"] == "MISSING: NOT_RETRIEVED"

        # --quiet silences the status line and the INFO progress lines, and leaves
        # stdout alone; the non-quiet run still logs its progress
        out, err = io.StringIO(), io.StringIO()
        with _patched(_Sec(fail=True), _Yahoo(fail=True), _Estimates(fail=True),
                      _EightK(fail=True), transcripts=_Transcripts(fail=True)), \
                _LogCapture(pull_logger) as log, \
                mock.patch("sys.stdout", out), mock.patch("sys.stderr", err):
            assert main(["NVDA", "--quiet"]) == 0
        assert err.getvalue() == "", err.getvalue()
        assert json.loads(out.getvalue())["status"] == "CACHED"
        assert log.messages(logging.INFO) == [], log.messages(logging.INFO)
        out, err = io.StringIO(), io.StringIO()
        with _patched(_Sec(fail=True), _Yahoo(fail=True), _Estimates(fail=True),
                      _EightK(fail=True), transcripts=_Transcripts(fail=True)), \
                _LogCapture(pull_logger) as log, \
                mock.patch("sys.stdout", out), mock.patch("sys.stderr", err):
            assert main(["NVDA"]) == 0
        assert log.messages(logging.INFO), "the non-quiet run logs its progress lines"
    print("  the CLI prints one cached/new status line to stderr ✓")


def test_a_transcript_label_that_is_not_a_quarter_is_refused_at_the_boundary():
    """A label the provider cannot name must fail before a request, not MISSING later."""
    with _TempPlane():
        s, y, a, t = _Sec(), _Yahoo(), _Estimates(), _Transcripts()
        try:
            with _patched(s, y, a, transcripts=t):
                pull("NVDA", transcripts=["none"])
        except ValueError as exc:
            assert "none" in str(exc), exc
        else:
            raise AssertionError("a bogus quarter label must be refused")
        assert (s.calls, y.calls, a.calls, t.calls) == (0, 0, 0, 0), \
            "the refusal must precede any provider call"
        assert all(QUARTER_LABEL.fullmatch(q) for q in reported_quarters({
            "filing": {"period_of_report": "2026-08-02"}}))
    print("  a transcript label that is not a quarter is refused before any request ✓")


def test_the_cli_refuses_a_sentinel_mixed_with_quarter_labels():
    """`--transcripts none,2025Q1` must not spend a request asking for quarter `none`."""
    with _TempPlane():
        out, err = io.StringIO(), io.StringIO()
        s, y, a, t = _Sec(), _Yahoo(), _Estimates(), _Transcripts()
        with _patched(s, y, a, transcripts=t), \
                mock.patch("sys.stdout", out), mock.patch("sys.stderr", err):
            assert main(["NVDA", "--transcripts", "none,2025Q1"]) == 1
        assert "YYYYQN" in err.getvalue(), err.getvalue()
        assert (s.calls, y.calls, a.calls, t.calls) == (0, 0, 0, 0), \
            "the refusal must precede any provider call"
    print("  the CLI refuses a sentinel mixed with quarter labels ✓")


def test_export_reads_only_the_latest_snapshot():
    with _TempPlane():
        s, y, a = _Sec(), _Yahoo(), _Estimates()
        with mock.patch.object(store, "now_iso", _ageing_clock()), _patched(s, y, a):
            everything = pull("NVDA")
            export_csv("NVDA")                                # a complete set first
            later = pull("NVDA", sources=["alpha_vantage"], refresh=True)  # a thinner latest version
        exported = export_csv("NVDA")
        latest = Path(later["snapshot_dir"]).name
        assert set(exported.values()) == {latest}, exported
        assert set(exported) == set(later["table_hashes"]), exported
        dropped = set(everything["table_hashes"]) - set(later["table_hashes"])
        assert dropped, "the fixture must leave the later snapshot thinner"
        for table in sorted(dropped):
            stale = store.CSV / "NVDA" / f"{table}.csv"
            assert not stale.exists(), f"{stale} is from an older run: it must not linger"
        frame = read_table("NVDA", "av_earnings_estimates")
        csv_path = store.CSV / "NVDA" / "av_earnings_estimates.csv"
        assert csv_path.read_text() == frame.to_csv(index=False), csv_path
        print("  export reads the latest snapshot only, and prunes what it dropped ✓")


def test_export_keeps_a_named_index_as_a_column():
    """yahoo_prices holds its dates in the index; index=False alone exports them undated."""
    with _TempPlane():
        y = _Yahoo()
        plain = y.__call__

        def dated(ticker, issuer=None, ceiling=None):
            frames, meta = plain(ticker, issuer, ceiling)
            index = pd.Index(["2026-09-21", "2026-09-22"], name="Date")
            return {name: frame.set_index(index) for name, frame in frames.items()}, meta

        with mock.patch.object(store, "now_iso", _ageing_clock()), \
                _patched(_Sec(), dated, _Estimates()):
            pull("NVDA")
        export_csv("NVDA")
        text = (store.CSV / "NVDA" / "yahoo_prices.csv").read_text()
        assert text.splitlines()[0].startswith("Date,"), text.splitlines()[0]
        assert "2026-09-22" in text, text
        print("  a named index survives the export as its own column ✓")


def test_publishing_a_thinner_snapshot_warns():
    with _TempPlane():
        with mock.patch.object(store, "now_iso", _ageing_clock()), \
                _patched(_Sec(), _Yahoo(), _Estimates()):
            pull("NVDA")
            with mock.patch.object(pull_logger, "warning") as warn:
                pull("NVDA", sources=["alpha_vantage"], refresh=True)
        said = " ".join(str(call.args[0] % call.args[1:]) for call in warn.call_args_list)
        assert "thinner" in said, said
        assert "income_annual_0" in said or "yahoo_prices" in said, said
        print("  a snapshot thinner than the previous latest warns at publish time ✓")


def test_the_cli_export_prints_provenance_and_makes_no_network_call():
    with _TempPlane():
        e = _EightK()
        with _patched(_Sec(), _Yahoo(), _Estimates(), e), \
                mock.patch("sys.stdout", new_callable=io.StringIO), \
                mock.patch("sys.stderr", new_callable=io.StringIO):
            assert main(["NVDA", "--earnings-8k", "2"]) == 0, "the acquisition publishes"
        # every provider now fails if touched: the export must ask none of them
        s, y, a = _Sec(fail=True), _Yahoo(fail=True), _Estimates(fail=True)
        out = io.StringIO()
        with _patched(s, y, a, _EightK(fail=True)), mock.patch("sys.stdout", out):
            assert main(["NVDA", "--export-csv"]) == 0, "an export is not a failed pull"
        lines = [ln for ln in out.getvalue().splitlines() if " ← " in ln]
        assert lines, out.getvalue()
        for line in lines:
            table, _, snapshot_id = line.partition(" ← ")
            snapshot = store.TABLES / "NVDA" / snapshot_id
            manifest_doc = json.loads((snapshot / "snapshot.json").read_text())
            assert table in manifest_doc["table_hashes"], line
        assert (s.calls, y.calls, a.calls) == (0, 0, 0), "the export must touch no provider"
        assert (store.CSV / "NVDA" / "sec_8k.csv").is_file()
        assert e.calls == 1 and e.counts == [2], (e.calls, e.counts)
    print("  the CLI export prints provenance per table with zero network ✓")


def test_eight_k_filings_are_archived_per_filing_and_scoped_by_depth():
    filings = [{"accession": "0000000000-25-000010", "filing_date": "2025-02-06",
                "items": "2.02,9.01"},
               {"accession": "0000000000-25-000009", "filing_date": "2025-01-08",
                "items": ""},
               {"accession": "0000000000-24-000011", "filing_date": "2024-10-30",
                "items": "2.02,9.01"}]
    with _TempPlane():
        with mock.patch.object(store, "now_iso", _ageing_clock()), \
                _patched(_Sec(), _Yahoo(), _Estimates(), _EightK(filings)):
            deep = pull("NVDA", eight_ks=3)
            early = export_csv("NVDA")                      # the deep run is the latest here
            shallow = pull("NVDA", sources=["sec"], eight_ks=1)
            again = pull("NVDA", sources=["sec"], eight_ks=1)
            without = pull("NVDA", sources=["sec"], eight_ks=0)
        assert deep["status"] == "RETRIEVED" and deep["eight_ks"] == 3, deep
        frame = read_table("NVDA", "sec_8k", run_id=Path(deep["snapshot_dir"]).name)
        assert list(frame["accession"]) == ["0000000000-25-000010",
                                            "0000000000-24-000011"], frame
        for path in frame["exhibit_path"]:
            assert path.endswith(".htm") and Path(path).is_file(), path
            assert Path(path).is_relative_to(store.RAW / "NVDA" / "sec"), path
        doc = _coverage(deep)
        assert contracts.validate_against(doc, "coverage") == []
        rows = {r["dataset"]: r for r in doc["rows"]}
        assert rows["sec_8k_0000000000-25-000010"]["acquisition"] == "RETRIEVED"
        unclassifiable = rows["sec_8k_0000000000-25-000009"]
        assert unclassifiable["acquisition"] == "MISSING", unclassifiable
        assert unclassifiable["reason"] == "NOT_PUBLISHED", unclassifiable
        assert "no items" in unclassifiable["label"], unclassifiable
        held = manifest("NVDA", run_id=Path(deep["snapshot_dir"]).name)
        assert held["eight_ks"] == 3, held
        assert Path(held["originals"]["sec_8k_0000000000-25-000010"]).is_file()
        # Depth and source filters now shape a serve; neither causes a new acquisition.
        assert len({deep["scope_key"], shallow["scope_key"], without["scope_key"]}) == 3
        assert shallow["status"] == again["status"] == without["status"] == "CACHED"
        assert len(shallow["tables"]["sec_8k"]) == 1
        assert "sec_8k" not in without["tables"]
        assert early["sec_8k"] == Path(deep["snapshot_dir"]).name, early
        assert (store.CSV / "NVDA" / "sec_8k.csv").is_file()
        assert "sec_8k" in export_csv("NVDA"), "exports remain newest-snapshot-only"
    print("  8-K depth filters the held union without acquiring ✓")


def test_export_prunes_csv_missing_from_newest_snapshot():
    with _TempPlane():
        with _patched(_Sec(), _Yahoo(), _Estimates()):
            pull("NVDA", sources=["yahoo"])
        exported = export_csv("NVDA")
        assert "yahoo_prices" in exported
        run_id = "9999-01-01T000000+0000-newest"
        staging = store.staging_dir("NVDA", run_id)
        frame = pd.DataFrame({"Value": [7]})
        frame.to_parquet(staging / "only_table.parquet")
        digest = store.sha256_file(staging / "only_table.parquet")
        store.write_snapshot_manifest(staging, {
            "issuer": "NVDA", "ticker": "NVDA", "run_id": run_id,
            "table_hashes": {"only_table": digest}, "originals": {},
        })
        store.commit_snapshot(staging, run_id)
        newest_export = export_csv("NVDA")
        assert newest_export == {"only_table": run_id}, newest_export
        assert not (store.CSV / "NVDA" / "yahoo_prices.csv").exists()
        assert (store.CSV / "NVDA" / "only_table.csv").is_file()
    print("  export prunes a stale CSV when newest snapshot omits that table ✓")


def test_a_sec_run_with_no_earnings_8k_reports_the_gap():
    with _TempPlane():
        with _patched(_Sec(), _Yahoo(), _Estimates(), _EightK(filings=[])):
            result = pull("NVDA", eight_ks=3)
        assert result["status"] == "RETRIEVED", "the statements still publish"
        assert "sec_8k" not in result["table_hashes"], result["table_hashes"]
        row = {r["dataset"]: r for r in _coverage(result)["rows"]}["sec_8k"]
        assert row["acquisition"] == "MISSING" and row["reason"] == "NOT_PUBLISHED", row
    print("  an 8-K request that finds no earnings release is a named gap ✓")


def test_eight_ks_without_the_sec_source_are_refused():
    try:
        pull("NVDA", sources=["yahoo"], eight_ks=2)
    except ValueError as exc:
        assert "sec" in str(exc), exc
    else:
        raise AssertionError("8-Ks come from the SEC provider")
    print("  8-Ks without the sec source are refused rather than silently skipped ✓")


def test_the_scope_key_is_stable_for_every_scope_that_predates_8k():
    # the hash a pre-8-K release wrote into its manifests: changing it would make every
    # held snapshot a cache miss and re-download the world
    golden = "a1735af32f50d0347396ec13c508cc3ee4c4bdc2d31358c7f6933be2315c8389"
    assert scope_key("VRT", "VRT", ["yahoo"], []) == golden
    assert scope_key("VRT", "VRT", ["yahoo"], [], None) == golden
    assert scope_key("VRT", "VRT", ["yahoo"], [], 0) == golden
    assert scope_key("VRT", "VRT", ["yahoo"], [], 10) != golden
    assert scope_key("VRT", "VRT", ["yahoo"], [], 10) \
        != scope_key("VRT", "VRT", ["yahoo"], [], 5)
    print("  the scope key is unchanged for pre-8-K scopes and splits by 8-K depth ✓")


class _Exhibit:
    """One filing attachment: filename, EDGAR document type, body (str or bytes)."""

    def __init__(self, filename="ex991.htm", doc_type="EX-99.1",
                 body="<html>release</html>", extension=None):
        self.document = filename
        self.document_type = doc_type
        self.extension = extension or Path(filename).suffix
        self.content = body


class _Attachments:
    """The one Attachments behaviour the provider uses: query(...).documents."""

    def __init__(self, docs):
        self._docs = docs

    def query(self, expr, include_data_files=True):
        wanted = expr.split("'")[1]
        return self.__class__([d for d in self._docs if d.document_type == wanted])

    @property
    def documents(self):
        return self._docs


class _EightKSecFiling:
    """An EntityFiling's surface: index metadata plus one filing's attachments."""

    def __init__(self, accession, filing_date, items, exhibits=(), attachments_fail=False):
        self.accession_no = accession
        self.filing_date = filing_date
        self.items = items
        self._exhibits = list(exhibits)
        self._attachments_fail = attachments_fail

    @property
    def attachments(self):
        if self._attachments_fail:
            raise RuntimeError("full-text submission unavailable")
        return _Attachments(self._exhibits)


class _EightKCompany:
    def __init__(self, filings):
        self._filings = filings
        self.asked = None

    def get_filings(self, form=None, amendments=True):
        self.asked = (form, amendments)
        return self._filings


def test_the_sec_provider_keeps_only_item_202_filings_and_their_exhibit():
    company = _EightKCompany([
        _EightKSecFiling("0000000000-25-000010", "2025-02-06", "2.02,9.01",
                         [_Exhibit(filename="deck.htm", doc_type="EX-99",
                                   body="<html>deck</html>"), _Exhibit()]),
        _EightKSecFiling("0000000000-25-000009", "2025-01-08", "7.01"),
        _EightKSecFiling("0000000000-25-000008", "2024-12-19", ""),
        _EightKSecFiling("0000000000-24-000011", "2024-10-30", "2.02,9.01",
                         [_Exhibit(filename="release99.htm", doc_type="EX-99",
                                   body="<html>other</html>"),
                          _Exhibit(filename="release9901.htm", doc_type="EX-99.01")]),
        _EightKSecFiling("0000000000-24-000010", "2024-08-01", "2.02,9.01",
                         attachments_fail=True),
        _EightKSecFiling("0000000000-24-000009", "2024-05-02", "2.02,9.01",
                         [_Exhibit(body=None)]),
        _EightKSecFiling("0000000000-24-000008", "2024-02-08", "2.02,9.01",
                         [_Exhibit(filename="ex991.pdf", body=b"%PDF-1.4 release")]),
    ])
    with _TempPlane():
        with mock.patch("edgar.Company", return_value=company), \
                mock.patch.object(sec, "_identity", return_value="me"):
            frame, meta = sec.earnings_8k("NVDA", 5, issuer="NVDA")
        assert company.asked == ("8-K", False), \
            "amendments are excluded by the query, not filtered after the fetch"
        assert list(frame["accession"]) == ["0000000000-25-000010",
                                            "0000000000-24-000011",
                                            "0000000000-24-000008"], frame
        paths = {r["accession"]: r["exhibit_path"] for r in meta["filings"]
                 if r["exhibit_path"]}
        assert Path(paths["0000000000-25-000010"]).read_text() == "<html>release</html>", \
            "Exhibit 99.1 is preferred over a plain EX-99"
        assert Path(paths["0000000000-24-000011"]).read_text() == "<html>release</html>", \
            "EX-99.01 is preferred over a plain EX-99"
        assert Path(paths["0000000000-24-000008"]).suffix == ".pdf", paths
        assert Path(paths["0000000000-24-000008"]).read_bytes() == b"%PDF-1.4 release"
        statuses = {r["accession"]: r["status"] for r in meta["filings"]}
        assert statuses == {"0000000000-25-000010": "RETRIEVED",
                            "0000000000-25-000008": "MISSING",
                            "0000000000-24-000011": "RETRIEVED",
                            "0000000000-24-000010": "FAILED",
                            "0000000000-24-000009": "FAILED",
                            "0000000000-24-000008": "RETRIEVED"}, statuses
        assert "0000000000-25-000009" not in statuses, "a non-2.02 8-K is not this dataset"
        failures = {r["accession"]: r["detail"] for r in meta["filings"]
                    if r["status"] == "FAILED"}
        assert "full-text submission unavailable" in failures["0000000000-24-000010"], failures
        assert "empty" in failures["0000000000-24-000009"], failures
    print("  the SEC provider keeps Item 2.02 filings and preserves their exhibit ✓")


def test_export_refuses_a_snapshot_that_lost_a_recorded_table():
    with _TempPlane():
        with mock.patch.object(store, "now_iso", _ageing_clock()), \
                _patched(_Sec(), _Yahoo(), _Estimates()):
            pull("NVDA")                                     # holds the statements
            later = pull("NVDA", sources=["alpha_vantage"])  # newer, holds one table
        (Path(later["snapshot_dir"]) / "av_earnings_estimates.parquet").unlink()
        try:
            export_csv("NVDA")
        except ValueError as exc:
            assert "missing" in str(exc), exc
        else:
            raise AssertionError("a recorded file that vanished is corruption, not a gap")
        # the older snapshot still carries the table; using it would hide the damage
        assert not (store.CSV / "NVDA" / "av_earnings_estimates.csv").exists()
        err = io.StringIO()
        with mock.patch("sys.stdout", new_callable=io.StringIO), mock.patch("sys.stderr", err):
            assert main(["NVDA", "--export-csv"]) == 1, "a corrupt store is not a zero exit"
        assert "missing" in err.getvalue(), err.getvalue()
    print("  export refuses a snapshot that lost a recorded table, in the API and the CLI ✓")


def test_the_cli_refuses_export_combined_with_acquisition_flags():
    with _TempPlane():
        err = io.StringIO()
        with mock.patch("sys.stderr", err), mock.patch("sys.stdout", new_callable=io.StringIO):
            assert main(["NVDA", "--export-csv", "--earnings-8k", "10"]) == 1
        assert "--earnings-8k" in err.getvalue(), err.getvalue()
    print("  --export-csv refuses to silently ignore an acquisition flag ✓")


def test_a_degraded_provider_logs_a_warning_with_its_reason():
    """A dataset that failed is legible in the log, not only in the coverage rows."""
    with _TempPlane():
        with _LogCapture(pull_logger) as log:
            with _patched(_Sec(), _Yahoo(fail=True), _Estimates()):
                pull("NVDA")
        warnings = log.messages(logging.WARNING)
        assert any(m.startswith("yahoo_prices: FAILED") and "yahoo unavailable" in m
                   for m in warnings), warnings
        print("  a degraded provider logs a WARNING naming its reason ✓")


def test_a_successful_pull_logs_one_finish_line_per_dataset():
    with _TempPlane():
        t = _Transcripts()
        with _LogCapture(pull_logger) as log:
            with _patched(_Sec(), _Yahoo(), _Estimates(), transcripts=t):
                result = pull("NVDA")
        assert result["status"] == "RETRIEVED", result
        infos = log.messages(logging.INFO)
        assert any(m.startswith("sec: ") for m in infos), infos
        assert any(m.startswith("yahoo: ") for m in infos), infos
        assert "av_earnings_estimates: ok" in infos, infos
        for quarter in t.quarters:
            assert f"av_transcript {quarter}: ok" in infos, (quarter, infos)
        print("  a successful pull logs one finish INFO line per dataset ✓")


def test_a_cache_only_hit_logs_cached_and_nothing_else():
    with _TempPlane():
        with _patched(_Sec(), _Yahoo(), _Estimates(), transcripts=_Transcripts()):
            first = pull("NVDA")
        with _LogCapture(pull_logger) as log:
            with _patched(_Sec(fail=True), _Yahoo(fail=True), _Estimates(fail=True),
                          transcripts=_Transcripts(fail=True)):
                result = pull("NVDA", cache_only=True)
        assert result["status"] == "CACHED", result
        assert result["snapshot"]["run_id"] == Path(first["snapshot_dir"]).name, result
        messages = [r.getMessage() for r in log.records]
        assert len(messages) == 1, messages
        assert messages[0] == f"CACHED NVDA: snapshot {Path(first['snapshot_dir']).name}", messages
        print("  a cache_only hit logs one CACHED line and nothing else ✓")


if __name__ == "__main__":
    test_held_evidence_serves_transcript_union_without_provider_calls()
    test_held_evidence_filters_sources_transcripts_and_releases()
    test_the_sec_fixture_serves_a_twelve_quarter_avgo_history()
    test_the_sec_fixture_serves_a_calendar_year_vrt_history()
    test_the_sec_fixture_still_raises_for_a_filing_it_does_not_hold()
    test_estimate_metadata_names_a_period_field_the_payload_carries()
    test_reported_quarters_uses_filing_periods_and_skips_missing_entries()
    test_default_transcripts_without_sec_evidence_require_explicit_labels()
    test_a_first_pull_derives_twelve_transcript_quarters_from_its_sec_fetch()
    test_held_ticker_without_transcripts_serves_and_names_the_absence()
    test_held_vrt_filing_history_is_served_without_transcript_acquisition()
    test_short_sec_history_is_not_padded_and_is_reported()
    test_a_restricted_default_pull_stays_transcript_free_and_does_not_raise()
    test_a_repeat_pull_is_cached_with_zero_provider_calls()
    test_held_ticker_filters_to_requested_source_family()
    test_a_failing_provider_becomes_coverage_rows_not_an_exception()
    test_a_statement_that_fails_to_convert_is_parse_failed_not_absent()
    test_a_yahoo_dataset_failure_keeps_the_error_text()
    test_a_foreign_private_issuer_still_gets_a_row_per_statement()
    test_an_alpha_vantage_message_is_redacted_before_it_reaches_an_artifact()
    test_a_bogus_ticker_records_gaps_and_publishes_no_snapshot()
    test_a_failed_run_is_not_cached_and_the_scope_recovers()
    test_one_issuer_two_tickers_do_not_share_a_snapshot()
    test_serving_one_listing_does_not_mix_the_other_listings_tables()
    test_an_empty_source_set_is_refused_not_read_as_every_source()
    test_refresh_adds_a_version_and_leaves_the_first_untouched()
    test_a_transcript_quarter_reports_the_providers_own_reason()
    test_an_empty_transcript_is_missing_not_a_silent_success()
    test_a_repeated_quarter_is_asked_for_once()
    test_a_cached_quarter_is_reused_and_the_rest_is_acquired()
    test_the_cli_exits_non_zero_when_nothing_was_published()
    test_the_cli_prints_the_run_status_to_stderr()
    test_export_reads_only_the_latest_snapshot()
    test_export_keeps_a_named_index_as_a_column()
    test_publishing_a_thinner_snapshot_warns()
    test_the_cli_export_prints_provenance_and_makes_no_network_call()
    test_eight_k_filings_are_archived_per_filing_and_scoped_by_depth()
    test_export_prunes_csv_missing_from_newest_snapshot()
    test_a_sec_run_with_no_earnings_8k_reports_the_gap()
    test_eight_ks_without_the_sec_source_are_refused()
    test_the_scope_key_is_stable_for_every_scope_that_predates_8k()
    test_the_sec_provider_keeps_only_item_202_filings_and_their_exhibit()
    test_export_refuses_a_snapshot_that_lost_a_recorded_table()
    test_the_cli_refuses_export_combined_with_acquisition_flags()
    test_a_transcript_label_that_is_not_a_quarter_is_refused_at_the_boundary()
    test_the_cli_refuses_a_sentinel_mixed_with_quarter_labels()
    test_a_degraded_provider_logs_a_warning_with_its_reason()
    test_a_successful_pull_logs_one_finish_line_per_dataset()
    test_a_cache_only_hit_logs_cached_and_nothing_else()
    print("all offline pull tests passed")
