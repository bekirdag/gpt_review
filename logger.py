#!/usr/bin/env python3
"""
Lightweight logging utility for GPT‑Review.

Features
--------
* Console + file output
* Daily rotation (keep 7 days)
"""
import logging
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path


def get_logger(
    log_dir: str | Path = "logs",
    name: str = "gpt_review",
    when: str = "midnight",
    backup_count: int = 7,
) -> logging.Logger:
    """
    Return a singleton `logging.Logger`.

    Log file:  <log_dir>/<name>.log
    Rotates:   daily, keep `backup_count` days
    Level:     INFO (console) / DEBUG (file)
    """
    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    logfile = log_dir / f"{name}.log"

    logger = logging.getLogger(name)
    if logger.handlers:  # already configured
        return logger

    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")

    # file handler (DEBUG, rotates daily)
    fh = TimedRotatingFileHandler(
        logfile, when=when, interval=1, backupCount=backup_count
    )
    fh.setFormatter(fmt)
    fh.setLevel(logging.DEBUG)
    logger.addHandler(fh)

    # console handler (INFO)
    ch = logging.StreamHandler()
    ch.setFormatter(fmt)
    ch.setLevel(logging.INFO)
    logger.addHandler(ch)

    logger.setLevel(logging.DEBUG)
    return logger
