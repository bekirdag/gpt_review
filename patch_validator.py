#!/usr/bin/env python3
"""
===============================================================================
GPT‑Review ▸ JSON‑Schema Patch Validator
===============================================================================

The **contract** between ChatGPT and the driver is one JSON object whose
structure is defined in `gpt_review/schema.json`.  This helper validates a
JSON string against that schema, returning the decoded Python `dict` on
success or raising `jsonschema.ValidationError` on failure.

Why a dedicated module?
-----------------------
* Centralises schema‑loading logic (`importlib.resources` works for wheels).
* Keeps `apply_patch.py` and `review.py` focused on their core tasks.
* Easier unit‑testing.

CLI usage
---------
```bash
python patch_validator.py '<json‑patch>'
echo "$json" | python patch_validator.py -
```
Returns exit 0 when valid, 1 when invalid (message printed to stderr).
"""

from __future__ import annotations

import json
import sys
from importlib import resources
from typing import Any, Dict

import jsonschema
from jsonschema import ValidationError

from logger import get_logger

# -----------------------------------------------------------------------------
# Logging
# -----------------------------------------------------------------------------
log = get_logger(__name__)

# -----------------------------------------------------------------------------
# Load schema at import‑time (fast & safe)
# -----------------------------------------------------------------------------
try:
    with resources.files("gpt_review").joinpath("schema.json").open(
        encoding="utf-8"
    ) as fh:
        _SCHEMA: Dict[str, Any] = json.load(fh)
except FileNotFoundError as exc:  # pragma: no cover
    log.critical("schema.json not found inside package: %s", exc)
    raise SystemExit(1) from exc


# =============================================================================
# Public API
# =============================================================================
def validate_patch(patch_json: str) -> Dict[str, Any]:
    """
    Validate **patch_json** (str / bytes) against the bundled schema.

    Returns
    -------
    dict
        Parsed JSON object.

    Raises
    ------
    jsonschema.ValidationError
        If the patch is invalid.
    json.JSONDecodeError
        If *patch_json* is not valid JSON.
    """
    if isinstance(patch_json, bytes):
        patch_json = patch_json.decode()

    data = json.loads(patch_json)
    jsonschema.validate(data, _SCHEMA)
    log.debug("Patch validated successfully (op=%s file=%s)", data.get("op"), data.get("file"))
    return data


# =============================================================================
# CLI wrapper
# =============================================================================
def _cli() -> None:
    """Minimal command‑line interface for manual checks."""
    if len(sys.argv) != 2:
        sys.exit("Usage: patch_validator.py <json-string | ->")

    payload_arg = sys.argv[1]
    payload = sys.stdin.read() if payload_arg == "-" else payload_arg

    try:
        validate_patch(payload)
        print("✓ Patch is valid.")
        sys.exit(0)
    except (ValidationError, json.JSONDecodeError) as exc:
        log.error("❌ Patch invalid: %s", exc)
        sys.exit(1)


if __name__ == "__main__":  # pragma: no cover
    _cli()
