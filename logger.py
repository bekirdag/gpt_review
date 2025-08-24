#!/usr/bin/env python3
"""
===============================================================================
GPT‑Review ▸ Unified Logging Facility
===============================================================================

Purpose
-------
Provide one **central** logging configuration so every module emits consistent
console and file logs without duplicating handlers.

Design
------
• **Root owner model** – only the root project logger "gpt_review" owns
  handlers.  Children (e.g. "gpt_review.review") **propagate** upward and have
  **no handlers** attached.  This prevents double logging.
• **Console handler** – level from env (INFO by default); optional JSON lines.
• **Timed rotating file** – DEBUG level, rotates at midnight (UTC optional),
  7 backups by default; path configurable with env.
• **Idempotent** – safe to call `get_logger()` repeatedly.
• **Resilient** – if the configured log directory is not writable, falls back
  to a temp dir, and finally console‑only.
• **Namespace routing** – callers that pass a **top‑level name** (e.g. "review",
  "apply_patch", "patch_validator") are transparently **prefixed** to become
  "gpt_review.<name>" so their logs route to the project root handlers.
• **Env overrides**
    GPT_REVIEW_LOG_DIR     – log directory (default: ./logs)
    GPT_REVIEW_LOG_LVL     – console level name (DEBUG/INFO/WARNING/…)
    GPT_REVIEW_LOG_ROT     – rotation schedule (default "midnight")
    GPT_REVIEW_LOG_BACK    – number of backups (default 7)
    GPT_REVIEW_LOG_UTC     – truthy → timestamps & rotation in UTC
    GPT_REVIEW_LOG_JSON    – truthy → JSON lines on console
    GPT_REVIEW_NO_FILE_LOG – truthy → disable file logging entirely
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from logging import Logger
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from typing import Optional

# ════════════════════════════════════════════════════════════════════════════
# Environment parsing
# ════════════════════════════════════════════════════════════════════════════
def _is_truthy(val: str | None) -> bool:
    return (val or "").strip().lower() in {"1", "true", "yes", "on", "y", "t"}


def _level_from_env(name: str | None, default: int = logging.INFO) -> int:
    if not name:
        return default
    try:
        return int(name)  # allow numeric levels
    except (ValueError, TypeError):
        pass
    lvl = getattr(logging, str(name).upper(), None)
    return lvl if isinstance(lvl, int) else default


# Defaults & env
_DEFAULT_DIR = Path("logs")
_LOG_DIR_ENV = os.getenv("GPT_REVIEW_LOG_DIR", str(_DEFAULT_DIR))
CONSOLE_LEVEL = _level_from_env(os.getenv("GPT_REVIEW_LOG_LVL"), logging.INFO)
ROTATE_WHEN = os.getenv("GPT_REVIEW_LOG_ROT", "midnight")
BACKUP_COUNT = int(os.getenv("GPT_REVIEW_LOG_BACK", "7"))
USE_UTC = _is_truthy(os.getenv("GPT_REVIEW_LOG_UTC"))
JSON_CONSOLE = _is_truthy(os.getenv("GPT_REVIEW_LOG_JSON"))
NO_FILE_LOG = _is_truthy(os.getenv("GPT_REVIEW_NO_FILE_LOG"))

# ════════════════════════════════════════════════════════════════════════════
# Formatters
# ════════════════════════════════════════════════════════════════════════════
FORMAT = "%(asctime)s | %(name)s | %(process)d | %(levelname)-8s | %(message)s"
DTFMT = "%Y-%m-%d %H:%M:%S"


class _JsonFormatter(logging.Formatter):
    """Minimal JSON formatter (safe for CI log scraping)."""

    def format(self, record: logging.LogRecord) -> str:  # noqa: D401
        ts = (
            time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(record.created))
            if USE_UTC
            else time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(record.created))
        )
        data = {
            "ts": ts,
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
        # type: ignore[attr-defined] – supported by stdlib
        fmt.converter = time.gmtime  # pragma: no cover (behavioural)
    return fmt


# ════════════════════════════════════════════════════════════════════════════
# Directory & handler utilities
# ════════════════════════════════════════════════════════════════════════════
def _ensure_log_dir(preferred: Path) -> Optional[Path]:
    """
    Ensure a writable log directory exists.

    Preference order:
      1) $GPT_REVIEW_LOG_DIR (or ./logs)
      2) $TMPDIR/gpt-review-logs
    Returns a Path or None if no writable location is available.
    """
    # 1) Preferred
    try:
        path = preferred.expanduser().resolve()
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".writable"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
        return path
    except Exception:
        pass
    # 2) Temp fallback
    try:
        tmp = Path(tempfile.gettempdir()) / "gpt-review-logs"
        tmp.mkdir(parents=True, exist_ok=True)
        return tmp
    except Exception:
        return None


def _make_file_handler(log_dir: Path, log_name: str) -> Optional[TimedRotatingFileHandler]:
    """Create a rotating file handler, or return None if it fails."""
    try:
        file_path = log_dir / f"{log_name}.log"
        fh = TimedRotatingFileHandler(
            filename=file_path,
            when=ROTATE_WHEN,
            interval=1,
            backupCount=BACKUP_COUNT,
            encoding="utf-8",
            delay=True,          # do not create the file until first log message
            utc=USE_UTC,         # rotate on UTC boundaries when requested
        )
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(_human_formatter())
        return fh
    except Exception:
        return None


def _make_console_handler() -> logging.Handler:
    """Create a console handler using either JSON or human formatter."""
    ch = logging.StreamHandler()
    ch.setLevel(CONSOLE_LEVEL)
    ch.setFormatter(_JsonFormatter() if JSON_CONSOLE else _human_formatter())
    return ch


# ════════════════════════════════════════════════════════════════════════════
# Root‑logger configuration (single owner of handlers)
# ════════════════════════════════════════════════════════════════════════════
_ROOT_CONFIGURED = False  # module‑local guard


def _configure_root_once() -> Logger:
    """
    Configure and return the **root project logger** "gpt_review".
    Idempotent – subsequent calls reuse existing handlers.
    """
    global _ROOT_CONFIGURED

    root = logging.getLogger("gpt_review")

    if _ROOT_CONFIGURED:
        return root

    # Capture everything from children; handlers filter.
    root.setLevel(logging.DEBUG)

    # Console handler (always)
    root.addHandler(_make_console_handler())

    # File handler (optional)
    if not NO_FILE_LOG:
        log_dir = _ensure_log_dir(Path(_LOG_DIR_ENV))
        if log_dir is not None:
            fh = _make_file_handler(log_dir, "gpt_review")
            if fh is not None:
                root.addHandler(fh)

    # Root logger should not propagate to the Python root
    root.propagate = False

    root.debug(
        "Logger initialised | console=%s | rotate=%s | backups=%s | utc=%s | json-console=%s | file-log=%s | dir=%s",
        logging.getLevelName(CONSOLE_LEVEL),
        ROTATE_WHEN,
        BACKUP_COUNT,
        USE_UTC,
        JSON_CONSOLE,
        not NO_FILE_LOG,
        _LOG_DIR_ENV,
    )
    _ROOT_CONFIGURED = True
    return root


# ════════════════════════════════════════════════════════════════════════════
# Public helper
# ════════════════════════════════════════════════════════════════════════════
def get_logger(name: str | None = None) -> Logger:
    """
    Return a logger configured with GPT‑Review's handlers & formatting.

    Behaviour
    ---------
    • If *name* is None or "gpt_review", you get the root project logger with
      handlers attached.
    • If *name* is something like "gpt_review.review", you get a *child* logger
      with **no handlers** and `propagate=True`, so it uses the root handlers.
    • If *name* is a top‑level module (e.g. "review", "apply_patch",
      "patch_validator"), it is **prefixed** to "gpt_review.<name>" so it becomes
      a child of the project root logger.
    """
    root = _configure_root_once()

    if not name or name == "gpt_review":
        return root

    # Route non‑namespaced modules into our hierarchy
    if not name.startswith("gpt_review."):
        name = f"gpt_review.{name}"

    log = logging.getLogger(name)
    # Ensure children do not attach their own handlers
    log.handlers = []
    log.setLevel(logging.NOTSET)  # delegate level filtering to handlers
    log.propagate = True
    return log


# ════════════════════════════════════════════════════════════════════════════
# CLI demonstration (python logger.py)
# ════════════════════════════════════════════════════════════════════════════
if __name__ == "__main__":  # pragma: no cover
    lg = get_logger()  # root
    lg.info("Console INFO message.")
    lg.debug("Debug message (file handler if enabled).")

    child = get_logger("review")  # top‑level name → routed to gpt_review.review
    child.info("Top‑level 'review' routed under 'gpt_review' hierarchy.")

    namespaced = get_logger("gpt_review.demo.child")
    namespaced.info("Namespaced child uses root handlers (no duplication).")

    # Show where logs live (best‑effort)
    path = Path(_LOG_DIR_ENV)
    try:
        resolved = path.expanduser().resolve()
    except Exception:
        resolved = path
    lg.info("Log directory configured as: %s", resolved)
