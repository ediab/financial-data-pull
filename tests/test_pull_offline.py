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
import json
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
from financial_data_pull.pull import manifest, pull, read_table

from test_store import _TempPlane


def _http(payload: dict) -> mock.MagicMock:
    body = json.dumps(payload).encode()
    response = io.BytesIO(body)
    response.__enter__ = lambda self: self          # type: ignore[attr-defined]
    response.__exit__ = lambda self, *exc: False    # type: ignore[attr-defined]
    return mock.MagicMock(return_value=response)


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


def _patched(s, y, a) -> ExitStack:
    """All three providers doubled: no test in this file can reach the network."""
    stack = ExitStack()
    stack.enter_context(mock.patch.object(sec, "statements", s))
    stack.enter_context(mock.patch.object(yahoo, "fetch", y))
    stack.enter_context(mock.patch.object(alphavantage, "earnings_estimates", a))
    return stack


def _coverage(result: dict) -> dict:
    return json.loads(Path(result["coverage_path"]).read_text())


def _tree(root: Path) -> dict[str, str]:
    """Every file of a snapshot with its hash — the whole published version."""
    return {str(p.relative_to(root)): store.sha256_file(p)
            for p in sorted(root.rglob("*")) if p.is_file()}


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


def test_a_repeat_pull_is_cached_with_zero_provider_calls():
    with _TempPlane():
        s, y, a = _Sec(), _Yahoo(), _Estimates()
        with _patched(s, y, a):
            first = pull("NVDA")
            asked = (s.calls, y.calls, a.calls)
            second = pull("NVDA")
        assert first["status"] == "RETRIEVED", first
        assert first["requests"] == {"sec": 2, "yahoo": 8, "alpha_vantage": 1}, first["requests"]
        assert second["status"] == "CACHED", second
        assert (s.calls, y.calls, a.calls) == asked, "a cached pull must ask no provider"
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


def test_a_bogus_ticker_records_gaps_and_publishes_no_snapshot():
    with _TempPlane():
        s, y, a = _Sec(fail=True), _Yahoo(fail=True), _Estimates(fail=True)
        with _patched(s, y, a):
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
        with _patched(s, y, a):
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
                with _patched(_Sec(), _Yahoo(), _Estimates()):
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
                with _patched(_Sec(), _Yahoo(), _Estimates()):
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
                with _patched(_Sec(), _Yahoo(), _Estimates()):
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
                with _patched(_Sec(), _Yahoo(), _Estimates()):
                    first = pull("NVDA", sources=["alpha_vantage"],
                                      transcripts=["2025Q1"])
            assert "av_transcript" in first["table_hashes"], first["table_hashes"]
            # a widened scope reuses the held quarter and asks only for the new one
            with mock.patch.object(alphavantage.urllib.request, "urlopen",
                                   _http({"symbol": "NVDA", "quarter": "2025Q2",
                                          "transcript": [segment]})) as provider:
                with _patched(_Sec(), _Yahoo(), _Estimates()):
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
        with _patched(_Sec(fail=True), _Yahoo(fail=True), _Estimates(fail=True)), \
                mock.patch("sys.stdout", new_callable=io.StringIO):
            assert main(["ZZZZ"]) == 1, "a run that acquired nothing must not exit 0"
        with _patched(_Sec(), _Yahoo(), _Estimates()), \
                mock.patch("sys.stdout", new_callable=io.StringIO):
            assert main(["NVDA"]) == 0, "a published snapshot exits 0"
    print("  the CLI exits non-zero when nothing was published ✓")


if __name__ == "__main__":
    test_estimate_metadata_names_a_period_field_the_payload_carries()
    test_a_repeat_pull_is_cached_with_zero_provider_calls()
    test_each_source_set_publishes_its_own_snapshot()
    test_a_failing_provider_becomes_coverage_rows_not_an_exception()
    test_a_statement_that_fails_to_convert_is_parse_failed_not_absent()
    test_a_yahoo_dataset_failure_keeps_the_error_text()
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
    print("all offline pull tests passed")
