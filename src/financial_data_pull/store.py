"""Immutable local evidence store.

Layout (private plane, gitignored):
    data/raw/<issuer>/<provider>/<sha256>/        original payloads
    data/tables/<issuer>/<snapshot-id>/           statement/analyst tables + snapshot.json
    data/coverage/<issuer>/<run-id>.json          acquisition/verification status rows

Rules:
- snapshots are versioned and immutable; refresh ADDS versions, never mutates
- partial failure preserves completed datasets and reports the rest
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .config import ROOT  # FINANCIAL_DATA_PULL_ROOT-aware; never site-packages by accident

DATA = ROOT / "data"
RAW = DATA / "raw"
TABLES = DATA / "tables"
COVERAGE = DATA / "coverage"

_COMPONENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]*$")


def safe_component(value: str, field: str = "identifier") -> str:
    """Reject identifiers that could escape their directory or collide."""
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field} must be a non-empty string")
    if not _COMPONENT.match(value) or value in (".", ".."):
        raise ValueError(f"{field} {value!r} is not a safe path component")
    return value


def safe_path(root: Path, *parts: str) -> Path:
    """Join validated components and assert the result stays under root."""
    for part in parts:
        safe_component(part)
    path = root.joinpath(*parts)
    root_resolved = root.resolve()
    resolved = path.resolve()
    if resolved != root_resolved and root_resolved not in resolved.parents:
        raise ValueError(f"path {path} escapes {root}")
    return path


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def atomic_write_json(path: Path, obj: dict) -> None:
    """Single-writer, atomic manifest publication."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(obj, fh, indent=2, default=str)
        Path(tmp).replace(path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def save_raw(issuer: str, provider: str, payload: bytes, suffix: str = "") -> Path:
    """Store an immutable original payload by hash; returns its path.

    An existing payload at the content-addressed path is re-verified rather
    than trusted — a mismatch means the store itself is corrupt.
    """
    issuer, provider = safe_component(issuer, "issuer"), safe_component(provider, "provider")
    if suffix and not re.fullmatch(r"\.[A-Za-z0-9]{1,8}", suffix):
        raise ValueError(f"suffix {suffix!r} is not a plain file extension")
    digest = sha256_bytes(payload)
    out = safe_path(RAW, issuer, provider) / digest / f"payload{suffix}"
    if out.exists():
        if sha256_file(out) != digest:
            raise ValueError(f"stored payload {out} does not match its content hash")
        return out
    out.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=out.parent)
    with os.fdopen(fd, "wb") as fh:
        fh.write(payload)
    Path(tmp).replace(out)
    return out


def staging_dir(issuer: str, snapshot_id: str) -> Path:
    """A private staging directory; nothing here is readable as a snapshot."""
    safe_component(snapshot_id, "snapshot_id")
    d = safe_path(TABLES, issuer) / f".staging-{snapshot_id}-{uuid.uuid4().hex[:8]}"
    d.mkdir(parents=True, exist_ok=False)
    return d


def commit_snapshot(staging: Path, snapshot_id: str) -> Path:
    """Publish a staged snapshot atomically. The manifest must already be
    inside staging, so a published directory is complete by construction."""
    safe_component(snapshot_id, "snapshot_id")
    if not (staging / "snapshot.json").is_file():
        raise ValueError(f"staging {staging} has no snapshot.json — refusing to publish")
    final = staging.parent / snapshot_id
    if final.exists():
        raise FileExistsError(f"snapshot {snapshot_id} already exists — refresh creates a new id")
    staging.rename(final)
    return final


def snapshot_dirs(issuer: str) -> list[Path]:
    """Only complete, published snapshots are visible to readers.

    Staging directories are hidden by name as well as by their missing manifest:
    `Path.glob('*')` matches dot-directories, so without this a crash between the
    manifest write and the rename would leave a staging directory that
    `--cache-only` could select.
    """
    root = TABLES / issuer
    if not root.exists():
        return []
    return sorted(d for d in root.glob("*")
                  if not d.name.startswith(".") and (d / "snapshot.json").is_file())


def latest_snapshot_dir(issuer: str) -> Path | None:
    dirs = snapshot_dirs(issuer)
    return dirs[-1] if dirs else None


def snapshot_by_id(issuer: str, snapshot_id: str) -> Path | None:
    """One published snapshot by its exact id, or None — a pinned read never falls
    back to a newer snapshot just because the pinned one is gone."""
    safe_component(issuer, "issuer")
    safe_component(snapshot_id, "snapshot_id")
    path = TABLES / issuer / snapshot_id
    return path if (path / "snapshot.json").is_file() else None


def write_snapshot_manifest(d: Path, manifest: dict) -> Path:
    manifest = dict(manifest, published_at=now_iso())
    atomic_write_json(d / "snapshot.json", manifest)
    return d / "snapshot.json"


def coverage_path(issuer: str, run_id: str) -> Path:
    return safe_path(COVERAGE, issuer) / f"{safe_component(run_id, 'run_id')}.json"
