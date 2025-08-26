#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
===============================================================================
GPT‑Review ▸ Iteration Runner (console wrapper)
===============================================================================

Purpose
-------
A small, dependency‑light wrapper that forwards execution to the
`gpt_review.orchestrator` CLI.  Keeping this shim separate allows us to expose
a dedicated console script (e.g. `gpt-review-iterate`) without coupling it to
the legacy `gpt-review` / `python -m gpt_review` entrypoints that drive the
classic patch‑loop.

Behaviour
---------
* Imports the package logger early (idempotent) for consistent formatting.
* Delegates argument parsing and execution to `orchestrator.main()`.
* Emits concise DEBUG/INFO breadcrumbs for traceability.
* Exits with the same code as the orchestrator.

Usage
-----
    # Once a console entrypoint is wired (pyproject), users will call:
    #   gpt-review-iterate <instructions> <repo> [options]

    # Until then, it can be run directly:
    python -m gpt_review.iterate  <instructions> <repo> [options]

Notes
-----
This module intentionally contains **no** business logic — it exists solely to
provide a clean, stable CLI surface that can be referenced from packaging.
"""
from __future__ import annotations

import sys

from gpt_review import get_logger
from gpt_review.orchestrator import main as _orchestrator_main

log = get_logger(__name__)


def main() -> None:
    """
    Delegate to the orchestrator's CLI entrypoint.

    We keep this wrapper so packaging can point a console script to a stable,
    single‑purpose module, while the orchestrator remains importable and testable.
    """
    log.debug("Dispatching to orchestrator.main() with argv: %r", sys.argv[1:])
    _orchestrator_main()


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        # Preserve orchestrator's intended exit code (e.g., 0, 1, 130).
        raise
    except KeyboardInterrupt:
        log.info("Interrupted by user (Ctrl‑C). Exiting.")
        sys.exit(130)
    except Exception as exc:  # pragma: no cover (defensive)
        log.exception("Unhandled error in iteration runner: %s", exc)
        sys.exit(1)
