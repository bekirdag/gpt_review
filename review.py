#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
===============================================================================
GPT‑Review ▸ Legacy CLI Shim
===============================================================================

This module exists to preserve the historical entry point
    gpt-review = "review:main"
while delegating to the modern CLI implemented at:

    gpt_review.cli:main

All functionality (browser, API, and multi‑iteration workflow) lives there.
"""

from __future__ import annotations

import sys

try:
    # Prefer the modern CLI
    from gpt_review.cli import main as _cli_main  # type: ignore
except Exception as exc:  # pragma: no cover
    # Fall back to a clear error rather than importing heavy legacy code
    from logger import get_logger  # lightweight shim
    log = get_logger(__name__)
    log.exception("Failed to import gpt_review.cli. Is the package installed correctly?")
    raise SystemExit(1) from exc


def main() -> None:
    """
    Delegate to the modern CLI entry point.
    """
    _cli_main()


if __name__ == "__main__":  # pragma: no cover
    main()
