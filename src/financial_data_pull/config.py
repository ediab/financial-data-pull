"""Environment/config loading. Secrets stay in .env — never logged, never in
agent context. This module returns booleans/values only where needed and never
prints key material."""
from __future__ import annotations

import os
from pathlib import Path


def _resolve_root() -> Path:
    """Where the private plane lives.

    Order: `FINANCIAL_DATA_PULL_ROOT`, then the source checkout (recognised by
    `pyproject.toml`), then the current working directory. The last step matters:
    without it an installed copy would put `data/` beside site-packages, inside a
    virtualenv that a rebuild deletes.
    """
    override = os.environ.get("FINANCIAL_DATA_PULL_ROOT")
    if override:
        return Path(override).expanduser().resolve()
    source_root = Path(__file__).resolve().parent.parent.parent
    if (source_root / "pyproject.toml").is_file():
        return source_root
    return Path.cwd()


ROOT = _resolve_root()
ENV_PATH = ROOT / ".env"


def _parse_env_file(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def get(key: str) -> str | None:
    """Value of KEY from process env, else .env. Never log the result."""
    val = os.environ.get(key)
    if val:
        return val
    return _parse_env_file(ENV_PATH).get(key) or None
