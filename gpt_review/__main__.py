#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
===============================================================================
GPT‑Review ▸ Module Entry Point  (python -m gpt_review)
===============================================================================

Canonical invocation:
    python -m gpt_review [<cli args>]

What this does
--------------
* Provides a **thin, resilient wrapper** that:
  - Handles a fast `--version` path without importing the full package.
  - Prints a concise startup banner (version, Python, platform).
  - Delegates to the **modern CLI driver** (`gpt_review.cli:main`).
  - Falls back to historical `review.main` entry points for backward
    compatibility, if present in the environment.

Notes
-----
Keeping this entry point robust ensures both `python -m gpt_review` and the
`gpt-review` console script behave identically.
"""
from __future__ import annotations

import argparse
import platform
import sys
from importlib.metadata import PackageNotFoundError, version as _pkg_version
from pathlib import Path
from typing import Callable


# ─────────────────────────────────────────────────────────────────────────────
# CLI pre‑parsing (global flags only)
# ─────────────────────────────────────────────────────────────────────────────
def _parse_cli(argv: list[str]) -> tuple[argparse.Namespace, list[str]]:
    """
    Extract global flags (currently just --version) and leave the rest
    for the real CLI driver to parse.

    Returns
    -------
    (parsed_args, remaining_argv)
    """
    parser = argparse.ArgumentParser(
        prog="python -m gpt_review",
        add_help=False,  # The CLI provides full usage/help.
    )
    parser.add_argument(
        "--version", action="store_true", help="Print package version and exit."
    )
    args, remainder = parser.parse_known_args(argv)
    return args, remainder


# ─────────────────────────────────────────────────────────────────────────────
# Version resolution
# ─────────────────────────────────────────────────────────────────────────────
def _resolve_version() -> str:
    """
    Resolve the installed package version **without importing** gpt_review.

    Falls back to importing `gpt_review.__version__` only when distribution
    metadata is unavailable (e.g., editable installs).
    """
    try:
        return _pkg_version("gpt-review")
    except PackageNotFoundError:
        try:
            from gpt_review import __version__  # type: ignore
        except Exception:
            __version__ = "0.0.0"  # Last‑resort constant; keeps CLI usable.
        return __version__


# ─────────────────────────────────────────────────────────────────────────────
# Logging banner
# ─────────────────────────────────────────────────────────────────────────────
def _print_banner(version: str) -> None:
    """
    Log a concise runtime banner. Helpful in pasted logs and CI output.
    """
    from gpt_review import get_logger  # Local import keeps --version path fast.

    log = get_logger(__name__)
    log.info(
        "GPT‑Review %s  |  Python %s  |  %s",
        version,
        platform.python_version(),
        platform.platform(),
    )


# ─────────────────────────────────────────────────────────────────────────────
# Driver import (CLI preferred; legacy fallbacks)
# ─────────────────────────────────────────────────────────────────────────────
def _import_driver_main() -> Callable[[], int | None]:
    """
    Locate the concrete CLI driver to execute.

    Preference order:
      1) gpt_review.cli.main          (modern CLI)
      2) review.main                   (historical top‑level module)
      3) gpt_review.review.main        (namespaced legacy)
    """
    # 1) Preferred modern CLI
    try:
        from gpt_review.cli import main as cli_main  # type: ignore
        return cli_main
    except Exception as exc_first:  # pragma: no cover
        # 2) Legacy top‑level `review`
        try:
            from review import main as review_main  # type: ignore
            return review_main
        except Exception as exc_second:  # pragma: no cover
            # 3) Legacy namespaced `gpt_review.review`
            try:
                from gpt_review.review import main as review_ns_main  # type: ignore
                return review_ns_main
            except Exception as exc_third:  # pragma: no cover
                # Log all failures coherently and exit.
                from gpt_review import get_logger

                get_logger(__name__).exception(
                    "Failed to import CLI driver:\n"
                    "  • gpt_review.cli.main         → %s\n"
                    "  • review.main                 → %s\n"
                    "  • gpt_review.review.main      → %s",
                    exc_first,
                    exc_second,
                    exc_third,
                )
                sys.exit(1)


# ─────────────────────────────────────────────────────────────────────────────
# Entry
# ─────────────────────────────────────────────────────────────────────────────
def main() -> None:
    """
    Top‑level dispatcher for `python -m gpt_review`.

    * Fast‑path `--version`
    * Print runtime banner
    * Delegate *all remaining args* to the CLI driver
    """
    args, remaining = _parse_cli(sys.argv[1:])

    # Fast path: print version and exit without importing heavy modules.
    if args.version:
        print(_resolve_version())
        sys.exit(0)

    # Normal path: banner + dispatch to CLI driver.
    version = _resolve_version()
    _print_banner(version)

    driver_main = _import_driver_main()

    # Ensure the driver sees the expected argv vector.
    sys.argv = [Path(sys.argv[0]).as_posix(), *remaining]

    try:
        rc = driver_main()
        if isinstance(rc, int):
            sys.exit(rc)
    except SystemExit:
        # Preserve intended exit codes from the underlying CLI.
        raise
    except KeyboardInterrupt:
        from gpt_review import get_logger

        get_logger(__name__).info("Interrupted by user (Ctrl‑C). Exiting.")
        sys.exit(130)
    except Exception as exc:  # pragma: no cover
        from gpt_review import get_logger

        get_logger(__name__).exception("Unhandled error in CLI driver: %s", exc)
        sys.exit(1)


if __name__ == "__main__":
    main()
