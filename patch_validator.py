#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
===============================================================================
GPT‑Review ▸ JSON‑Schema Patch Validator (merged & hardened)
===============================================================================

Purpose
-------
Validate a single **JSON patch** produced by ChatGPT against the canonical
schema bundled with the package at `gpt_review/schema.json`, and enforce
additional runtime safety guards (path hygiene, Base64 correctness).

Public API
---------
* `validate_patch(patch_json: str | bytes | dict) -> dict`
    - Returns the parsed JSON object on success
    - Raises `jsonschema.ValidationError` on schema violations
    - Raises `json.JSONDecodeError` on malformed JSON
    - Raises `ValueError` on extra safety violations (paths/Base64)
* `is_safe_repo_rel_posix(path: str) -> bool`
    - Exported canonical path guard used across modules.

CLI usage
---------
    # Validate a JSON string literal
    python patch_validator.py '{"op":"create","file":"a.txt","body":"x","status":"in_progress"}'

    # Read JSON from stdin
    echo '{"op":"delete","file":"a","status":"completed"}' | python patch_validator.py -

    # Read JSON from a file
    python patch_validator.py -f /path/to/patch.json

    # Print the active schema (debugging/education)
    python patch_validator.py --schema

Design notes
------------
* The schema is loaded **once** at import time via `importlib.resources`.
* We compile a `Draft7Validator` for speed and structured errors.
* Extra guards go beyond the schema:
    - `file`/`target` must be safe repo‑relative **POSIX** paths (no abs/backslashes/.., not .git/).
      Leading "./" is **not allowed** (aligns with api_driver/workflow). Windows drive letters are rejected.
    - `body_b64` (when present) must be valid Base64 (strict check).
* Logging is centralised via the project logger.
"""
from __future__ import annotations

import argparse
import base64
import json
import re
import sys
from importlib import resources
from pathlib import PurePosixPath
from typing import Any, Dict

import jsonschema
from jsonschema import Draft7Validator, ValidationError

# Prefer the shim; it delegates to the packaged logger and avoids duplicate config.
try:
    from logger import get_logger  # type: ignore
except Exception:  # pragma: no cover
    from gpt_review import get_logger  # type: ignore

# -----------------------------------------------------------------------------
# Logging
# -----------------------------------------------------------------------------
log = get_logger(__name__)

# -----------------------------------------------------------------------------
# Load schema at import‑time (fast & safe)
# -----------------------------------------------------------------------------
def _load_schema() -> Dict[str, Any]:
    """
    Load the bundled schema from the installed package.

    Returns
    -------
    dict
        Decoded JSON schema content.

    Raises
    ------
    SystemExit
        If the schema cannot be located (broken install).
    """
    try:
        with resources.files("gpt_review").joinpath("schema.json").open(
            encoding="utf-8"
        ) as fh:
            schema = json.load(fh)
            return schema
    except FileNotFoundError as exc:  # pragma: no cover
        log.critical("schema.json not found inside package: %s", exc)
        raise SystemExit(1) from exc
    except json.JSONDecodeError as exc:  # pragma: no cover
        log.critical("schema.json is invalid JSON: %s", exc)
        raise SystemExit(1) from exc
    except Exception as exc:  # pragma: no cover
        log.critical("Failed to load schema.json: %s", exc)
        raise SystemExit(1) from exc


_SCHEMA: Dict[str, Any] = _load_schema()

# Sanity‑check the schema itself (fail fast if broken)
try:
    Draft7Validator.check_schema(_SCHEMA)
except Exception as exc:  # pragma: no cover
    log.critical("Bundled JSON‑Schema is invalid: %s", exc)
    raise

_VALIDATOR: Draft7Validator = Draft7Validator(_SCHEMA)

# -----------------------------------------------------------------------------
# Extra guards (beyond JSON‑Schema)
# -----------------------------------------------------------------------------
_ALLOWED_OPS = {"create", "update", "delete", "rename", "chmod"}
_ALLOWED_STATUS = {"in_progress", "completed"}
_MODE_RE = re.compile(r"^[0-7]{3,4}$")  # chmod mode (3 or 4 octal digits)
_DRIVE_PREFIX_RE = re.compile(r"^[A-Za-z]:")  # Windows drive letter


def _pretty_pointer(exc: ValidationError) -> str:
    """
    Human‑friendly location of the failing field (JSON Pointer‑ish).
    """
    if not exc.path:
        return "$"
    parts = ["$"]
    for p in exc.path:
        parts.append(str(p))
    return ".".join(parts)


def is_safe_repo_rel_posix(path: str) -> bool:
    """
    Canonical defensive path guard used across GPT‑Review.

    Rules:
      - POSIX separators only
      - not absolute (no leading '/')
      - no backslashes, no parent traversal ('..')
      - not under '.git/' and not '.git' itself
      - no Windows drive letters (e.g. 'C:...')
      - no redundant segments (e.g., 'a//b', 'a/./b'), no trailing '/'
      - leading './' is **not** allowed (normalization would change the string)

    Returns
    -------
    bool
        True if the path is a safe, repo‑relative POSIX string.
    """
    if not isinstance(path, str) or not path.strip():
        return False

    raw = path.strip()

    # Reject Windows/backslash, absolute paths, drive letters
    if "\\" in raw:
        return False
    if raw.startswith("/"):
        return False
    if _DRIVE_PREFIX_RE.match(raw):
        return False

    # Reject parent traversal and .git anywhere
    if ".." in raw.split("/"):
        return False
    if raw == ".git" or raw.startswith(".git/") or "/.git/" in raw or raw.endswith("/.git"):
        return False

    # Normalization must be stable (rejects './x', 'a//b', 'a/./b', trailing '/')
    p = PurePosixPath(raw)
    if str(p) != raw:
        return False

    # Ensure all segments non‑empty
    return all(seg for seg in p.parts)


def _require(cond: bool, msg: str) -> None:
    if not cond:
        raise ValueError(msg)


def _extra_safety_checks(data: Dict[str, Any]) -> None:
    """
    Apply stricter, runtime checks that complement (and do not replace)
    the JSON‑Schema guarantees.
    """
    op = data.get("op")
    status = data.get("status")

    # Basic enums (schema already constrains these; this adds clear messages)
    _require(isinstance(op, str) and op in _ALLOWED_OPS, f"Invalid or missing 'op' ({op!r}).")
    _require(isinstance(status, str) and status in _ALLOWED_STATUS, f"Invalid or missing 'status' ({status!r}).")

    def _check_path_field(key: str) -> None:
        val = data.get(key)
        _require(isinstance(val, str) and val.strip(), f"Missing or empty '{key}'.")
        _require(is_safe_repo_rel_posix(val), f"Unsafe/non‑POSIX '{key}': {val!r}.")

    if op in {"create", "update"}:
        _check_path_field("file")
        if "body_b64" in data:
            b64 = data["body_b64"]
            _require(isinstance(b64, str) and b64.strip(), "'body_b64' must be a non‑empty Base64 string.")
            try:
                base64.b64decode(b64, validate=True)
            except Exception:
                raise ValueError("Invalid Base64 in 'body_b64'.")

    elif op == "delete":
        _check_path_field("file")
        # If schema remains permissive for extras, warn (no hard failure).
        if "body" in data or "body_b64" in data:
            log.warning("delete patch contains body/body_b64; ignoring extra fields.")

    elif op == "rename":
        _check_path_field("file")
        _check_path_field("target")
        if data.get("file") == data.get("target"):
            log.warning("rename 'file' and 'target' are identical; no‑op rename will be ignored.")

    elif op == "chmod":
        _check_path_field("file")
        mode = data.get("mode")
        _require(isinstance(mode, str) and _MODE_RE.match(mode) is not None,
                 "Missing or invalid 'mode' for chmod (expected 3 or 4 octal digits).")


# =============================================================================
# Public API
# =============================================================================
def validate_patch(patch_json: str | bytes | Dict[str, Any]) -> Dict[str, Any]:
    """
    Validate **patch_json** (dict/str/bytes) against the bundled schema and
    additional safety guards.

    Returns
    -------
    dict
        Parsed JSON object.

    Raises
    ------
    jsonschema.ValidationError
        If the patch is invalid per JSON‑Schema.
    json.JSONDecodeError
        If *patch_json* is not valid JSON.
    ValueError
        If path/Base64 guards fail.
    """
    # Normalize input
    if isinstance(patch_json, bytes):
        patch_json = patch_json.decode()

    if isinstance(patch_json, str):
        data = json.loads(patch_json)
    elif isinstance(patch_json, dict):
        data = patch_json
    else:  # pragma: no cover
        raise TypeError(f"Unsupported payload type: {type(patch_json).__name__}")

    # Schema validation (raises jsonschema.ValidationError on first violation).
    _VALIDATOR.validate(data)

    # Extra safety validation (raises ValueError with concise messages).
    _extra_safety_checks(data)

    log.debug(
        "Patch validated successfully (op=%s, file=%s, status=%s)",
        data.get("op"),
        data.get("file"),
        data.get("status"),
    )
    return data


# =============================================================================
# CLI wrapper
# =============================================================================
def _cli(argv: list[str] | None = None) -> int:
    """
    Minimal command‑line interface for manual checks.

    Returns
    -------
    int
        Exit code (0 ok, 1 error).
    """
    argv = argv if argv is not None else sys.argv[1:]

    parser = argparse.ArgumentParser(
        prog="patch_validator.py",
        description="Validate a GPT‑Review JSON patch against the bundled schema.",
    )
    parser.add_argument(
        "payload",
        nargs="?",
        help="JSON string payload or '-' to read from stdin (omit when using -f/--file or --schema).",
    )
    parser.add_argument(
        "-f",
        "--file",
        dest="file",
        help="Read JSON payload from a file path.",
    )
    parser.add_argument(
        "--schema",
        action="store_true",
        help="Print the active JSON schema and exit.",
    )
    args = parser.parse_args(argv)

    # Print schema and exit.
    if args.schema:
        print(json.dumps(_SCHEMA, indent=2, ensure_ascii=False))
        return 0

    # Determine payload source (file, stdin, or positional)
    payload: str | None = None
    if args.file:
        try:
            payload = open(args.file, "r", encoding="utf-8").read()
        except Exception as exc:  # pragma: no cover
            log.error("Failed to read file '%s': %s", args.file, exc)
            return 1
    elif args.payload == "-":
        payload = sys.stdin.read()
    else:
        payload = args.payload

    if not payload:
        parser.print_usage(sys.stderr)
        log.error("Missing payload. Provide a JSON string, '-', or -f/--file.")
        return 1

    try:
        validate_patch(payload)
        print("✓ Patch is valid.")
        return 0
    except ValidationError as exc:
        log.error("❌ Patch invalid at %s: %s", _pretty_pointer(exc), exc.message)
        return 1
    except json.JSONDecodeError as exc:
        log.error("❌ Payload is not valid JSON: %s", exc)
        return 1
    except ValueError as exc:
        log.error("❌ Patch failed safety checks: %s", exc)
        return 1
    except Exception as exc:  # pragma: no cover
        log.exception("❌ Unexpected error: %s", exc)
        return 1


__all__ = ["validate_patch", "is_safe_repo_rel_posix"]


if __name__ == "__main__":  # pragma: no cover
    sys.exit(_cli())
