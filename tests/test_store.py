#!/usr/bin/env python3
"""Store integrity: snapshot visibility, atomic publish, hash-gated reads, paths.

No network: the store is exercised directly.
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import pandas as pd

from financial_data_pull import contracts, store


class _TempPlane:
    """Point every private path at a temp directory for the duration of a test."""

    def __init__(self):
        self._td = tempfile.TemporaryDirectory()
        self.root = Path(self._td.name)

    def __enter__(self):
        self._saved = (store.RAW, store.TABLES, store.COVERAGE)
        store.RAW = self.root / "raw"
        store.TABLES = self.root / "tables"
        store.COVERAGE = self.root / "coverage"
        return self.root

    def __exit__(self, *exc):
        (store.RAW, store.TABLES, store.COVERAGE) = self._saved
        self._td.cleanup()


def _publish(issuer: str = "TEST", snapshot_id: str = "snap-1",
             table: str = "income_annual_0") -> Path:
    """One published snapshot holding one hash-recorded table."""
    staging = store.staging_dir(issuer, snapshot_id)
    frame = pd.DataFrame({"concept": ["us-gaap_Revenues"], "2025-01-31 (FY)": [1.0]})
    frame.to_parquet(staging / f"{table}.parquet")
    store.write_snapshot_manifest(staging, {
        "issuer": issuer, "run_id": snapshot_id,
        "table_hashes": {table: store.sha256_file(staging / f"{table}.parquet")}})
    return store.commit_snapshot(staging, snapshot_id)


def test_a_staging_directory_is_invisible():
    with _TempPlane():
        staging = store.staging_dir("TEST", "snap-1")
        (staging / "income_annual_0.parquet").write_bytes(b"half-written")
        assert store.snapshot_dirs("TEST") == [], "staging must not be readable"
        assert store.latest_snapshot_dir("TEST") is None
        # a crash between the manifest write and the rename must still leave the
        # staging directory invisible (Path.glob('*') matches dot-directories)
        store.write_snapshot_manifest(staging, {"issuer": "TEST", "table_hashes": {}})
        assert store.snapshot_dirs("TEST") == [], "manifest-inside-staging must stay hidden"
        assert store.latest_snapshot_dir("TEST") is None
        snap = store.commit_snapshot(staging, "snap-1")
        assert store.snapshot_dirs("TEST") == [snap]
        assert store.latest_snapshot_dir("TEST") == snap
        print("  a staging directory is invisible until the rename publishes it ✓")


def test_commit_refuses_an_incomplete_or_duplicate_snapshot():
    with _TempPlane():
        bare = store.staging_dir("TEST", "snap-1")
        try:
            store.commit_snapshot(bare, "snap-1")
            raise AssertionError("a staging directory without a manifest must not publish")
        except ValueError as exc:
            assert "snapshot.json" in str(exc), exc
        assert store.snapshot_dirs("TEST") == []
        published = _publish()
        again = store.staging_dir("TEST", "snap-1")
        store.write_snapshot_manifest(again, {"issuer": "TEST", "table_hashes": {}})
        try:
            store.commit_snapshot(again, "snap-1")
            raise AssertionError("an existing snapshot id must not be reused")
        except FileExistsError as exc:
            assert "snap-1" in str(exc), exc
        assert store.snapshot_dirs("TEST") == [published], "the published snapshot is untouched"
        print("  commit refuses a missing manifest and a duplicate run id ✓")


def test_a_changed_table_file_is_refused():
    with _TempPlane():
        snap = _publish()
        frame = contracts.read_verified_table(snap, "income_annual_0")
        assert frame["concept"].tolist() == ["us-gaap_Revenues"]
        path = snap / "income_annual_0.parquet"
        path.write_bytes(path.read_bytes()[:40])  # truncated, not the published file
        try:
            contracts.read_verified_table(snap, "income_annual_0")
            raise AssertionError("a table that no longer matches its hash must not be served")
        except ValueError as exc:
            assert "hash mismatch" in str(exc), exc
        print("  a truncated table is refused, never returned as evidence ✓")


def test_safe_component_rejects_escapes():
    for bad in ("..", ".", "/", "a/b", "../etc", "", "a b"):
        try:
            store.safe_component(bad, "issuer")
            raise AssertionError(f"{bad!r} must be refused")
        except ValueError:
            pass
    assert store.safe_component("BRK.B", "issuer") == "BRK.B"
    print("  identifiers that could escape their directory or collide are refused ✓")


def test_save_raw_is_content_addressed_and_re_verified():
    with _TempPlane():
        first = store.save_raw("TEST", "sec", b"payload", suffix=".txt")
        assert store.save_raw("TEST", "sec", b"payload", suffix=".txt") == first, \
            "the same bytes must address the same path"
        assert first.read_bytes() == b"payload"
        assert store.save_raw("TEST", "sec", b"other") != first
        # an existing file is re-verified rather than trusted: a payload that no
        # longer matches its own name means the store itself is corrupt
        first.write_bytes(b"tampered")
        try:
            store.save_raw("TEST", "sec", b"payload", suffix=".txt")
            raise AssertionError("a stored payload that lost its hash must be refused")
        except ValueError as exc:
            assert "content hash" in str(exc), exc
        for bad in ("/x", "..", ".toolongextension"):
            try:
                store.save_raw("TEST", "sec", b"x", suffix=bad)
                raise AssertionError(f"suffix {bad!r} must be refused")
            except ValueError:
                pass
        print("  raw payloads are content-addressed and re-verified on write ✓")


if __name__ == "__main__":
    test_a_staging_directory_is_invisible()
    test_commit_refuses_an_incomplete_or_duplicate_snapshot()
    test_a_changed_table_file_is_refused()
    test_safe_component_rejects_escapes()
    test_save_raw_is_content_addressed_and_re_verified()
    print("all store tests passed")
