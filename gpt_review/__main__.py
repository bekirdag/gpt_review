#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
===============================================================================
GPT‑Review ▸ Module Entry Point  (python -m gpt_review)
===============================================================================

Canonical invocation:
    python -m gpt_review [args…]

Responsibilities
----------------
* Handle a **lightweight** global flag: `--version`
  - For speed and to avoid side‑effects, we resolve the version **without**
    importing the package unless necessary.
* Print a concise startup banner (version, Python, platform) for normal runs.
* Delegate the rest of the CLI parsing/execution to `review.main`.

The console script remains separate and points to the same runtime:
    gpt-review  →  review.main
"""
from __future__ import annotations

import argparse
import platform
import sys
from importlib.metadata import PackageNotFoundError, version as _pkg_version
from pathlib import Path


def _parse_cli(argv: list[str]) -> tuple[argparse.Namespace, list[str]]:
    """
    Extract global flags (currently just --version) and leave the rest
    for `review.main()` to parse.

    Returns
    -------
    (parsed_args, remaining_argv)
    """
    parser = argparse.ArgumentParser(
        prog="python -m gpt_review",
        add_help=False,  # `review.main` provides full help/usage
    )
    parser.add_argument("--version", action="store_true")
    args, remainder = parser.parse_known_args(argv)
    return args, remainder


def _resolve_version() -> str:
    """
    Resolve the installed package version **without importing** gpt_review.

    Falls back to importing `gpt_review` only when distribution metadata
    is unavailable (e.g., editable installs).
    """
    try:
        return _pkg_version("gpt-review")
    except PackageNotFoundError:
        # Fallback: import only the attribute, at the cost of initialising logging.
        try:
            from gpt_review import __version__  # type: ignore
        except Exception:
            # Very defensive: last resort constant to ensure CLI stays usable.
            __version__ = "0.0.0"
        return __version__


def _print_banner(version: str) -> None:
    """
    Log a concise runtime banner. Helpful in pasted logs and CI output.
    """
    # Lazy import keeps `--version` path dependency-light.
    from gpt_review import get_logger  # local import to avoid side effects on cold path

    log = get_logger(__name__)
    log.info(
        "GPT‑Review %s  |  Python %s  |  %s",
        version,
        platform.python_version(),
        platform.platform(),
    )


def _import_review_main():
    """
    Import the CLI driver function `main` with resilience.

    We prefer the historical top-level `review` module (console script target),
    but also support a packaged `gpt_review.review` for environments that install
    the CLI within the package namespace.
    """
    try:
        from review import main as review_main  # type: ignore
        return review_main
    except Exception as exc_first:  # pragma: no cover
        # Fallback to a namespaced import (keeps the entrypoint robust).
        try:
            from gpt_review.review import main as review_main  # type: ignore
            return review_main
        except Exception as exc_second:
            from gpt_review import get_logger

            get_logger(__name__).exception(
                "Failed to import CLI driver: review.main (%s) and gpt_review.review.main (%s)",
                exc_first,
                exc_second,
            )
            sys.exit(1)


def main() -> None:
    """
    Top‑level dispatcher for `python -m gpt_review`.
    """
    args, remaining = _parse_cli(sys.argv[1:])

    # Fast path: print *only* the version and exit 0.
    if args.version:
        print(_resolve_version())
        sys.exit(0)

    version = _resolve_version()
    _print_banner(version)

    # Resolve the actual CLI implementation.
    review_main = _import_review_main()

    # Ensure the CLI driver sees the expected argv vector.
    sys.argv = [Path(sys.argv[0]).as_posix(), *remaining]

    try:
        review_main()
    except SystemExit:
        # Preserve intended exit codes from the underlying CLI.
        raise
    except KeyboardInterrupt:
        # Graceful Ctrl‑C handling (POSIX convention: 130)
        from gpt_review import get_logger

        get_logger(__name__).info("Interrupted by user (Ctrl‑C). Exiting.")
        sys.exit(130)
    except Exception as exc:  # pragma: no cover
        from gpt_review import get_logger

        get_logger(__name__).exception("Unhandled error in CLI driver: %s", exc)
        sys.exit(1)


if __name__ == "__main__":
    main()
