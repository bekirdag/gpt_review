# logger.py
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
===============================================================================
GPT‑Review ▸ Logging Shim (compatibility module)
===============================================================================

Purpose
-------
Provide a small, stable wrapper so legacy imports like:

    from logger import get_logger

continue to work even if the fully featured implementation lives in
`gpt_review/logger.py` (the packaged module).

Behavior
--------
* First, delegate to the packaged logger (the "real" implementation).
* If delegation is not available (unusual), fall back to a minimal console logger.
"""

from __future__ import annotations

import logging
from typing import Optional

# Prefer the packaged implementation.
try:
    from gpt_review.logger import get_logger as _delegate_get_logger  # type: ignore
except Exception:
    _delegate_get_logger = None  # type: ignore[assignment]


def _fallback_get_logger(name: Optional[str] = None) -> logging.Logger:
    """
    Minimal, idempotent console logger used only if the packaged
    implementation cannot be imported for some reason.
    """
    root_name = "gpt_review"
    root = logging.getLogger(root_name)
    if not root.handlers:
        root.setLevel(logging.DEBUG)
        ch = logging.StreamHandler()
        ch.setLevel(logging.INFO)
        fmt = logging.Formatter(
            fmt="%(asctime)s | %(name)s | %(process)d | %(levelname)-8s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        ch.setFormatter(fmt)
        root.addHandler(ch)
        root.propagate = False
        root.debug("Fallback logger initialised in top-level logger.py (shim).")
    if name is None or name == root_name:
        return root
    logger = logging.getLogger(name)
    logger.setLevel(logging.DEBUG)
    logger.propagate = True
    return logger


def get_logger(name: Optional[str] = None) -> logging.Logger:
    """
    Return a configured logger. Prefer the packaged implementation; else fallback.
    """
    if _delegate_get_logger is not None:
        return _delegate_get_logger(name)
    return _fallback_get_logger(name)


__all__ = ["get_logger"]


if __name__ == "__main__":  # pragma: no cover
    log = get_logger(__name__)
    log.info("Logging shim is operational.")
