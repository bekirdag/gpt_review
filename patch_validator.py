#!/usr/bin/env python3
"""
patch_validator.py — validate a single JSON patch against the bundled schema.

Return the parsed dict on success, or raise jsonschema.ValidationError.
"""
from importlib import resources
import json

import jsonschema


def _load_schema() -> dict:
    with resources.files("gpt_review").joinpath("schema.json").open(
        encoding="utf-8"
    ) as f:
        return json.load(f)


_SCHEMA = _load_schema()


def validate_patch(patch_json: str) -> dict:
    """Validate *patch_json* and return the decoded Python dict."""
    data = json.loads(patch_json)
    jsonschema.validate(data, _SCHEMA)  # raises on error
    return data
