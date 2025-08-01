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
* **Daily rotating file** – DEBUG level, 7 days retention.
* **Idempotent** – multiple imports won’t duplicate handlers.
* **Environment overrides**:
      GPT_REVIEW_LOG_DIR   – custom log directory
      GPT_REVIEW_LOG_LVL   – console level  (DEBUG / INFO / WARNING / …)
      GPT_REVIEW_LOG_ROT   – rotation interval (midnight, H, M, S, …)
      GPT_REVIEW_LOG_BACK  – number of backup files (default 7)

Example
-------
```python
from logger import get_logger
log = get_logger(__name__)
log.info("Everything is awesome!")
```
"""

from __future__ import annotations

import logging
import os
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

# ════════════════════════════════════════════════════════════════════════════
# Defaults & environment overrides
# ════════════════════════════════════════════════════════════════════════════
LOG_DIR = Path(os.getenv("GPT_REVIEW_LOG_DIR", "logs")).expanduser()
LOG_DIR.mkdir(parents=True, exist_ok=True)

CONSOLE_LEVEL = os.getenv("GPT_REVIEW_LOG_LVL", "INFO").upper()
ROTATE_WHEN = os.getenv("GPT_REVIEW_LOG_ROT", "midnight")  # see python docs
BACKUP_COUNT = int(os.getenv("GPT_REVIEW_LOG_BACK", "7"))

# ════════════════════════════════════════════════════════════════════════════
# Formatter
# ════════════════════════════════════════════════════════════════════════════
FORMAT = "%(asctime)s | %(name)s | %(levelname)-8s | %(message)s"
DTFMT = "%Y-%m-%d %H:%M:%S"
FMT = logging.Formatter(fmt=FORMAT, datefmt=DTFMT)

# ════════════════════════════════════════════════════════════════════════════
# Public helper
# ════════════════════════════════════════════════════════════════════════════
def get_logger(name: str | None = None) -> logging.Logger:
    """
    Return a module‑specific `logging.Logger`.

    Parameters
    ----------
    name : str | None
        • Explicit logger name, e.g. `__name__` from caller.
        • *None*  → root project logger `"gpt_review"`.

    The function is *idempotent*: handlers are added only once per logger,
    so repeated calls are safe.
    """
    log_name = name or "gpt_review"
    logger = logging.getLogger(log_name)

    if logger.handlers:  # already configured
        return logger

    logger.setLevel(logging.DEBUG)  # capture everything; handlers filter

    # ── FILE HANDLER ───────────────────────────────────────────────────────
    file_path = LOG_DIR / f"{log_name}.log"
    fh = TimedRotatingFileHandler(
        filename=file_path,
        when=ROTATE_WHEN,
        interval=1,
        backupCount=BACKUP_COUNT,
        encoding="utf-8",
    )
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(FMT)
    logger.addHandler(fh)

    # ── CONSOLE HANDLER ────────────────────────────────────────────────────
    ch = logging.StreamHandler()
    ch.setLevel(CONSOLE_LEVEL)
    ch.setFormatter(FMT)
    logger.addHandler(ch)

    logger.propagate = False  # prevent duplicate messages

    logger.debug(
        "Logger initialised | dir=%s | console=%s | rotate=%s | backups=%s",
        LOG_DIR,
        CONSOLE_LEVEL,
        ROTATE_WHEN,
        BACKUP_COUNT,
    )
    return logger


# ════════════════════════════════════════════════════════════════════════════
# CLI demonstration (python logger.py)
# ════════════════════════════════════════════════════════════════════════════
if __name__ == "__main__":  # pragma: no cover
    log = get_logger()
    log.info("Console INFO message.")
    log.debug("Debug message (only in file unless console DEBUG).")
    print(f"Log file located at: {LOG_DIR.resolve()}")
