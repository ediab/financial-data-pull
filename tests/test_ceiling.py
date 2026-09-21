#!/usr/bin/env python3
"""Request ceiling: named limits bite, unnamed providers are only counted.

No network. The raise is scoped to the three acquisition providers (sec, yahoo,
alpha_vantage estimates). A transcript ceiling breach is the exception: it is
recorded as MISSING coverage rows, because one capped quarter must not discard
the quarters that did succeed.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from financial_data_pull import store
from financial_data_pull.providers import alphavantage
from financial_data_pull.pull import Ceiling, pull

from test_pull_offline import _Estimates, _Sec, _Yahoo, _http, _patched
from test_store import _TempPlane

TRANSCRIPT = {"symbol": "NVDA", "quarter": "2025Q1",
              "transcript": [{"speaker": "Operator", "title": "Operator",
                              "content": "Welcome to the call."}]}


def _refusal(fn) -> BaseException:
    try:
        fn()
    except (ValueError, PermissionError) as exc:
        return exc
    raise AssertionError("the call should have been refused")


def test_a_named_ceiling_refuses_the_call_that_exceeds_it():
    ceiling = Ceiling({"sec": 2})
    ceiling.spend("sec")
    ceiling.spend("sec")
    assert "ceiling exceeded" in str(_refusal(lambda: ceiling.spend("sec")))
    assert "ceiling exceeded" in str(_refusal(lambda: Ceiling({"sec": 0}).spend("sec")))
    assert ceiling.used == {"sec": 3}, ceiling.used  # counted, then refused
    print("  a named ceiling refuses the call that exceeds it ✓")


def test_an_unnamed_provider_is_counted_and_never_capped():
    ceiling = Ceiling({"alpha_vantage": 1})
    for _ in range(50):
        ceiling.spend("yahoo")
    assert ceiling.used == {"yahoo": 50}, ceiling.used
    assert bool(ceiling) is True, "providers guard with `if ceiling:` — it must be truthy"
    print("  an unnamed provider is counted but never capped ✓")


def test_a_sec_yahoo_or_alpha_vantage_breach_raises_out_of_pull():
    with _TempPlane():
        s, y, a = _Sec(), _Yahoo(), _Estimates()
        with _patched(s, y, a):
            for sources, limits in ((["sec"], {"sec": 1}),
                                    (["yahoo"], {"yahoo": 3}),
                                    (["alpha_vantage"], {"alpha_vantage": 0})):
                exc = _refusal(lambda src=sources, lim=limits:
                               pull("NVDA", sources=src, ceilings=lim))
                assert "ceiling exceeded" in str(exc), (sources, exc)
                assert store.snapshot_dirs("NVDA") == [], "a breached run publishes nothing"
    print("  a breach of a named live source raises and publishes nothing ✓")


def test_a_transcript_breach_is_recorded_not_raised():
    with _TempPlane():
        with mock.patch.object(alphavantage, "config") as cfg:
            cfg.get.return_value = "key"
            with mock.patch.object(alphavantage.urllib.request, "urlopen",
                                   _http(TRANSCRIPT)):
                with _patched(_Sec(), _Yahoo(), _Estimates()):
                    result = pull("NVDA", sources=["alpha_vantage"],
                                       transcripts=["2025Q1"],
                                       ceilings={"alpha_vantage_transcripts": 0})
        assert result["status"] == "RETRIEVED", result
        assert result["requests"] == {"alpha_vantage": 1,
                                      "alpha_vantage_transcripts": 1}, result["requests"]
        assert "av_transcript" not in result["table_hashes"]
        row = next(r for r in json.loads(Path(result["coverage_path"]).read_text())["rows"]
                   if r["dataset"] == "av_transcript_2025Q1")
        assert row["acquisition"] == "MISSING", row
        assert row["reason"] == "NOT_RETRIEVABLE", row
    print("  a transcript ceiling breach is MISSING rows, never an exception ✓")


if __name__ == "__main__":
    test_a_named_ceiling_refuses_the_call_that_exceeds_it()
    test_an_unnamed_provider_is_counted_and_never_capped()
    test_a_sec_yahoo_or_alpha_vantage_breach_raises_out_of_pull()
    test_a_transcript_breach_is_recorded_not_raised()
    print("all ceiling tests passed")
