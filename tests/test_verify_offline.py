#!/usr/bin/env python3
"""Verification checks run only against generated filing fixtures and a temp store."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from financial_data_pull import store
from financial_data_pull.pull import manifest, pull
from financial_data_pull.verify import _own_revenue, verify_bundle
from test_pull_offline import AVGO_SHAPE, _Sec, _EightK, _Estimates, _Yahoo, _TempPlane, _patched


def _frames():
    result = {}
    for form in ("10-K", "10-Q"):
        double = _Sec(shape=AVGO_SHAPE)
        for i in range(len(double.periods[form])):
            tables, _ = double("AVGO", form, i)
            kind = "annual" if form == "10-K" else "quarterly"
            for statement, frame in tables.items():
                result[f"{statement}_{kind}_{i}"] = frame
    return result


def test_clean_and_tampered_filing_frames():
    with _TempPlane():
        tables = _frames()
        report = verify_bundle(tables, "AVGO")
        assert report["verdict"] == "CHECKED"  # release absence is a gap, not a failure
        assert {c["name"] for c in report["checks"]} == {
            "balance", "ytd_sum", "q4_fy", "revenue_release", "cash_reconcile", "quarter_window"}
        assert report["identified"]["equity"] == "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"
        frame = tables["balance_quarterly_0"]
        date_col = next(c for c in frame if str(c).startswith("2026-08-02"))
        idx = frame.index[frame.concept.astype(str).str.endswith("Assets")][0]
        frame.loc[idx, date_col] += 50_000
        failed = verify_bundle(tables, "AVGO")
        check = next(c for c in failed["checks"] if c["name"] == "balance")
        assert failed["verdict"] == "DISCREPANCY"
        assert not check["ok"] and "$1,000" in check["tolerance"] and "2026-08-02" in check["detail"]
        assert tables["balance_quarterly_0"].loc[idx, date_col] > 0


def test_publish_keeps_all_data_when_arithmetic_disagrees():
    with _TempPlane():
        double = _Sec(shape=AVGO_SHAPE)
        frame = double.frames["10-Q"][0]["balance"]
        col = next(c for c in frame if str(c).startswith("2026-08-02"))
        idx = frame.index[frame.concept.astype(str).str.endswith("Assets")][0]
        frame.loc[idx, col] += 50_000
        with _patched(double, _Yahoo(), _Estimates(), _EightK()):
            result = pull("AVGO", sources=["sec"], transcripts=[], eight_ks=0)
        assert result["report"]["verdict"] == "DISCREPANCY"
        assert "balance_quarterly_0" in result["tables"]
        assert result["tables"]["balance_quarterly_0"].loc[idx, col] > 0


def test_preserved_release_cells_and_missing_release_gap():
    with _TempPlane():
        tables = _frames()
        from financial_data_pull.verify import _own_revenue
        revenue = _own_revenue(tables)["2026Q3"]["value"]
        html = (f"<html><table><tr><th>Net sales</th><th>Three Months Ended</th></tr>"
                f"<tr><td>Net sales</td><td>{revenue / 1e6:.0f}</td></tr></table></html>").encode()
        exhibit = {"accession": "fixture", "filing_date": "2026-09-06"}
        q2_revenue = _own_revenue(tables)["2026Q2"]["value"]
        thousand_html = (f"<html><table><tr><td>Net sales</td><td>Three Months Ended</td></tr>"
                         f"<tr><td>Net sales</td><td>{q2_revenue / 1e3:.0f}</td></tr></table></html>").encode()
        exhibit_k = {"accession": "fixture-k", "filing_date": "2026-06-07"}
        report = verify_bundle(tables, "AVGO", exhibits=[
            (exhibit, html, "fixture-hash"), (exhibit_k, thousand_html, "fixture-hash-k")])
        release = next(c for c in report["checks"] if c["name"] == "revenue_release")
        assert release["ok"]
        assert any("no held release" in gap for gap in report["gaps"])
        assert report["verdict"] != "DISCREPANCY"


def test_publish_records_verdict_and_cached_serve_modes():
    with _TempPlane():
        sec_double = _Sec(shape=AVGO_SHAPE)
        with _patched(sec_double, _Yahoo(), _Estimates(), _EightK()):
            result = pull("AVGO", sources=["sec"], transcripts=[], eight_ks=0)
        recorded = manifest("AVGO")
        assert recorded["verification"]["verdict"] == result["report"]["verdict"]
        cov = json.loads(Path(result["coverage_path"]).read_text())
        assert cov["rows"] and all(r["verification"] == recorded["verification"]["verdict"] for r in cov["rows"])
        with _patched(_Sec(fail=True), _Yahoo(fail=True), _Estimates(fail=True), _EightK(fail=True)):
            cached = pull("AVGO", sources=["sec"], transcripts=[], eight_ks=0)
        assert cached["report"]["verdict_source"] == "recorded"
        run_id = "independent-yahoo"
        staging = store.staging_dir("AVGO", run_id)
        import pandas as pd
        frame = pd.DataFrame({"value": [1]})
        frame.to_parquet(staging / "yahoo_prices.parquet")
        digest = store.sha256_file(staging / "yahoo_prices.parquet")
        store.write_snapshot_manifest(staging, {
            "issuer": "AVGO", "ticker": "AVGO", "run_id": run_id,
            "sources": ["yahoo"], "transcripts": [], "eight_ks": 0,
            "table_hashes": {"yahoo_prices": digest}, "originals": {}})
        store.commit_snapshot(staging, run_id)
        with _patched(_Sec(fail=True), _Yahoo(fail=True), _Estimates(fail=True), _EightK(fail=True)):
            multi = pull("AVGO", sources=["sec", "yahoo"], transcripts=[], eight_ks=0)
        assert multi["report"]["verdict_source"] == "live"


def test_legacy_manifest_uses_live_report_and_note():
    with _TempPlane():
        with _patched(_Sec(shape=AVGO_SHAPE), _Yahoo(), _Estimates(), _EightK()):
            pull("AVGO", sources=["sec"], transcripts=[], eight_ks=0)
        snap = store.latest_snapshot_dir("AVGO")
        doc = json.loads((snap / "snapshot.json").read_text())
        doc.pop("verification")
        store.write_snapshot_manifest(snap, doc)
        with _patched(_Sec(fail=True), _Yahoo(fail=True), _Estimates(fail=True), _EightK(fail=True)):
            result = pull("AVGO", sources=["sec"], transcripts=[], eight_ks=0)
        assert result["report"]["verdict_source"] == "live"
        assert "verdict not recorded" in result["report"]["gaps"]


def _quarter_frames(ends):
    return {f"income_quarterly_{i}": pd.DataFrame([{
        "concept": "RevenueFromContractWithCustomerExcludingAssessedTax", "label": "Total revenue",
        "dimension": False, f"{end} (Q{((int(end[5:7]) - 1) // 3) + 1})": 1000.0 + i}])
        for i, end in enumerate(ends)}


def _window(tables):
    report = verify_bundle(tables, "GAPCO")
    return next(c for c in report["checks"] if c["name"] == "quarter_window"), report["verdict"]


def test_quarter_window_tests_day_gaps_at_eleven_quarters():
    with _TempPlane():
        spaced = ["2023-03-31", "2023-06-30", "2023-09-30", "2023-12-31", "2024-03-31", "2024-06-30",
                  "2024-09-30", "2024-12-31", "2025-03-31", "2025-06-30", "2025-09-30"]
        eleven, verdict = _window(_quarter_frames(spaced))
        assert eleven["ok"] and eleven["detail"] == "11 contiguous quarters noted", eleven
        assert verdict == "UNCHECKED"  # income-only tables: the balance and cash checks cannot run
        # eleven contiguous labels, but one pair sits a day apart: 2025-03-31 then 2025-04-01
        broken = [*spaced[:8], "2025-03-31", "2025-04-01", "2025-09-30"]
        eleven, verdict = _window(_quarter_frames(broken))
        assert not eleven["ok"] and verdict == "DISCREPANCY", eleven
        assert "gap" in eleven["detail"] and "2025-04-01" in eleven["detail"], eleven
        # eleven of twelve quarter-ends: the missing quarter is named, not just counted
        holed = [end for end in [*spaced, "2025-12-31"] if end != "2024-06-30"]
        eleven, verdict = _window(_quarter_frames(holed))
        assert not eleven["ok"] and "2024Q1..2024Q3" in eleven["detail"], eleven


def test_a_comparative_ytd_derives_the_q4_of_a_year_without_its_own_q3():
    concept = "RevenueFromContractWithCustomerExcludingAssessedTax"
    annual = pd.DataFrame([{"concept": concept, "label": "Total revenue", "dimension": False,
                            "2024-12-31 (FY)": 100.0}])
    # The FY2025 Q3 10-Q: its own quarter plus the FY2024 comparative, which is the only
    # held 9M for FY2024 because no FY2024 Q3 10-Q is in the window.
    later = pd.DataFrame([{"concept": concept, "label": "Total revenue", "dimension": False,
                           "2025-09-30 (Q3)": 30.0, "2024-09-30 (Q3)": 25.0,
                           "2025-09-30 (YTD)": 70.0, "2024-09-30 (YTD)": 65.0}])
    revenue = _own_revenue({"income_annual_0": annual, "income_quarterly_0": later})
    assert revenue["2024Q4"]["value"] == 35.0 and revenue["2024Q4"]["derived"], revenue
    assert "2024Q3" not in revenue, revenue  # a comparative's own quarter is outside the window
    # An own-filing 9M always beats a later filing's restated comparative.
    own = pd.DataFrame([{"concept": concept, "label": "Total revenue", "dimension": False,
                         "2024-09-30 (Q3)": 25.0, "2024-09-30 (YTD)": 66.0}])
    held = _own_revenue({"income_annual_0": annual, "income_quarterly_0": later,
                         "income_quarterly_1": own})
    assert held["2024Q4"]["value"] == 34.0, held


if __name__ == "__main__":
    for name, test in sorted(globals().copy().items()):
        if name.startswith("test_") and callable(test):
            test()
            print(f"  {name} ✓")
