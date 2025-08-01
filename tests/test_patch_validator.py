"""
===============================================================================
Unit‑tests for *patch_validator.validate_patch*
===============================================================================

The validator enforces the JSON‑Schema defined in
`gpt_review/schema.json`.  We exercise representative *valid* and
*invalid* patches for every operation to ensure:

* Required keys are enforced
* Mutual exclusivity of `body` / `body_b64`
* Enum / pattern constraints (op, status, mode)
* `jsonschema.ValidationError` is raised on bad input
"""
from __future__ import annotations

import base64
import json
import logging

import pytest
from jsonschema import ValidationError

from patch_validator import validate_patch

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------
def _good_base() -> dict:
    """
    Minimal valid *create* patch used as a template.
    """
    return {
        "op": "create",
        "file": "example.txt",
        "body": "hello",
        "status": "in_progress",
    }


def _as_json(patch: dict) -> str:
    """
    Serialize *patch* to JSON for validate_patch (expects str/bytes).
    """
    return json.dumps(patch)


def _expect_error(patch: dict):
    """
    Helper: assert that *patch* triggers ValidationError.
    """
    with pytest.raises(ValidationError):
        validate_patch(_as_json(patch))
        log.info("Expected schema validation failure: %s", patch)


# =============================================================================
# Positive cases – should pass
# =============================================================================
@pytest.mark.parametrize(
    "patch",
    [
        _good_base(),  # basic create
        {
            "op": "update",
            "file": "demo.md",
            "body": "# New",
            "status": "in_progress",
        },
        {
            "op": "create",
            "file": "logo.png",
            "body_b64": base64.b64encode(b"\x89PNG").decode(),
            "status": "in_progress",
        },
        {
            "op": "delete",
            "file": "old.txt",
            "status": "in_progress",
        },
        {
            "op": "rename",
            "file": "src/old.py",
            "target": "src/new.py",
            "status": "in_progress",
        },
        {
            "op": "chmod",
            "file": "script.sh",
            "mode": "755",
            "status": "completed",
        },
    ],
    ids=[
        "create_text",
        "update_text",
        "create_binary",
        "delete",
        "rename",
        "chmod",
    ],
)
def test_valid_patches(patch):
    """
    All *patch* examples above should validate cleanly.
    """
    assert validate_patch(_as_json(patch)) == patch
    log.info("Valid patch passed schema: %s", patch["op"])


# =============================================================================
# Negative cases – should fail
# =============================================================================
def test_missing_required_keys():
    bad = _good_base()
    del bad["file"]
    _expect_error(bad)


def test_body_and_body_b64_same_time():
    bad = _good_base()
    bad["body_b64"] = base64.b64encode(b"dup").decode()
    _expect_error(bad)


def test_invalid_op():
    bad = _good_base()
    bad["op"] = "copy"  # not allowed
    _expect_error(bad)


def test_invalid_status():
    bad = _good_base()
    bad["status"] = "done"
    _expect_error(bad)


def test_chmod_missing_mode():
    bad = {
        "op": "chmod",
        "file": "test.sh",
        "status": "in_progress",
    }
    _expect_error(bad)


def test_rename_missing_target():
    bad = {
        "op": "rename",
        "file": "a",
        "status": "in_progress",
    }
    _expect_error(bad)


def test_mode_pattern():
    bad = {
        "op": "chmod",
        "file": "ex.sh",
        "mode": "abc",
        "status": "in_progress",
    }
    _expect_error(bad)
