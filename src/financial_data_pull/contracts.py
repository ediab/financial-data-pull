"""Minimal JSON-contract validation helpers.

One contract: `schemas/coverage.json`, inside the package so that an installed copy
validates against the same file the checkout does. These helpers check what it
declares — required keys, a single type per property, enums, array items and
`required_when` conditionals — and are deliberately not a general JSON-Schema
engine. Sounding exhaustive about everything else would be a promise this code does
not keep.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pandas as pd

# Contracts travel with the package (`financial_data_pull/schemas/`): one source of
# truth for the shapes this package reads and writes, reachable from a wheel as
# well as from the checkout.
SCHEMA_DIR = Path(__file__).resolve().parent / "schemas"

_TYPE_CHECKS = {
    "string": lambda v: isinstance(v, str),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "array": lambda v: isinstance(v, list),
    "object": lambda v: isinstance(v, dict),
    "null": lambda v: v is None,
}


def load_schema(name: str) -> dict[str, Any]:
    path = SCHEMA_DIR / f"{name}.json"
    return json.loads(path.read_text())


def read_verified_table(snap_dir, table: str):
    """Read one snapshot table only after its recorded hash still matches.

    Stored tables are the evidence behind a memo; a mismatch means the file on
    disk is not what was published, so this fails closed rather than serving it.
    """
    from . import store
    snap_dir = Path(snap_dir)
    manifest_path = snap_dir / "snapshot.json"
    if not manifest_path.is_file():
        raise ValueError(f"{snap_dir} is not a published snapshot (no snapshot.json)")
    manifest = json.loads(manifest_path.read_text())
    expected = (manifest.get("table_hashes") or {}).get(table)
    path = snap_dir / f"{table}.parquet"
    if expected is None or not path.is_file():
        raise KeyError(f"table {table!r} is not in snapshot {snap_dir.name}")
    actual = store.sha256_file(path)
    if actual != expected:
        raise ValueError(
            f"hash mismatch for {table} in {snap_dir.name}: stored {expected[:12]}… "
            f"but file hashes to {actual[:12]}…")
    return pd.read_parquet(path)


def _type_ok(value: Any, declared: Any) -> bool:
    """A declared type is a single name from the table above; a type array is beyond
    this validator and left unchecked. The one exception is a required key holding an
    explicit null: that is judged in `check_object`, against a `"null"` type."""
    if not isinstance(declared, str):
        return True
    return _TYPE_CHECKS.get(declared, lambda _v: True)(value)


def validate_against(instance: dict[str, Any], schema_name: str) -> list[str]:
    """Return a list of contract violations (empty list = valid)."""
    schema = load_schema(schema_name)
    errors: list[str] = []

    def check_value(value: Any, prop: dict, path: str) -> None:
        if not _type_ok(value, prop.get("type")):
            errors.append(f"{schema_name}: {path} has the wrong type "
                          f"(expected {prop.get('type')})")
            return
        if "enum" in prop and value not in prop["enum"]:
            errors.append(f"{schema_name}: {path} {value!r} is not one of {prop['enum']}")

    def check_object(obj: Any, schema_obj: dict, path: str) -> None:
        if not isinstance(obj, dict):
            errors.append(f"{schema_name}: {path or 'root'} must be an object")
            return
        props = schema_obj.get("properties", {})
        for key in schema_obj.get("required", []):
            # an explicit null counts as present only when the contract types the
            # property as null; otherwise the required value is missing
            if key not in obj or (obj[key] is None
                                  and (props.get(key) or {}).get("type") != "null"):
                errors.append(f"{schema_name}: {path}missing required key '{key}'")
        for key, prop in props.items():
            if key not in obj:
                continue
            value = obj[key]
            check_value(value, prop, f"{path}{key}")
            if prop.get("type") == "array" and isinstance(value, list):
                item = prop.get("items", {})
                if item.get("type") == "object":
                    for i, entry in enumerate(value):
                        check_object(entry, item, f"{path}{key}[{i}].")
        for rule in schema_obj.get("required_when", []):
            trigger = obj.get(rule.get("key"))
            if trigger in rule.get("values", []):
                for key in rule.get("require", []):
                    if obj.get(key) in (None, "", [], {}):
                        errors.append(f"{schema_name}: {path}{rule.get('key')}={trigger} "
                                      f"requires '{key}'")

    check_object(instance, schema, "")
    return errors
