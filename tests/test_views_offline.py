#!/usr/bin/env python3
"""Derived views, offline: documents, transcript Markdown, the 8-K cell dump.

No network and no real store: the providers are doubled and every path is a temp plane.
The exhibit fixture carries the traps the real releases carry — cell text in nested
`<span>`, a colspan multi-row header, a rowspan label column, NBSP, `$ 637.9`, `(3.9)`,
`26.1 %`, `—`, `N/M`, `Diluted EPS(1)`, a split `$ | 1,234` and a range that must stay
blank — so the dump is pinned row by row rather than asserted by count.
"""
from __future__ import annotations

import csv
import io
import itertools
import json
import re
import shutil
import sys
from contextlib import ExitStack
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import pandas as pd
from lxml import html as lxml_html

from financial_data_pull import store, views
from financial_data_pull.cli import main
from financial_data_pull.providers import alphavantage, sec, yahoo
from financial_data_pull.pull import _statements_from_sec, export_csv, pull

from test_pull_offline import AVGO_SHAPE, _Sec
from test_store import _TempPlane

EXHIBIT = """<html><head><meta charset="utf-8"></head><body>
<div style="text-align:center">Reconciliation of GAAP to Non-GAAP <span>measures</span></div>
<table>
  <thead>
    <tr><th rowspan="2">Metric</th><th colspan="2">Three months ended</th>
        <th rowspan="2">Change</th></tr>
    <tr><th><span>June 30,</span> <span>2026</span></th>
        <th><div>June 30,<br>2025</div></th></tr>
  </thead>
  <tbody>
    <tr><td rowspan="2">Net sales</td><td>$ 637.9</td><td>512.4</td><td>24.5&nbsp;%</td></tr>
    <tr><td>612.4</td><td>500.0</td><td>N/M</td></tr>
    <tr><td>Diluted EPS(1)</td><td>$ 1.23</td><td>(3.9)</td><td>+86</td></tr>
    <tr><td>Restructuring</td><td>$</td><td>1,234</td><td>—</td></tr>
  </tbody>
</table>
<div>Fourth Quarter 2026 Guidance</div>
<table>
  <tr><td colspan="2"><span>Fourth Quarter 2026 Guidance</span></td></tr>
  <tr><td>Net sales</td><td>2,375</td></tr>
  <tr><td>Adjusted diluted EPS</td><td>$ 1.77 - $ 1.83</td></tr>
</table>
</body></html>"""

# A second filing with its own byte content, so the dump is provably the union of both.
SECOND = ("<html><body><table><tr><td>Net sales</td><td>1,234</td></tr></table>"
          "</body></html>")

SEGMENTS = {
    "2026Q1": [
        {"speaker": "Operator", "title": "Operator",
         "content": "Welcome to the first quarter call."},
        {"speaker": "Jane Doe", "title": "Chief Financial Officer",
         "content": "Revenue grew 12% — above plan."},
        {"speaker": "Jane Doe", "title": "Chief Financial Officer", "content": ""},
        {"speaker": "Operator", "title": "Operator", "content": "That concludes the call."},
    ],
    "2026Q2": [
        {"speaker": "Operator", "title": "Operator", "content": "Welcome back."},
    ],
}

QUARTER_ONE_MARKDOWN = """# NVDA — 2026Q1 earnings call transcript

Alpha Vantage EARNINGS_CALL_TRANSCRIPT · snapshot {snapshot} · generated — do not edit

**Operator** — *Operator*

Welcome to the first quarter call.

**Jane Doe** — *Chief Financial Officer*

Revenue grew 12% — above plan.

**Jane Doe** — *Chief Financial Officer*

**Operator** — *Operator*

That concludes the call.
"""

# One row per cell of the fixture, in the dump's own order:
# (table, row, col, kind, row_label, column_label, raw_text, value, unit)
FIXTURE_ROWS = [
    (0, 0, 0, "header", "Metric Three months ended Change", "", "Metric", "", ""),
    (0, 0, 1, "header", "Metric Three months ended Change", "", "Three months ended", "", ""),
    (0, 0, 3, "header", "Metric Three months ended Change", "", "Change", "", ""),
    (0, 1, 1, "header", "June 30, 2026", "Three months ended", "June 30, 2026", "", ""),
    (0, 1, 2, "header", "June 30, 2026", "Three months ended", "June 30, 2025", "", ""),
    (0, 2, 0, "data", "Net sales", "Metric", "Net sales", "", ""),
    (0, 2, 1, "data", "Net sales", "Three months ended June 30, 2026", "$ 637.9", "637.9", "$"),
    (0, 2, 2, "data", "Net sales", "Three months ended June 30, 2025", "512.4", "512.4", ""),
    (0, 2, 3, "data", "Net sales", "Change", "24.5 %", "24.5", "%"),
    (0, 3, 1, "data", "", "Three months ended June 30, 2026", "612.4", "612.4", ""),
    (0, 3, 2, "data", "", "Three months ended June 30, 2025", "500.0", "500.0", ""),
    (0, 3, 3, "data", "", "Change", "N/M", "", ""),
    (0, 4, 0, "data", "Diluted EPS(1)", "Metric", "Diluted EPS(1)", "", ""),
    (0, 4, 1, "data", "Diluted EPS(1)", "Three months ended June 30, 2026", "$ 1.23", "1.23", "$"),
    (0, 4, 2, "data", "Diluted EPS(1)", "Three months ended June 30, 2025", "(3.9)", "-3.9", ""),
    (0, 4, 3, "data", "Diluted EPS(1)", "Change", "+86", "86", ""),
    (0, 5, 0, "data", "Restructuring", "Metric", "Restructuring", "", ""),
    (0, 5, 1, "data", "Restructuring", "Three months ended June 30, 2026", "$", "", "$"),
    (0, 5, 2, "data", "Restructuring", "Three months ended June 30, 2025", "1,234", "1234", "$"),
    (0, 5, 3, "data", "Restructuring", "Change", "—", "", ""),
    (1, 0, 0, "header", "Fourth Quarter 2026 Guidance", "",
     "Fourth Quarter 2026 Guidance", "", ""),
    (1, 1, 0, "data", "Net sales", "Fourth Quarter 2026 Guidance", "Net sales", "", ""),
    (1, 1, 1, "data", "Net sales", "Fourth Quarter 2026 Guidance", "2,375", "2375", ""),
    (1, 2, 0, "data", "Adjusted diluted EPS", "Fourth Quarter 2026 Guidance",
     "Adjusted diluted EPS", "", ""),
    (1, 2, 1, "data", "Adjusted diluted EPS", "Fourth Quarter 2026 Guidance",
     "$ 1.77 - $ 1.83", "", "$"),
]

FILINGS = [{"accession": "0000000000-26-000001", "filing_date": "2026-07-29",
            "items": "2.02,9.01", "payload": EXHIBIT},
           {"accession": "0000000000-26-000002", "filing_date": "2026-04-22",
            "items": "2.02,9.01", "payload": SECOND}]


class _Statements:
    """One retrievable filing per form; the views do not care what it holds."""

    def __call__(self, ticker, form, index=0, issuer=None, ceiling=None):
        if index > 0:
            raise RuntimeError("no such filing")
        if ceiling:
            ceiling.spend("sec")
        frame = pd.DataFrame({"concept": ["us-gaap_Revenues"], "2026-06-30 (Q2)": [1.0]})
        meta = {"ticker": ticker, "form": form, "filing_date": "2026-07-29",
                "period_of_report": "2026-06-30", "accession": "0000000000-26-000009",
                "retrieved_at": store.now_iso(), "as_of": "2026-06-30"}
        if issuer:
            meta["original_path"] = str(store.save_raw(issuer, "sec", b"<filing/>",
                                                       suffix=".txt"))
        return {"income": frame}, meta


class _Filings:
    """Item 2.02 8-Ks whose Exhibit 99.1 is the fixture above."""

    def __init__(self, filings=None, fail: bool = False):
        self.fail = fail
        self.filings = FILINGS if filings is None else filings
        self.calls = 0

    def __call__(self, ticker, count, issuer=None, ceiling=None):
        self.calls += 1
        if self.fail:
            raise RuntimeError("8-K index unavailable")
        records = []
        for filing in self.filings[:count]:
            if ceiling:
                ceiling.spend("sec")
            payload = filing["payload"].encode()
            suffix = ".pdf" if payload[:5] == b"%PDF-" else ".htm"
            path = store.save_raw(issuer, "sec", payload, suffix=suffix)
            records.append({**{k: v for k, v in filing.items() if k != "payload"},
                            "ticker": ticker, "status": "RETRIEVED", "reason": None,
                            "detail": None,
                            "exhibit_file": filing.get("exhibit_file")
                            or f"ex99-{filing['accession']}{suffix}",
                            "exhibit_path": str(path)})
        meta = {"provider": "sec", "dataset": "sec_8k", "ticker": ticker, "status": "RETRIEVED",
                "reason": None, "detail": None, "retrieved_at": store.now_iso(),
                "filings": records}
        columns = ("ticker", "filing_date", "accession", "items", "exhibit_file",
                   "exhibit_path")
        return pd.DataFrame([{k: r[k] for k in columns} for r in records]), meta


class _Estimates:
    """Alpha Vantage EARNINGS_ESTIMATES: one entry, enough for the pull to publish it."""

    def __call__(self, symbol, horizon="12month", issuer=None, ceiling=None):
        if ceiling:
            ceiling.spend("alpha_vantage")
        data = {"estimates": [{"date": "2027-12-31", "horizon": "fiscal year",
                               "eps_estimate_average": "9.1224"}]}
        meta = {"provider": "alpha_vantage", "dataset": "av_earnings_estimates",
                "status": "RETRIEVED", "reason": None, "retrieved_at": store.now_iso(),
                "original_path": str(store.save_raw(issuer, "alpha_vantage",
                                                    json.dumps(data).encode(), suffix=".json"))}
        return data, meta


class _Transcripts:
    """Alpha Vantage EARNINGS_CALL_TRANSCRIPT, as the provider hands it over."""

    def __call__(self, symbol, quarter, issuer=None, ceiling=None):
        if ceiling:
            ceiling.spend("alpha_vantage_transcripts")
        segments = [dict(segment) for segment in SEGMENTS.get(quarter, [])]
        payload = json.dumps({"symbol": symbol, "quarter": quarter,
                              "transcript": segments}).encode()
        meta = {"provider": "alpha_vantage", "dataset": "av_transcript", "quarter": quarter,
                "status": "RETRIEVED" if segments else "MISSING",
                "reason": None if segments else "NOT_PUBLISHED",
                "retrieved_at": store.now_iso(),
                "original_path": str(store.save_raw(issuer, "alpha_vantage", payload,
                                                    suffix=".json"))}
        return {"symbol": symbol, "quarter": quarter, "segments": segments}, meta


def _patched(filings: _Filings, transcripts: _Transcripts) -> ExitStack:
    """Every provider a views test could reach is doubled: no test here is online."""
    stack = ExitStack()
    stack.enter_context(mock.patch.object(sec, "statements", _Statements()))
    stack.enter_context(mock.patch.object(sec, "earnings_8k", filings))
    stack.enter_context(mock.patch.object(alphavantage, "earnings_call_transcript",
                                         transcripts))
    stack.enter_context(mock.patch.object(yahoo, "fetch",
                                          mock.Mock(side_effect=RuntimeError("unused"))))
    stack.enter_context(mock.patch.object(alphavantage, "earnings_estimates", _Estimates()))
    return stack


def _ageing_clock():
    """A strictly increasing timestamp per call: two pulls in one test would otherwise
    share a second, and which snapshot is newest would come down to its random suffix."""
    ticks = itertools.count()
    return mock.Mock(side_effect=lambda: f"2026-09-21T14:{next(ticks):02d}:00+0000")


def _stored() -> None:
    """One store holding both kinds of evidence: an 8-K-only snapshot and then a
    transcripts-only one, so a view must take each table from the newest snapshot that
    carries it."""
    with mock.patch.object(store, "now_iso", _ageing_clock()), \
            _patched(_Filings(), _Transcripts()):
        pull("NVDA", sources=["sec"], eight_ks=2)
        # A held ticker now serves by default; explicitly refresh to fabricate the
        # second independent snapshot this newest-snapshot view test requires.
        pull("NVDA", sources=["alpha_vantage"], transcripts=["2026Q1", "2026Q2"],
             refresh=True)


def _dump(issuer: str = "NVDA") -> list[dict]:
    path = store.DERIVED / issuer / "8k_cells.csv"
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def _files(root: Path) -> dict[str, str]:
    return {str(p.relative_to(root)): store.sha256_file(p)
            for p in sorted(root.rglob("*")) if p.is_file()}


def test_statement_histories_keep_as_filed_rows_and_derive_quarters():
    with _TempPlane():
        sec_double = _Sec(shape=AVGO_SHAPE)
        with mock.patch.object(sec, "statements", sec_double):
            frames, _, _ = _statements_from_sec("AVGO", "AVGO", None, store.now_iso())
        # The latest 10-Q carries a comparative for this quarter and governs the older
        # filing's own-period value. A separately renamed row exists only where stated.
        target = "2025-08-03 (Q3)"
        newest = frames["income_quarterly_0"]
        newest.loc[0, target] = 12345
        renamed = newest.iloc[0].copy()
        renamed["label"] = "Renamed revenue"
        for column in newest.columns:
            if column not in ("concept", "label", "dimension", target):
                renamed[column] = float("nan")
        newest.loc[len(newest)] = renamed
        income = views.build_history(
            {name: frame for name, frame in frames.items() if name.startswith("income_")},
            "income")
        assert len(income.columns) == 15, income.columns
        assert income.iloc[:, :3].columns.tolist() == ["concept", "label", "dimension"]
        col = next(column for column in income if column.startswith("2025-08-03"))
        assert income.loc[income.label == "Total net revenue", col].iloc[0] == 12345, income
        renamed_row = income.loc[income.label == "Renamed revenue"].iloc[0]
        assert renamed_row[col] == 12345 and renamed_row.drop(labels=["concept", "label", "dimension", col]).isna().all(), renamed_row
        q4 = [column for column in income if "(Q4 derived)" in column]
        assert q4 and len(q4) == 3, income.columns
        cashflow = views.build_history(
            {name: frame for name, frame in frames.items() if name.startswith("cashflow_")},
            "cashflow")
        assert len(cashflow.columns) == 15 and all(
            column.endswith("derived)") for column in cashflow.columns[3:]), cashflow.columns
        balance = views.build_history(
            {name: frame for name, frame in frames.items() if name.startswith("balance_")},
            "balance")
        assert all(re.fullmatch(r"\d{4}-\d{2}-\d{2}", column)
                   for column in balance.columns[3:]), balance.columns

        # CSV histories come from the newest snapshot, and are not manifest tables.
        with mock.patch.object(sec, "statements", sec_double):
            acquired = pull("AVGO", sources=["sec"], eight_ks=0)
        exported = export_csv("AVGO")
        for family in ("income", "balance", "cashflow"):
            history_name = f"{family}_history"
            assert exported[history_name] == Path(acquired["snapshot_dir"]).name
            with (store.CSV / "AVGO" / f"{history_name}.csv").open(newline="") as fh:
                header = next(csv.reader(fh))
            assert len(header) == 15, header
            if family == "balance":
                assert all(re.fullmatch(r"\d{4}-\d{2}-\d{2}", col) for col in header[3:]), header
            else:
                assert sum("(Q4 derived)" in col for col in header) == 3, header
    print("  history views preserve filing rows and export derived quarters ✓")


def test_the_dump_holds_every_cell_of_every_table_in_the_expected_shape():
    with _TempPlane():
        _stored()
        counts = views.export_views("NVDA")
        rows = _dump()
        # completeness against an independent cell count of the two fixtures
        source = store.DERIVED / "NVDA" / "documents" / "8-k"
        cells = sum(len(lxml_html.fromstring(path.read_bytes()).xpath("//td|//th"))
                    for path in source.iterdir())
        assert len(rows) == cells == counts["8k_cells.csv"], (len(rows), cells, counts)
        assert list(rows[0]) == list(views.CELL_COLUMNS), list(rows[0])

        newest = [row for row in rows if row["accession"] == "0000000000-26-000001"]
        actual = [(int(row["table_index"]), int(row["row_index"]), int(row["col_index"]),
                   row["row_kind"], row["row_label"], row["column_label"], row["raw_text"],
                   row["value"], row["unit"]) for row in newest]
        wrong = [f"{got}\n!=\n{want}" for got, want in zip(actual, FIXTURE_ROWS)
                 if got != want]
        assert not wrong and len(actual) == len(FIXTURE_ROWS), "\n".join(wrong) or (
            f"{len(actual)} rows, expected {len(FIXTURE_ROWS)}")

        # the filing-level fields, and the second filing's own rows, are present too
        payload = (source / f"{FILINGS[0]['filing_date']}-{FILINGS[0]['accession']}-"
                            f"ex99-{FILINGS[0]['accession']}.htm").read_bytes()
        for row in newest:
            assert row["filing_date"] == "2026-07-29", row
            assert row["exhibit_sha256"] == store.sha256_bytes(payload), row
        older = [row for row in rows if row["accession"] == "0000000000-26-000002"]
        assert [(row["row_label"], row["value"]) for row in older] \
            == [("Net sales", ""), ("Net sales", "1234")], older
        # newest filing first, then document order: the file is byte-stable by construction
        assert [row["accession"] for row in rows] == ["0000000000-26-000001"] * len(newest) \
            + ["0000000000-26-000002"] * len(older)
        print("  the dump holds every cell of every table, in the expected shape ✓")


def test_documents_carry_readable_names_and_verified_bytes():
    with _TempPlane():
        _stored()
        counts = views.export_views("NVDA")
        out = store.DERIVED / "NVDA" / "documents" / "8-k"
        assert counts["documents/8-k"] == len(FILINGS) == len(list(out.iterdir())), counts
        for filing in FILINGS:
            name = (f"{filing['filing_date']}-{filing['accession']}-"
                    f"ex99-{filing['accession']}.htm")
            copied = out / name
            assert copied.is_file(), name
            expected = store.sha256_bytes(filing["payload"].encode())
            assert store.sha256_file(copied) == expected, name
        assert counts["documents/transcript"] == len(SEGMENTS), counts
        print("  documents carry readable names and hash-equal bytes ✓")


def test_a_second_run_writes_nothing_and_a_tampered_original_writes_nothing_at_all():
    with _TempPlane():
        _stored()
        views.export_views("NVDA")
        derived = store.DERIVED / "NVDA"
        before = _files(derived)
        views.export_views("NVDA")
        assert _files(derived) == before, "a re-render must be byte-identical"
        # a store that lost a byte must fail the whole export, not part of it
        shutil.rmtree(derived)
        preserved = next(exhibit["path"] for exhibit in views._exhibits("NVDA")
                         if exhibit["accession"] == FILINGS[0]["accession"])
        preserved.write_bytes(preserved.read_bytes() + b"<!-- tampered -->")
        try:
            views.export_views("NVDA")
            raise AssertionError("a tampered original must abort, never copy unverified bytes")
        except ValueError as exc:
            assert str(preserved) in str(exc) and "unverified" in str(exc), exc
        assert not derived.exists() or _files(derived) == {}, \
            "a failed export must not leave a partial view behind"
        print("  a re-run writes nothing, and a tampered original writes nothing at all ✓")


def test_transcript_markdown_is_the_whole_call_and_is_deterministic():
    with _TempPlane():
        _stored()
        views.export_views("NVDA")
        out = store.DERIVED / "NVDA" / "documents" / "transcript"
        snapshot = sorted(p.name for p in store.snapshot_dirs("NVDA"))[-1]
        expected = QUARTER_ONE_MARKDOWN.format(snapshot=snapshot)
        assert (out / "2026Q1.md").read_text() == expected, (out / "2026Q1.md").read_text()
        assert (out / "2026Q2.md").read_text().count("**Operator**") == 1
        before = _files(out)
        views.export_transcripts("NVDA")
        assert _files(out) == before, "a re-render must be byte-identical"
        print("  a transcript renders whole (blank turns included) and byte-stable ✓")


def test_an_undeclared_document_is_read_as_utf8_not_latin1():
    """Real exhibits declare no charset at all; libxml2's own fallback is Latin-1, which
    would put `â€”` in the CSV where the filing wrote an em dash."""
    exhibit = {"accession": "0000000000-26-000003", "filing_date": "2026-01-01"}
    rows = views._exhibit_rows("<table><tr><td>Net sales</td><td>—</td></tr></table>".encode(),
                               exhibit, "0" * 64)
    assert [row["raw_text"] for row in rows] == ["Net sales", "—"], rows
    print("  an undeclared exhibit is read as UTF-8, not Latin-1 ✓")


def test_a_ticker_with_no_documents_derives_nothing():
    with _TempPlane():
        with mock.patch.object(store, "now_iso", _ageing_clock()), \
                _patched(_Filings(fail=True), _Transcripts()):
            pull("NVDA", sources=["sec"])
        counts = views.export_views("NVDA")
        assert counts == {"documents/8-k": 0, "documents/transcript": 0, "8k_cells.csv": 0}, counts
        assert not (store.DERIVED / "NVDA" / "8k_cells.csv").exists(), \
            "an empty export must not leave an empty artifact"
        print("  a ticker with no documents derives nothing, and no empty artifact ✓")


def test_a_malformed_accession_is_refused_rather_than_used_as_a_path():
    """The accession comes from filing metadata and is composed into a file name: one that
    is not a safe path component is refused instead of climbing out of the view."""
    with _TempPlane() as root:
        staging = store.staging_dir("NVDA", "snap-1")
        raw = store.save_raw("NVDA", "sec", b"<html><body></body></html>", suffix=".htm")
        store.write_snapshot_manifest(staging, {
            "issuer": "NVDA", "run_id": "snap-1", "table_hashes": {},
            "originals": {"sec_8k_../../../escaped": str(raw)}})
        store.commit_snapshot(staging, "snap-1")
        try:
            views.export_documents("NVDA")
            raise AssertionError("a malformed accession must not become a file name")
        except ValueError as exc:
            assert "exhibit name" in str(exc), exc
        written = [str(p.relative_to(root)) for p in root.rglob("*") if p.is_file()]
        assert all(p.startswith(("raw/", "tables/")) for p in written), written
    print("  a malformed accession is refused, never turned into a path ✓")


def test_a_vanished_original_or_table_is_a_clean_refusal_not_a_traceback():
    """A snapshot records the files it published, so a missing one is damage: it must be
    refused with a message the CLI can print, exactly as `--export-csv` refuses it."""
    with _TempPlane():
        _stored()
        preserved = next(exhibit["path"] for exhibit in views._exhibits("NVDA")
                         if exhibit["accession"] == FILINGS[0]["accession"])
        preserved.unlink()
        err = io.StringIO()
        with mock.patch("sys.stdout", io.StringIO()), mock.patch("sys.stderr", err):
            assert main(["NVDA", "--export-views"]) == 1, "a damaged store is not a zero exit"
        assert str(preserved) in err.getvalue(), err.getvalue()
        assert "Traceback" not in err.getvalue(), err.getvalue()
    with _TempPlane():
        _stored()
        snapshot = sorted(p.name for p in store.snapshot_dirs("NVDA"))[-1]
        (store.TABLES / "NVDA" / snapshot / "av_transcript.parquet").unlink()
        try:
            views.export_views("NVDA")
            raise AssertionError("a recorded table that vanished must be refused")
        except ValueError as exc:
            assert "parquet is missing" in str(exc), exc
    print("  a vanished original or table is refused with a message, not a traceback ✓")


def test_a_non_html_exhibit_is_named_and_skipped_not_parsed():
    """The SEC provider allows a PDF press release. It has no cells, so it is named on
    stderr and absent from the dump — never parsed as HTML that looks like a release with
    no numbers, and never a reason to drop the filings that do parse."""
    pdf = ("%PDF-1.7\n1 0 obj<</Type/Catalog>>endobj\nstream\nBT (Net sales 2,375) Tj "
           "ET\nendstream\n%%EOF\n")
    filings = [FILINGS[0],
               {"accession": "0000000000-26-000004", "filing_date": "2026-01-08",
                "items": "2.02,9.01", "payload": pdf, "exhibit_file": "ex99-release.pdf"}]
    with _TempPlane():
        with mock.patch.object(store, "now_iso", _ageing_clock()), \
                _patched(_Filings(filings), _Transcripts()):
            pull("NVDA", sources=["sec"], eight_ks=2)
        err = io.StringIO()
        with mock.patch("sys.stderr", err):
            counts = views.export_views("NVDA")
        assert "ex99-release.pdf" in err.getvalue(), err.getvalue()
        assert counts["8k_cells.csv"] == len(
            lxml_html.fromstring(EXHIBIT.encode()).xpath("//td|//th")), counts
        assert counts["documents/8-k"] == len(filings), counts
        assert {row["accession"] for row in _dump()} == {FILINGS[0]["accession"]}, _dump()
        # the PDF still reached the document view, byte-identical to what was preserved
        copied = store.DERIVED / "NVDA" / "documents" / "8-k"
        assert len(list(copied.iterdir())) == len(filings), list(copied.iterdir())
        assert [p.suffix for p in copied.iterdir()].count(".pdf") == 1
        print("  a non-HTML exhibit is named on stderr, skipped, and its copy kept ✓")


def test_the_cli_exports_views_offline_and_refuses_acquisition_flags():
    with _TempPlane():
        filings, transcripts = _Filings(), _Transcripts()
        with mock.patch.object(store, "now_iso", _ageing_clock()), _patched(filings,
                                                                           transcripts):
            pull("NVDA", sources=["sec"], eight_ks=2)
            pull("NVDA", sources=["alpha_vantage"], transcripts=["2026Q1", "2026Q2"],
                 refresh=True)
        # every provider now fails if touched: the export must ask none of them
        out = io.StringIO()
        with _patched(_Filings(fail=True), mock.Mock(side_effect=RuntimeError("offline"))), \
                mock.patch("sys.stdout", out):
            assert main(["NVDA", "--export-views"]) == 0, "an export is not a failed pull"
        lines = out.getvalue().splitlines()
        assert f"documents/8-k: {len(FILINGS)} files" in lines, lines
        assert f"documents/transcript: {len(SEGMENTS)} files" in lines, lines
        assert any(line.startswith("8k_cells.csv: ") and line.endswith(" rows")
                   for line in lines), lines
        with _patched(_Filings(), _Transcripts()), \
                mock.patch("sys.stdout", io.StringIO()), mock.patch("sys.stderr", io.StringIO()):
            assert main(["NVDA", "--export-views", "--earnings-8k", "2"]) == 1, \
                "--export-views refuses to silently ignore an acquisition flag"
            assert main(["NVDA", "--export-views", "--refresh"]) == 1
            assert main(["ZZZZ", "--export-views"]) == 0, "nothing to derive is not a failure"
            assert main(["NVDA", "--export-csv", "--export-views"]) == 0, \
                "both exports may run in one call; neither is silently dropped"
        print("  the CLI exports views with zero network and refuses acquisition flags ✓")


if __name__ == "__main__":
    test_statement_histories_keep_as_filed_rows_and_derive_quarters()
    test_the_dump_holds_every_cell_of_every_table_in_the_expected_shape()
    test_documents_carry_readable_names_and_verified_bytes()
    test_a_second_run_writes_nothing_and_a_tampered_original_writes_nothing_at_all()
    test_transcript_markdown_is_the_whole_call_and_is_deterministic()
    test_an_undeclared_document_is_read_as_utf8_not_latin1()
    test_a_ticker_with_no_documents_derives_nothing()
    test_a_malformed_accession_is_refused_rather_than_used_as_a_path()
    test_a_vanished_original_or_table_is_a_clean_refusal_not_a_traceback()
    test_a_non_html_exhibit_is_named_and_skipped_not_parsed()
    test_the_cli_exports_views_offline_and_refuses_acquisition_flags()
    print("all derived-view tests passed")
