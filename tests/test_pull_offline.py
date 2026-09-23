#!/usr/bin/env python3
"""Pull orchestration, offline: caching, per-source snapshots, honest gaps.

No network: two providers are replaced by doubles and the Alpha Vantage path
reads a patched HTTP response through the real provider code. The three success
examples from the brief are covered — a repeat pull is CACHED with zero provider
calls, a source set publishes its own snapshot and is not a cache hit for
another, and a failing provider degrades to coverage rows instead of raising.
"""
from __future__ import annotations

import io
import itertools
import json
import logging
import sys
from contextlib import ExitStack
from datetime import date, datetime, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import pandas as pd

from financial_data_pull import contracts, store
from financial_data_pull.cli import main
from financial_data_pull.providers import alphavantage, sec, yahoo
from financial_data_pull.pull import (QUARTER_LABEL, export_csv, last_completed_quarters,
                                      manifest, pull, read_table, scope_key)
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


class _Sec:
    """One retrievable filing per form; everything else is absent, as upstream reads it."""

    def __init__(self, fail: bool = False):
        self.calls = 0
        self.fail = fail

    def __call__(self, ticker, form, index=0, issuer=None, ceiling=None):
        self.calls += 1
        if self.fail or index > 0:
            raise RuntimeError("no such filing")
        if ceiling:
            ceiling.spend("sec")
        frame = pd.DataFrame({"concept": ["us-gaap_Revenues"], "2025-01-31 (FY)": [1.0]})
        meta = {"ticker": ticker, "form": form, "filing_date": "2025-03-01",
                "period_of_report": "2025-01-31", "accession": f"0000000{index}",
                "retrieved_at": store.now_iso(), "as_of": "2025-01-31"}
        if issuer:
            meta["original_path"] = str(store.save_raw(issuer, "sec", b"<filing/>", suffix=".txt"))
        return {"income": frame}, meta


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


def test_last_completed_quarters_follows_the_calendar():
    """The derived default: the quarter has to have *ended* to be asked for."""
    # mid-quarter dates: the current quarter is not completed
    assert last_completed_quarters(date(2026, 5, 10)) == \
        ["2025Q2", "2025Q3", "2025Q4", "2026Q1"]
    assert last_completed_quarters(date(2026, 2, 15)) == \
        ["2025Q1", "2025Q2", "2025Q3", "2025Q4"]
    assert last_completed_quarters(date(2026, 8, 7)) == \
        ["2025Q3", "2025Q4", "2026Q1", "2026Q2"]
    # first day of a quarter: the one that just ended counts as completed
    assert last_completed_quarters(date(2026, 1, 1)) == \
        ["2025Q1", "2025Q2", "2025Q3", "2025Q4"]
    assert last_completed_quarters(date(2026, 4, 1)) == \
        ["2025Q2", "2025Q3", "2025Q4", "2026Q1"]
    # the last day of a quarter: that quarter has not ended yet
    assert last_completed_quarters(date(2026, 3, 31)) == \
        ["2025Q1", "2025Q2", "2025Q3", "2025Q4"]
    print("  the last four completed quarters follow the calendar ✓")


def test_a_default_pull_derives_the_last_four_completed_quarters():
    with _TempPlane():
        t = _Transcripts()
        with _patched(_Sec(), _Yahoo(), _Estimates(), transcripts=t):
            result = pull("NVDA")
        expected = last_completed_quarters(datetime.now(timezone.utc).date())
        assert result["status"] == "RETRIEVED", result
        assert t.quarters == expected, (t.quarters, expected)
        rows = [r for r in _coverage(result)["rows"]
                if str(r["dataset"]).startswith("av_transcript_")]
        assert sorted(r["period"] for r in rows) == sorted(expected), rows
        assert "av_transcript" in result["table_hashes"], result["table_hashes"]
        # an explicit empty list opts out, and its scope is not the derived one
        assert result["scope_key"] != scope_key("NVDA", "NVDA",
                                                ["sec", "alpha_vantage", "yahoo"], [])
        print("  a plain pull derives the last four completed quarters ✓")


def test_a_restricted_default_pull_stays_transcript_free_and_does_not_raise():
    with _TempPlane():
        with _patched(_Sec(), _Yahoo(), _Estimates()):
            result = pull("NVDA", sources=["sec"])
        assert result["status"] == "RETRIEVED", result
        assert not any(str(r["dataset"]).startswith("av_transcript_")
                       for r in _coverage(result)["rows"])
        assert "av_transcript" not in result["table_hashes"]
        assert result["requests"] == {"sec": 2}, result["requests"]
        print("  a restricted default pull is transcript-free and does not raise ✓")


def test_a_repeat_pull_is_cached_with_zero_provider_calls():
    with _TempPlane():
        s, y, a = _Sec(), _Yahoo(), _Estimates()
        t = _Transcripts()
        with _patched(s, y, a, transcripts=t):
            first = pull("NVDA")
            asked = (s.calls, y.calls, a.calls, t.calls)
            second = pull("NVDA")
        assert first["status"] == "RETRIEVED", first
        assert first["requests"] == {"sec": 2, "yahoo": 8, "alpha_vantage": 1,
                                     "alpha_vantage_transcripts": 4}, first["requests"]
        assert second["status"] == "CACHED", second
        assert (s.calls, y.calls, a.calls, t.calls) == asked, \
            "a cached pull must ask no provider"
        assert len(store.snapshot_dirs("NVDA")) == 1
        assert len(list((store.COVERAGE / "NVDA").glob("*.json"))) == 1
        cached_only = pull("NVDA", cache_only=True)
        assert cached_only["status"] == "CACHED" and cached_only["cache_only"] is True
        assert cached_only["snapshot"]["run_id"] == Path(first["snapshot_dir"]).name
        print("  a repeat pull is CACHED with zero provider calls ✓")


def test_each_source_set_publishes_its_own_snapshot():
    with _TempPlane():
        s, y, a = _Sec(), _Yahoo(), _Estimates()
        with _patched(s, y, a):
            yahoo_only = pull("NVDA", sources=["yahoo"])
            sec_only = pull("NVDA", sources=["sec"])
            again = pull("NVDA", sources=["yahoo"])
        assert len(store.snapshot_dirs("NVDA")) == 2, "one scope, one snapshot"
        assert set(yahoo_only["table_hashes"]) == set(yahoo.DATASETS), yahoo_only["table_hashes"]
        assert all(k.startswith(("income_", "balance_", "cashflow_"))
                   for k in sec_only["table_hashes"]), sec_only["table_hashes"]
        assert set(yahoo_only["statuses"]) == set(yahoo.DATASETS), yahoo_only["statuses"]
        assert not any(k.startswith("yahoo_") for k in sec_only["statuses"]), sec_only["statuses"]
        # neither source set is a cache hit for the other, and yahoo was asked once
        assert sec_only["status"] == "RETRIEVED" and again["status"] == "CACHED"
        assert y.calls == 1 and s.calls > 0
        assert yahoo_only["scope_key"] != sec_only["scope_key"]
        print("  each source set publishes its own snapshot; neither satisfies the other ✓")


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
                estimates = pull("NVDA", sources=["alpha_vantage"])
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
        with _patched(s, y, a, transcripts=_Transcripts(fail=True)):
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
        with _patched(s, y, a, transcripts=_Transcripts(fail=True)):
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
                                       transcripts=["2025Q1", "2025Q2"])
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
                      transcripts=_Transcripts(fail=True)), \
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
                      transcripts=_Transcripts(fail=True)), \
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
                      transcripts=_Transcripts(fail=True)), \
                _LogCapture(pull_logger) as log, \
                mock.patch("sys.stdout", out), mock.patch("sys.stderr", err):
            assert main(["NVDA", "--quiet"]) == 0
        assert err.getvalue() == "", err.getvalue()
        assert json.loads(out.getvalue())["status"] == "CACHED"
        assert log.messages(logging.INFO) == [], log.messages(logging.INFO)
        out, err = io.StringIO(), io.StringIO()
        with _patched(_Sec(fail=True), _Yahoo(fail=True), _Estimates(fail=True),
                      transcripts=_Transcripts(fail=True)), \
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
        # the derived default always names valid quarters — the sentinel cannot leak in
        assert all(QUARTER_LABEL.fullmatch(q)
                   for q in last_completed_quarters(date(2026, 5, 10)))
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
            later = pull("NVDA", sources=["alpha_vantage"])  # a thinner latest version
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
                pull("NVDA", sources=["alpha_vantage"])
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
            without = pull("NVDA", sources=["sec"])
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
        # depth is part of the scope: a different count is its own acquisition
        assert len({deep["scope_key"], shallow["scope_key"], without["scope_key"]}) == 3
        assert shallow["status"] == "RETRIEVED" and again["status"] == "CACHED", (shallow, again)
        assert early["sec_8k"] == Path(deep["snapshot_dir"]).name, early
        assert (store.CSV / "NVDA" / "sec_8k.csv").is_file()
        # the newest run asked for no 8-Ks, so the export is the newest run: the CSV from the
        # earlier one goes rather than being left beside current files as if it were current
        exported = export_csv("NVDA")
        assert "sec_8k" not in exported, exported
        assert not (store.CSV / "NVDA" / "sec_8k.csv").exists(), "a stale export is pruned"
    print("  8-K filings are archived per filing, and a shallower depth is its own scope ✓")


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
    test_estimate_metadata_names_a_period_field_the_payload_carries()
    test_last_completed_quarters_follows_the_calendar()
    test_a_default_pull_derives_the_last_four_completed_quarters()
    test_a_restricted_default_pull_stays_transcript_free_and_does_not_raise()
    test_a_repeat_pull_is_cached_with_zero_provider_calls()
    test_each_source_set_publishes_its_own_snapshot()
    test_a_failing_provider_becomes_coverage_rows_not_an_exception()
    test_a_statement_that_fails_to_convert_is_parse_failed_not_absent()
    test_a_yahoo_dataset_failure_keeps_the_error_text()
    test_a_foreign_private_issuer_still_gets_a_row_per_statement()
    test_an_alpha_vantage_message_is_redacted_before_it_reaches_an_artifact()
    test_a_bogus_ticker_records_gaps_and_publishes_no_snapshot()
    test_a_failed_run_is_not_cached_and_the_scope_recovers()
    test_one_issuer_two_tickers_do_not_share_a_snapshot()
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
