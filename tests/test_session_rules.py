"""
===============================================================================
Unit‑test ▸ Session‑rule injection
===============================================================================

Confirms that the constant *EXTRA_RULES* in `review.py` contains the wording
we rely on to keep ChatGPT in **chunk‑by‑chunk** mode.

The test is intentionally lightweight – it does **not** launch a browser or
touch the network; it merely validates the module‑level string.
"""
from __future__ import annotations

import logging

from review import EXTRA_RULES

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")


def test_extra_rules_content() -> None:
    """Core assertions: wording must include key phrases (case-insensitive)."""
    assert isinstance(EXTRA_RULES, str)
    text = EXTRA_RULES.lower().strip()
    assert text, "EXTRA_RULES must not be empty"
    assert "chunk by chunk" in text
    assert "one script" in text
    assert "continue" in text
    log.info("Session rule keywords present.")


def test_extra_rules_no_code_fences() -> None:
    """
    Sanity check: the helper text should be plain prose (no ``` fences),
    because we inject it into a larger prompt and we expect raw JSON outputs.
    """
    assert "```" not in EXTRA_RULES
    log.info("Session rules contain no code fences.")
