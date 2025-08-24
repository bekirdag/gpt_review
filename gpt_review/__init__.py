#!/usr/bin/env python3
"""
===============================================================================
GPT‑Review – Package Initialisation
===============================================================================

Exports
-------
* __version__     – Resolved from installed package metadata
* get_version()   – Helper returning the version string
* get_logger()    – Re‑export of the packaged logger.get_logger

Side‑effects
------------
* Configures the root "gpt_review" logger on first import so all sub‑modules
  share the same rotating file + console handlers (see gpt_review/logger.py).

Notes
-----
This module imports the **packaged** logger implementation directly to avoid any
duplication. A thin top‑level `logger.py` shim exists for backward compatibility
with legacy imports (`from logger import get_logger`), and simply delegates to
`gpt_review.logger`.
"""
from __future__ import annotations

import logging
from importlib.metadata import PackageNotFoundError, version as _pkg_version

# Initialise the project‑wide logging configuration early.
# The underlying implementation is idempotent and will not duplicate handlers.
from gpt_review.logger import get_logger as _configure_logger  # packaged logger

# -----------------------------------------------------------------------------
# Logging – initialise root logger once
# -----------------------------------------------------------------------------
_ROOT_LOGGER = _configure_logger()  # sets up "gpt_review" logger
_ROOT_LOGGER.debug("Logger initialised in %s", __name__)

# -----------------------------------------------------------------------------
# Version helpers
# -----------------------------------------------------------------------------
try:
    __version__: str = _pkg_version("gpt-review")
except PackageNotFoundError:
    # Editable installs (pip install -e .) may lack distribution metadata.
    # Keep this fallback in sync with pyproject.toml
    __version__ = "0.3.0"
    _ROOT_LOGGER.warning(
        "Package metadata not found – using fallback version %s", __version__
    )


def get_version() -> str:
    """Return the package version string."""
    return __version__


# -----------------------------------------------------------------------------
# Logger accessor (re‑export)
# -----------------------------------------------------------------------------
def get_logger(name: str | None = None) -> logging.Logger:
    """
    Return a logger configured with GPT‑Review's handlers & formatting.

    Parameters
    ----------
    name : str | None
        • Explicit module logger name (e.g., __name__) or None for the root
          project logger "gpt_review".
    """
    # Reuse the same configuration function; it avoids duplicate handlers.
    from gpt_review.logger import get_logger as _get  # local import to prevent cycles

    return _get(name)


__all__ = ["__version__", "get_version", "get_logger"]
