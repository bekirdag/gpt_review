#!/usr/bin/env python3
"""
===============================================================================
GPT‑Review ▸ Unified Logging Facility (shim)
===============================================================================

Purpose
-------
This file is a **compatibility shim** so legacy imports like:

    from logger import get_logger

continue to work. All real configuration lives in the packaged logger at
`gpt_review.logger`, ensuring there is a single owner of handlers and no
duplicate configuration.

Features
--------
* Namespace routing:
    - If you pass a top‑level name like "review" or "apply_patch", we prefix it
      to "gpt_review.<name>" so it becomes a child of the project root logger.
* Idempotent behavior:
    - The packaged logger configures handlers exactly once; this shim never
      attaches handlers itself, it only delegates.
"""
from __future__ import annotations

import logging
from typing import Optional

# Delegate to the packaged implementation (single source of truth)
from gpt_review.logger import get_logger as _pkg_get_logger


def get_logger(name: Optional[str] = None) -> logging.Logger:
    """
    Return a logger configured with GPT‑Review's handlers & formatting.

    Behaviour
    ---------
    • If *name* is None or already starts with "gpt_review", delegate as‑is.
    • If *name* is a top‑level module (e.g., "review", "apply_patch",
      "patch_validator"), route it under the project namespace by prefixing
      "gpt_review.".
    """
    routed = name
    if name and not name.startswith("gpt_review"):
        routed = f"gpt_review.{name}"
    return _pkg_get_logger(routed)


__all__ = ["get_logger"]


if __name__ == "__main__":  # pragma: no cover
    # Quick demo without altering global configuration
    root = get_logger()  # resolves to packaged root logger
    root.info("Root logger OK (shim → packaged).")

    a = get_logger("review")
    b = get_logger("gpt_review.review")
    assert a is b
    a.info("Top-level 'review' correctly routed to 'gpt_review.review'.")
