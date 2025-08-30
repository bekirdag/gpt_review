#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
===============================================================================
GPT‑Review ▸ Unified Logging Facility (packaged)
===============================================================================

Purpose
-------
Provide one **centralised**, **idempotent** logger configuration used across the
project.  Other modules should obtain loggers via:

    from gpt_review.logger import get_logger

A thin compatibility shim can live at the repository root `logger.py` so legacy
imports (`from logger import get_logger`) continue to work without duplication.

Key features
------------
* Console output – INFO level by default (override via env).
* Daily rotating file – DEBUG level, 7 days retention (both tunable).
* Idempotent – root handlers are configured **once**; child loggers propagate.
* Resilient – falls back to a temp dir, then console‑only, if log dir unwritable.
* Environment overrides:
    GPT_REVIEW_LOG_DIR   – log directory (default: ./logs)
    GPT_REVIEW_LOG_LVL   – console level  (DEBUG / INFO / WARNING / … or numeric)
    GPT_REVIEW_LOG_ROT   – rotation schedule ("midnight", "H", "M", …)
    GPT_REVIEW_LOG_BACK  – number of backup files (default 7)
    GPT_REVIEW_LOG_UTC   – truthy → timestamps & rotation in UTC (1/true/yes/on)
    GPT_REVIEW_LOG_JSON  – truthy → emit JSON lines to console
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from typing import Optional

# Only the root project logger "gpt_review" owns handlers; children propagate.
_ROOT_LOGGER_NAME = "gpt_review"

# ════════════════════════════════════════════════════════════════════════════
# Helpers
# ════════════════════════════════════════════════════════════════════════════
def _is_truthy(val: str | None) -> bool:
    """Return True if *val* represents a truthy setting."""
    if val is None:
        return False
    return val.strip().lower() in {"1", "true", "yes", "on", "y", "t"}


def _parse_level(val: str | None, default: int = logging.INFO) -> int:
    """
    Parse an environment level value which may be a name ("INFO") or an integer ("20").
    Falls back to *default* on invalid input.
    """
    if val is None:
        return default
    s = val.strip()
    if not s:
        return default
    if s.isdigit():
        try:
            return int(s)
        except Exception:
            return default
    name = s.upper()
    name_to_level = {
        "CRITICAL": logging.CRITICAL,
        "ERROR": logging.ERROR,
        "WARNING": logging.WARNING,
        "WARN": logging.WARNING,
        "INFO": logging.INFO,
        "DEBUG": logging.DEBUG,
        "NOTSET": logging.NOTSET,
    }
    return name_to_level.get(name, default)


# ════════════════════════════════════════════════════════════════════════════
# Defaults & environment overrides
# ════════════════════════════════════════════════════════════════════════════
_DEFAULT_DIR = Path("logs")
_LOG_DIR_ENV = os.getenv("GPT_REVIEW_LOG_DIR", str(_DEFAULT_DIR))

# Keep both the raw name (for banner) and parsed numeric level (for handlers)
_CONSOLE_LEVEL_ENV = os.getenv("GPT_REVIEW_LOG_LVL", "INFO")
CONSOLE_LEVEL = _parse_level(_CONSOLE_LEVEL_ENV, default=logging.INFO)
CONSOLE_LEVEL_NAME = (_CONSOLE_LEVEL_ENV or "INFO").strip().upper()

ROTATE_WHEN = os.getenv("GPT_REVIEW_LOG_ROT", "midnight")  # TimedRotatingFileHandler 'when'
BACKUP_COUNT = int(os.getenv("GPT_REVIEW_LOG_BACK", "7"))
USE_UTC = _is_truthy(os.getenv("GPT_REVIEW_LOG_UTC"))
JSON_CONSOLE = _is_truthy(os.getenv("GPT_REVIEW_LOG_JSON"))

# ════════════════════════════════════════════════════════════════════════════
# Formatters
# ════════════════════════════════════════════════════════════════════════════
FORMAT = "%(asctime)s | %(name)s | %(process)d | %(levelname)-8s | %(message)s"
DTFMT = "%Y-%m-%d %H:%M:%S"


class _JsonFormatter(logging.Formatter):
    """Minimal JSON formatter (useful for CI/log scraping)."""

    def format(self, record: logging.LogRecord) -> str:  # noqa: D401
        data = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(record.created))
            if USE_UTC
            else time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(record.created)),
            "name": record.name,
            "pid": record.process,
            "level": record.levelname,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            data["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(data, ensure_ascii=False)


def _human_formatter() -> logging.Formatter:
    fmt = logging.Formatter(fmt=FORMAT, datefmt=DTFMT)
    if USE_UTC:
        # type: ignore[attr-defined]
        fmt.converter = time.gmtime  # pragma: no cover (behavioural)
    return fmt


# ════════════════════════════════════════════════════════════════════════════
# Directory & handler utilities
# ════════════════════════════════════════════════════════════════════════════
def _ensure_log_dir(preferred: Path) -> Path:
    """
    Ensure a writable log directory exists.

    Preference order:
      1) $GPT_REVIEW_LOG_DIR (or ./logs)
      2) $TMPDIR/gpt-review-logs

    Falls back to console‑only if everything fails.
    """
    # 1) Preferred path
    try:
        preferred = preferred.expanduser().resolve()
        preferred.mkdir(parents=True, exist_ok=True)
        # Explicit writability check
        test_path = preferred / ".writable"
        test_path.write_text("ok", encoding="utf-8")
        test_path.unlink(missing_ok=True)
        return preferred
    except Exception:
        pass

    # 2) Temp fallback
    try:
        tmp = Path(tempfile.gettempdir()) / "gpt-review-logs"
        tmp.mkdir(parents=True, exist_ok=True)
        return tmp
    except Exception:
        # 3) Final fallback handled by get_logger (console‑only)
        return Path(".")


def _root_log_filename(base_dir: Path) -> Path:
    """
    Use a single rotating file for the package ('gpt_review.log') so
    sub‑loggers don't create many separate files.
    """
    return base_dir / "gpt_review.log"


def _make_file_handler(log_dir: Path) -> Optional[TimedRotatingFileHandler]:
    """
    Create a rotating file handler inside *log_dir*.

    Returns None if the file handler cannot be created (permissions, etc.).
    """
    try:
        file_path = _root_log_filename(log_dir)
        fh = TimedRotatingFileHandler(
            filename=file_path,
            when=ROTATE_WHEN,
            interval=1,
            backupCount=BACKUP_COUNT,
            encoding="utf-8",
            utc=USE_UTC,  # rotate based on UTC when requested
        )
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(_human_formatter())
        return fh
    except Exception:
        return None


def _make_console_handler() -> logging.Handler:
    """
    Create a console handler using either JSON or human formatter.
    """
    ch = logging.StreamHandler()
    ch.setLevel(CONSOLE_LEVEL)
    ch.setFormatter(_JsonFormatter() if JSON_CONSOLE else _human_formatter())
    return ch


# ════════════════════════════════════════════════════════════════════════════
# Public helper
# ════════════════════════════════════════════════════════════════════════════
def get_logger(name: str | None = None) -> logging.Logger:
    """
    Return a configured `logging.Logger`.

    Parameters
    ----------
    name : str | None
        • Explicit logger name, e.g. __name__ from caller.
        • *None* → root project logger "gpt_review".

    Notes
    -----
    Handlers are attached **only to the root** "gpt_review" logger. Child loggers
    are returned without handlers and **propagate** to the root, avoiding duplicate
    console/file outputs across modules.
    """
    root = logging.getLogger(_ROOT_LOGGER_NAME)

    # Configure root once
    if not root.handlers:
        root.setLevel(logging.DEBUG)

        # Determine log directory (resilient)
        preferred_dir = Path(_LOG_DIR_ENV)
        log_dir = _ensure_log_dir(preferred_dir)

        # File handler (if possible) – single shared file for the package
        fh = _make_file_handler(log_dir)
        if fh is not None:
            root.addHandler(fh)

        # Console handler (always attach)
        root.addHandler(_make_console_handler())

        # Root does not propagate to ancestors
        root.propagate = False

        # Startup banner at DEBUG so we don't spam normal console INFO output
        root.debug(
            "Logger initialised | dir=%s | console=%s | rotate=%s | backups=%s | utc=%s | json-console=%s",
            str(log_dir),
            CONSOLE_LEVEL_NAME,
            ROTATE_WHEN,
            BACKUP_COUNT,
            USE_UTC,
            JSON_CONSOLE,
        )

    # Return root or a child that propagates to root
    if name is None or name == _ROOT_LOGGER_NAME:
        return root

    logger = logging.getLogger(name)
    # Ensure child loggers don't attach their own handlers; propagate to root.
    logger.setLevel(logging.DEBUG)
    logger.propagate = True
    return logger


# ════════════════════════════════════════════════════════════════════════════
# CLI demonstration (python -m gpt_review.logger)
# ════════════════════════════════════════════════════════════════════════════
if __name__ == "__main__":  # pragma: no cover
    log = get_logger()
    log.info("Console INFO message.")
    log.debug("Debug message (file handler if available).")
    # Emit an example JSON line if enabled
    if JSON_CONSOLE:
        log.info("JSON console logging is enabled.")
    # Show where logs live
    try:
        resolved = Path(_LOG_DIR_ENV).expanduser().resolve()
    except Exception:
        resolved = Path(_LOG_DIR_ENV)
    print(f"Log directory configured as: {resolved}")
