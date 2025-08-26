#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
===============================================================================
CLI smoke tests for entrypoints
===============================================================================

Goals
-----
* Ensure the module entrypoint works:

      python -m gpt_review --version

  This path must **not** import Selenium or open a browser; it should return
  quickly with the package version.

* Ensure the console script is available and shows help:

      gpt-review --help

  We only assert the help banner renders and mentions required args.

These are **fast** smoke checks to catch packaging/entrypoint regressions.
"""
from __future__ import annotations

import logging
import re
import shutil
import subprocess
import sys

import pytest

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")


def _run(cmd: list[str]) -> tuple[int, str]:
    """
    Run *cmd*, returning (returncode, combined stdout+stderr).
    """
    proc = subprocess.run(cmd, capture_output=True, text=True)
    out = (proc.stdout or "") + (proc.stderr or "")
    log.info("Ran: %s\n%s", " ".join(cmd), out.strip())
    return proc.returncode, out


# Accept classic "X.Y.Z" or PEP 440 local/dev segments (e.g., 0.3.0.dev1, 0.3.0+local)
_PEP440ish = re.compile(r"\b\d+\.\d+\.\d+(?:[A-Za-z0-9_.+-]+)?\b")


def test_module_entrypoint_version() -> None:
    """
    `python -m gpt_review --version` should print a version and exit 0.
    """
    code, out = _run([sys.executable, "-m", "gpt_review", "--version"])
    assert code == 0, "Module entrypoint should exit 0 for --version"
    assert _PEP440ish.search(out), f"Unexpected version output: {out!r}"


def test_console_script_help() -> None:
    """
    `gpt-review --help` should render argparse help and exit 0.

    If the console script is not on PATH (e.g. tests run without an editable
    install), the test is skipped rather than failing.
    """
    exe = shutil.which("gpt-review")
    if not exe:
        pytest.skip("console script `gpt-review` not found on PATH")

    code, out = _run([exe, "--help"])
    assert code == 0, "Console script should exit 0 for --help"
    # Basic sanity of the help banner
    assert "gpt-review" in out
    assert "instructions" in out and "repo" in out
