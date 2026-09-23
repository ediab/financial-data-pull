#!/usr/bin/env python3
"""Verification checks run only against generated filing fixtures and a temp store."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from financial_data_pull import store
from financial_data_pull.pull import manifest, pull
from financial_data_pull.verify import verify_bundle
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


if __name__ == "__main__":
    for name, test in sorted(globals().copy().items()):
        if name.startswith("test_") and callable(test):
            test()
            print(f"  {name} ✓")
