#!/usr/bin/env python3
"""
===============================================================================
GPT‑Review ▸ JSON‑Schema Patch Validator
===============================================================================

Purpose
-------
Validate a single **JSON patch** produced by ChatGPT against the canonical
schema bundled with the package at `gpt_review/schema.json`.

Public API
---------
* `validate_patch(patch_json: str | bytes) -> dict`
    - Returns the parsed JSON object on success
    - Raises `jsonschema.ValidationError` on schema violations
    - Raises `json.JSONDecodeError` on malformed JSON

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
* Logging is centralised via the project logger.
"""
from __future__ import annotations

import argparse
import json
import sys
from importlib import resources
from typing import Any, Dict

import jsonschema
from jsonschema import Draft7Validator, ValidationError

from logger import get_logger

# -----------------------------------------------------------------------------
# Logging
# -----------------------------------------------------------------------------
log = get_logger("patch_validator")

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
_VALIDATOR: Draft7Validator = Draft7Validator(_SCHEMA)


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


# =============================================================================
# Public API
# =============================================================================
def validate_patch(patch_json: str | bytes) -> Dict[str, Any]:
    """
    Validate **patch_json** (str/bytes) against the bundled schema.

    Returns
    -------
    dict
        Parsed JSON object.

    Raises
    ------
    jsonschema.ValidationError
        If the patch is invalid.
    json.JSONDecodeError
        If *patch_json* is not valid JSON.
    """
    if isinstance(patch_json, bytes):
        try:
            patch_json = patch_json.decode()
        except Exception as exc:
            # Mirror json.JSONDecodeError semantics for non‑utf8 bytes
            log.error("Failed to decode bytes payload as UTF‑8: %s", exc)
            raise

    data = json.loads(patch_json)

    # Perform validation (raises ValidationError on first violation).
    _VALIDATOR.validate(data)

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
        print(json.dumps(_SCHEMA, indent=2))
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
        # Pretty print pointer + message for humans, but keep exit code.
        log.error("❌ Patch invalid at %s: %s", _pretty_pointer(exc), exc.message)
        return 1
    except json.JSONDecodeError as exc:
        log.error("❌ Payload is not valid JSON: %s", exc)
        return 1
    except Exception as exc:  # pragma: no cover
        log.exception("❌ Unexpected error: %s", exc)
        return 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(_cli())
