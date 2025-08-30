#!/usr/bin/env python3
# -*- coding: utf-8 -*-
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
This module prefers the **packaged** logger implementation. If that import
fails for any reason (broken install, partial environment), it falls back to
a minimal console logger so that importing `gpt_review` never crashes.
"""
from __future__ import annotations

import logging
from importlib.metadata import PackageNotFoundError, version as _pkg_version
from typing import Optional

# -----------------------------------------------------------------------------
# Logger bootstrap (prefer packaged implementation; else safe fallback)
# -----------------------------------------------------------------------------
try:
    # Primary: packaged implementation (expected in normal installs)
    from gpt_review.logger import get_logger as _delegate_get_logger  # type: ignore
except Exception:  # pragma: no cover
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
        root.debug("Fallback logger initialised in gpt_review/__init__.py.")
    if name is None or name == root_name:
        return root
    logger = logging.getLogger(name)
    logger.setLevel(logging.DEBUG)
    logger.propagate = True
    return logger

# Choose the effective accessor and initialise the root once
_effective_get_logger = _delegate_get_logger or _fallback_get_logger
_ROOT_LOGGER = _effective_get_logger(None)  # configure "gpt_review" root
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
# Logger accessor (public re‑export)
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
    return _effective_get_logger(name)

__all__ = ["__version__", "get_version", "get_logger"]
