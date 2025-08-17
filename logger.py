#!/usr/bin/env python3
"""
===============================================================================
GPT‑Review ▸ Unified Logging Facility
===============================================================================

Purpose
-------
Create one **opinionated**, **centralised** logger configuration so that every
sub‑module shares consistent formatting and rotation behaviour.

Key features
------------
* **Console output** – INFO level by default (override via env).
* **Daily rotating file** – DEBUG level, 7 days retention (both tunable).
* **Idempotent** – repeated calls don't duplicate handlers.
* **Resilient** – if the log directory is not writable, falls back to a
  temporary directory and then to console‑only.
* **Environment overrides**:
    GPT_REVIEW_LOG_DIR   – custom log directory (default: ./logs)
    GPT_REVIEW_LOG_LVL   – console level  (DEBUG / INFO / WARNING / …)
    GPT_REVIEW_LOG_ROT   – rotation interval (midnight, H, M, S, …)
    GPT_REVIEW_LOG_BACK  – number of backup files (default 7)
    GPT_REVIEW_LOG_UTC   – truthy → timestamps in UTC (1/true/yes/on)
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

# ════════════════════════════════════════════════════════════════════════════
# Helpers
# ════════════════════════════════════════════════════════════════════════════
def _is_truthy(val: str | None) -> bool:
    """Return True if *val* represents a truthy setting."""
    if val is None:
        return False
    return val.strip().lower() in {"1", "true", "yes", "on", "y", "t"}

# ════════════════════════════════════════════════════════════════════════════
# Defaults & environment overrides
# ════════════════════════════════════════════════════════════════════════════
_DEFAULT_DIR = Path("logs")
_LOG_DIR_ENV = os.getenv("GPT_REVIEW_LOG_DIR", str(_DEFAULT_DIR))
CONSOLE_LEVEL = os.getenv("GPT_REVIEW_LOG_LVL", "INFO").upper()
ROTATE_WHEN = os.getenv("GPT_REVIEW_LOG_ROT", "midnight")  # see python docs
BACKUP_COUNT = int(os.getenv("GPT_REVIEW_LOG_BACK", "7"))
USE_UTC = _is_truthy(os.getenv("GPT_REVIEW_LOG_UTC"))
JSON_CONSOLE = _is_truthy(os.getenv("GPT_REVIEW_LOG_JSON"))

# ════════════════════════════════════════════════════════════════════════════
# Formatters
# ════════════════════════════════════════════════════════════════════════════
# Human‑friendly formatter
FORMAT = "%(asctime)s | %(name)s | %(process)d | %(levelname)-8s | %(message)s"
DTFMT = "%Y-%m-%d %H:%M:%S"

class _JsonFormatter(logging.Formatter):
    """Minimal JSON formatter (for CI/log scraping)."""

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

def _build_formatter() -> logging.Formatter:
    fmt = logging.Formatter(fmt=FORMAT, datefmt=DTFMT)
    if USE_UTC:
        # type: ignore[attr-defined]  – supported by stdlib
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


def _make_file_handler(log_dir: Path, log_name: str) -> Optional[TimedRotatingFileHandler]:
    """
    Create a rotating file handler for *log_name* inside *log_dir*.

    Returns None if file handler cannot be created (permissions, etc.).
    """
    try:
        file_path = log_dir / f"{log_name}.log"
        fh = TimedRotatingFileHandler(
            filename=file_path,
            when=ROTATE_WHEN,
            interval=1,
            backupCount=BACKUP_COUNT,
            encoding="utf-8",
        )
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(_build_formatter())
        return fh
    except Exception:
        return None


def _make_console_handler() -> logging.Handler:
    """
    Create a console handler using either JSON or human formatter.
    """
    ch = logging.StreamHandler()
    ch.setLevel(CONSOLE_LEVEL)
    ch.setFormatter(_JsonFormatter() if JSON_CONSOLE else _build_formatter())
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
    The function is *idempotent*: handlers are added only once per logger,
    so repeated calls are safe and inexpensive.
    """
    log_name = name or "gpt_review"
    logger = logging.getLogger(log_name)

    # Already configured?
    if logger.handlers:
        return logger

    # Capture everything; handlers will filter.
    logger.setLevel(logging.DEBUG)

    # Determine log directory (resilient)
    preferred_dir = Path(_LOG_DIR_ENV)
    log_dir = _ensure_log_dir(preferred_dir)

    # File handler (if possible)
    fh = _make_file_handler(log_dir, log_name)
    if fh is not None:
        logger.addHandler(fh)

    # Console handler (always attach)
    logger.addHandler(_make_console_handler())

    # Avoid double‑logging via ancestor propagation
    logger.propagate = False

    # Startup banner at DEBUG so we don't spam normal console INFO output
    logger.debug(
        "Logger initialised | dir=%s | console=%s | rotate=%s | backups=%s | utc=%s | json-console=%s",
        str(log_dir),
        CONSOLE_LEVEL,
        ROTATE_WHEN,
        BACKUP_COUNT,
        USE_UTC,
        JSON_CONSOLE,
    )
    return logger


# ════════════════════════════════════════════════════════════════════════════
# CLI demonstration (python logger.py)
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
