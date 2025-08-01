"""
===============================================================================
Unit‑test ▸ Session‑rule injection
===============================================================================

Confirms that the constant *EXTRA_RULES* in ``review.py`` contains the wording
we rely on to keep ChatGPT in **chunk‑by‑chunk** mode.

The test is intentionally lightweight – it does **not** launch a browser or
touch the network; it merely imports the module‑level string.
"""

from review import EXTRA_RULES


def test_extra_rules_content() -> None:
    """Core assertions: wording must include key phrases (case‑insensitive)."""
    text = EXTRA_RULES.lower()
    assert "chunk by chunk" in text
    assert "one script" in text
    assert "continue" in text
