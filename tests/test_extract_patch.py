"""
===============================================================================
Unit‑tests for *review._extract_patch*
===============================================================================

Scope
-----
* Verify that JSON patches are correctly extracted from messy assistant
  replies, including:
    • Wrapped in ```json … ``` fences
    • Curly‑braces inside quoted strings
    • Multiple JSON objects (first one wins)
    • Replies without JSON → **None**
"""
from __future__ import annotations

import json
import logging
from textwrap import dedent

import pytest

# The extractor is an internal helper (prefixed _); import explicitly
from review import _extract_patch as extract_patch

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------
def _patch_dict(**kwargs) -> dict:
    """
    Return a *minimal valid* patch dict merged with overrides.
    """
    base = {
        "op": "create",
        "file": "demo.js",
        "body": "console.log('{a:1}');",
        "status": "in_progress",
    }
    base.update(kwargs)
    return base


def _fence(txt: str, lang: str = "") -> str:
    """
    Wrap *txt* inside triple‑backtick fences, optional language label.
    """
    return f"```{lang}\n{txt}\n```"


# =============================================================================
# Tests
# =============================================================================
def test_code_fence_extraction():
    """
    JSON inside ```json … ``` should be parsed.
    """
    patch = _patch_dict()
    reply = dedent(
        f"""
        Sure, here is the patch:

        {_fence(json.dumps(patch, indent=2), "json")}

        ASKING‑FOR‑CONTINUE
        """
    )
    result = extract_patch(reply)
    assert result == patch
    log.info("Code‑fence extraction passed.")


def test_braces_inside_strings():
    """
    Braces inside the *body* string must not break the balanced‑brace parser.
    """
    body = "function foo() {{ return {{a:1}}; }}"
    patch = _patch_dict(body=body)
    reply = _fence(json.dumps(patch))
    result = extract_patch(reply)
    assert result["body"] == body
    log.info("Balanced‑brace parser handles inner braces.")


def test_multiple_json_objects():
    """
    When two JSON blobs appear, extractor should return the *first*.
    """
    first = _patch_dict(file="a.txt")
    second = _patch_dict(file="b.txt")
    reply = f"{_fence(json.dumps(first))}\n\nSome text\n\n{_fence(json.dumps(second))}"
    result = extract_patch(reply)
    assert result["file"] == "a.txt"
    log.info("First JSON object wins as expected.")


def test_no_json_returns_none():
    """
    Replies with no JSON patch should yield **None**.
    """
    reply = "I have no patch this time."
    assert extract_patch(reply) is None
    log.info("No‑JSON reply correctly returns None.")


def test_invalid_json_fails_gracefully():
    """
    Malformed JSON should return **None** (extractor logs error).
    """
    broken = "{ this is : not json }"
    reply = _fence(broken)
    assert extract_patch(reply) is None
    log.info("Invalid JSON returns None without raising.")
