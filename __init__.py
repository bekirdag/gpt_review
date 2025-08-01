"""
===============================================================================
GPT‑Review – Python package initialisation
===============================================================================

Exposes
-------
* **__version__** – Resolved at runtime from installed package metadata
* **get_version()** – Helper that returns the version string
* **get_logger()** – Re‑export of `logger.get_logger` with sane defaults

Side‑effects
------------
* Configures a daily‑rotating file + console logger on first import so that
  *all* sub‑modules inherit the same root handler hierarchy.
* Notes (v0.3.0, 2025‑08‑01):
    • Enforces *chunk‑by‑chunk* patching – ChatGPT must update one file per
      reply and ask the user to **continue** before proceeding.
"""
from __future__ import annotations

import logging
from importlib.metadata import PackageNotFoundError, version as _pkg_version

from logger import get_logger as _configure_logger  # local module import

# ---------------------------------------------------------------------------#
# Logging – initialise early
# ---------------------------------------------------------------------------#
_logger = _configure_logger()
_logger.debug("Logger initialised in %s", __name__)

# ---------------------------------------------------------------------------#
# Version helpers
# ---------------------------------------------------------------------------#
try:
    __version__: str = _pkg_version("gpt-review")
except PackageNotFoundError:
    # Editable installs (`pip install -e .`) don't have metadata – use fallback
    __version__ = "0.3.0"  # ↞ keep in sync with pyproject.toml
    _logger.warning(
        "Package metadata not found – using fallback version %s", __version__
    )


def get_version() -> str:
    """Return the package version string."""
    return __version__


def get_logger(name: str | None = None) -> logging.Logger:
    """
    Convenience wrapper so external scripts can share GPT‑Review’s logging
    configuration without importing the `logger` module directly.

    Example
    -------
    >>> from gpt_review import get_logger
    >>> log = get_logger(__name__)
    >>> log.info("Hello from outside!")
    """
    return logging.getLogger(name) if name else _logger


# ---------------------------------------------------------------------------#
# What we export when users do `from gpt_review import *`
# ---------------------------------------------------------------------------#
__all__ = ["__version__", "get_version", "get_logger"]
