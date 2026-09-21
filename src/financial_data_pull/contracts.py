"""Minimal JSON-contract validation helpers (Stage 1: no external deps).

Stage 2.5c widened these helpers beyond "required key exists": types, enums,
numeric bounds, string patterns, array sizes, uniqueness, and schema-declared
conditional requirements. They remain a small, readable validator rather than a
full JSON-Schema engine — but a contract that says `{}` is wrong must now
actually reject `{}`.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

# Contracts live at the repository root (single source of truth, per the plan) and are
# copied into the package by the build (pyproject force-include), so an installed copy
# can validate too. Prefer the packaged copy when it is populated.
_PACKAGE_SCHEMAS = Path(__file__).resolve().parent / "schemas"
_REPO_SCHEMAS = Path(__file__).resolve().parent.parent.parent / "schemas"
SCHEMA_DIR = (_PACKAGE_SCHEMAS if _PACKAGE_SCHEMAS.is_dir()
              and any(_PACKAGE_SCHEMAS.glob("*.json")) else _REPO_SCHEMAS)

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


def read_table(path):
    """Read a parquet table (pandas, pyarrow already a dependency of the stack)."""
    import pandas as pd
    return pd.read_parquet(path)


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
    return read_table(path)


def _type_ok(value: Any, declared: Any) -> bool:
    types = [declared] if isinstance(declared, str) else list(declared or [])
    if not types:
        return True
    return any(_TYPE_CHECKS.get(t, lambda _v: True)(value) for t in types)


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
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if "minimum" in prop and value < prop["minimum"]:
                errors.append(f"{schema_name}: {path} {value} is below the minimum "
                              f"{prop['minimum']}")
            if "maximum" in prop and value > prop["maximum"]:
                errors.append(f"{schema_name}: {path} {value} is above the maximum "
                              f"{prop['maximum']}")
        if isinstance(value, str):
            if prop.get("pattern") and not re.match(prop["pattern"], value):
                errors.append(f"{schema_name}: {path} {value!r} does not match "
                              f"{prop['pattern']}")
            if "minLength" in prop and len(value) < prop["minLength"]:
                errors.append(f"{schema_name}: {path} must be at least "
                              f"{prop['minLength']} characters")
        if isinstance(value, dict) and "minProperties" in prop:
            if len(value) < prop["minProperties"]:
                errors.append(f"{schema_name}: {path} needs at least "
                              f"{prop['minProperties']} key(s)")

    def check_object(obj: Any, schema_obj: dict, path: str) -> None:
        if not isinstance(obj, dict):
            errors.append(f"{schema_name}: {path or 'root'} must be an object")
            return
        for key in schema_obj.get("required", []):
            if key not in obj:
                errors.append(f"{schema_name}: {path}missing required key '{key}'")
                continue
            # an explicit null is a present value: fields typed [type, null] are
            # legitimately MISSING-labelled, which is the honest state, not an
            # absent key
            if obj[key] is None:
                allowed = (schema_obj.get("properties", {}).get(key, {}) or {}).get("type")
                nullable = allowed is not None and (
                    (isinstance(allowed, list) and "null" in allowed)
                    or allowed == "null")
                if not nullable:
                    errors.append(f"{schema_name}: {path}missing required key '{key}'")
        props = schema_obj.get("properties", {})
        for key, prop in props.items():
            if key not in obj:
                continue
            value = obj[key]
            check_value(value, prop, f"{path}{key}")
            if prop.get("type") == "object" and isinstance(value, dict):
                # recurse so a nested enum/pattern (e.g. run.json intake.state) is
                # actually enforced, not merely present in the schema
                check_object(value, prop, f"{path}{key}.")
            if prop.get("type") == "array" and isinstance(value, list):
                if "minItems" in prop and len(value) < prop["minItems"]:
                    errors.append(f"{schema_name}: {path}{key} needs at least "
                                  f"{prop['minItems']} item(s)")
                item = prop.get("items", {})
                if item.get("type") == "object":
                    for i, entry in enumerate(value):
                        check_object(entry, item, f"{path}{key}[{i}].")
                for spec in prop.get("unique_by", []):
                    fields = spec if isinstance(spec, list) else [spec]
                    seen = []
                    for i, entry in enumerate(value):
                        if not isinstance(entry, dict):
                            continue
                        got = tuple(entry.get(f) for f in fields)
                        if got in seen:
                            errors.append(f"{schema_name}: {path}{key}[{i}] duplicates "
                                          f"{'/'.join(fields)} {got}")
                        seen.append(got)
        for rule in schema_obj.get("required_when", []):
            trigger = obj.get(rule.get("key"))
            if trigger in rule.get("values", []):
                for key in rule.get("require", []):
                    if obj.get(key) in (None, "", [], {}):
                        errors.append(f"{schema_name}: {path}{rule.get('key')}={trigger} "
                                      f"requires '{key}'")

    check_object(instance, schema, "")
    return errors
