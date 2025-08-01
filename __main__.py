#!/usr/bin/env python3
"""
===============================================================================
GPT‑Review ▸ Module CLI Entrypoint
===============================================================================

This file allows the package to be executed with:

    python -m gpt_review  [args …]

It acts as a thin wrapper around **review.main()**, adding:

* `--version`   – print package version and exit
* A friendly banner that prints the resolved virtual‑env Python runtime
  and GPT‑Review version, helpful when users paste logs.
"""
from __future__ import annotations

import argparse
import platform
import sys
from pathlib import Path

from gpt_review import get_logger, get_version

# Defer heavy import until after --version parsing
logger = get_logger(__name__)


def _parse_cli(argv: list[str]) -> tuple[argparse.Namespace, list[str]]:
    """
    Extract global flags (`--version`) and leave the rest for review.main().
    """
    parser = argparse.ArgumentParser(
        prog="python -m gpt_review",
        add_help=False,  # help handled by review.main()
    )
    parser.add_argument("--version", action="store_true")
    args, remainder = parser.parse_known_args(argv)
    return args, remainder


def _print_banner() -> None:
    logger.info(
        "GPT‑Review %s – Python %s – %s",
        get_version(),
        platform.python_version(),
        platform.platform(),
    )


def main() -> None:
    """
    Top‑level CLI dispatcher.
    """
    args, remaining = _parse_cli(sys.argv[1:])
    if args.version:
        print(get_version())
        sys.exit(0)

    _print_banner()

    # --------------------------------------------------------------------- #
    # Delegate to review.main(), passing through remaining CLI arguments.
    # --------------------------------------------------------------------- #
    try:
        # Lazy import keeps startup lightweight when --version used.
        from review import main as review_main
    except Exception as exc:  # pragma: no cover
        logger.exception("Failed to import review module: %s", exc)
        sys.exit(1)

    # Replace sys.argv so review.main() sees the right CLI list
    sys.argv = [Path(sys.argv[0]).as_posix(), *remaining]
    review_main()


# ---------------------------------------------------------------------------#
# Python -m invocation guard
# ---------------------------------------------------------------------------#
if __name__ == "__main__":
    main()
