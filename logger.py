#!/usr/bin/env python3
"""
===============================================================================
GPT‑Review ▸ Logger Shim
===============================================================================

Purpose
-------
Maintain backward compatibility for legacy imports:

    from logger import get_logger

without duplicating the logging implementation.  The canonical logger now lives
in `gpt_review/logger.py`.  This shim simply delegates to it.

Behaviour
---------
* Idempotent configuration (handled by the packaged logger).
* Safe to import early.
"""
from __future__ import annotations

import logging

# Delegate to the packaged logger (single source of truth)
from gpt_review.logger import get_logger as _get_logger


def get_logger(name: str | None = None) -> logging.Logger:
    """
    Return a configured logger from the packaged implementation.
    """
    return _get_logger(name)


if __name__ == "__main__":  # pragma: no cover
    log = get_logger(__name__)
    log.info("Logger shim active — delegating to gpt_review.logger")
