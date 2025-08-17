#!/usr/bin/env python3
"""
===============================================================================
GPT‑Review ▸ Module Entry Point  (python -m gpt_review)
===============================================================================

This is the canonical **module** entry point invoked via:

    python -m gpt_review [args…]

Responsibilities
----------------
* Handle a lightweight global flag: `--version`
* Print a helpful startup banner (version, Python, platform)
* Delegate the rest of the CLI parsing/execution to `review.main`

The console script remains separate and points to the same runtime:
    gpt-review  →  review.main
"""
from __future__ import annotations

import argparse
import platform
import sys
from pathlib import Path

from gpt_review import get_logger, get_version

# Defer the heavy import of `review` until after we handle --version
log = get_logger(__name__)


def _parse_cli(argv: list[str]) -> tuple[argparse.Namespace, list[str]]:
    """
    Extract global flags (currently just --version) and leave the rest
    for `review.main()` to parse.

    Returns a pair of (parsed_args, remaining_argv).
    """
    parser = argparse.ArgumentParser(
        prog="python -m gpt_review",
        add_help=False,  # `review.main` provides full help/usage
    )
    parser.add_argument("--version", action="store_true")
    args, remainder = parser.parse_known_args(argv)
    return args, remainder


def _print_banner() -> None:
    """
    Log a concise runtime banner. Helpful in pasted logs and CI output.
    """
    log.info(
        "GPT‑Review %s  |  Python %s  |  %s",
        get_version(),
        platform.python_version(),
        platform.platform(),
    )


def main() -> None:
    """
    Top‑level dispatcher for `python -m gpt_review`.
    """
    args, remaining = _parse_cli(sys.argv[1:])
    if args.version:
        # Print to stdout so tools can capture the raw version cleanly.
        print(get_version())
        sys.exit(0)

    _print_banner()

    # Delegate to the actual CLI implementation.
    try:
        # Lazy import keeps `-m gpt_review --version` fast and dependency‑light.
        from review import main as review_main
    except Exception as exc:  # pragma: no cover (import errors are rare)
        log.exception("Failed to import CLI driver (review.main): %s", exc)
        sys.exit(1)

    # Ensure `review.main()` sees the expected argv vector.
    sys.argv = [Path(sys.argv[0]).as_posix(), *remaining]

    try:
        review_main()
    except SystemExit:
        # Preserve intended exit codes from the underlying CLI.
        raise
    except KeyboardInterrupt:
        # Graceful Ctrl‑C handling (POSIX convention: 130)
        log.info("Interrupted by user (Ctrl‑C). Exiting.")
        sys.exit(130)
    except Exception as exc:  # pragma: no cover
        log.exception("Unhandled error in CLI driver: %s", exc)
        sys.exit(1)


if __name__ == "__main__":
    main()
