#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
===============================================================================
Logger import compatibility tests
===============================================================================

Goals
-----
* The **packaged** logger accessor (`from gpt_review.logger import get_logger`)
  and the **shim** accessor (`from logger import get_logger`) must return the
  *same* underlying logger object for a given name (singleton semantics).
* Repeated calls must **not** duplicate handlers (idempotent configuration).
"""
from __future__ import annotations

import logging

from gpt_review.logger import get_logger as pkg_get_logger
from logger import get_logger as shim_get_logger  # legacy shim
from gpt_review import get_logger as pkg_root_get_logger


def test_same_logger_instance_for_same_name() -> None:
    """
    All access paths should return the same Logger object for a given name.
    """
    name = "gpt_review.test.logger"
    a = pkg_get_logger(name)
    b = shim_get_logger(name)
    c = pkg_root_get_logger(name)

    assert isinstance(a, logging.Logger)
    assert a is b is c  # exact same logger instance


def test_idempotent_handlers() -> None:
    """
    Calling get_logger repeatedly must not add duplicate handlers.
    """
    name = "gpt_review.test.idempotent"
    logger = pkg_get_logger(name)
    before = len(logger.handlers)

    # Call via different access paths; handler count must remain stable.
    for _ in range(3):
        _ = pkg_get_logger(name)
        _ = shim_get_logger(name)
        _ = pkg_root_get_logger(name)

    after = len(logger.handlers)
    assert after == before >= 1  # at least one handler, no duplicates added
