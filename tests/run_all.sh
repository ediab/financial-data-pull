#!/usr/bin/env bash
# Run every financial-data-pull check. No network and no Excel: the providers are
# doubled in the pull tests, and the store tests write only into a temp plane.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
PY="${PY:-.venv/bin/python}"
echo "== Store integrity (atomic publish / hash gating / path containment)"
"$PY" tests/test_store.py
echo "== Pull orchestration, offline (cache / per-source snapshots / honest gaps)"
"$PY" tests/test_pull_offline.py
echo "== Request ceiling (named limits / unnamed counting / transcript breach)"
"$PY" tests/test_ceiling.py
