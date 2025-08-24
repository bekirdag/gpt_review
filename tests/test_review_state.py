"""
===============================================================================
Unit‑tests for review.py repo state helpers
===============================================================================

Covers:
* _current_commit() on an empty repository (unborn HEAD)
* _current_commit() after an initial commit
"""
from __future__ import annotations

import logging
import re
import subprocess
from pathlib import Path

from review import _current_commit, HEAD_UNBORN

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, text=True)


def _init_repo(tmp: Path, do_commit: bool = False) -> Path:
    repo = tmp / "repo_state"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "Test")
    if do_commit:
        (repo / "a.txt").write_text("x\n")
        _git(repo, "add", "a.txt")
        _git(repo, "commit", "-m", "init")
    return repo


def test_unborn_head_returns_sentinel(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path, do_commit=False)
    sha = _current_commit(repo)
    assert sha == HEAD_UNBORN


def test_after_initial_commit_returns_sha(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path, do_commit=True)
    sha = _current_commit(repo)
    assert re.fullmatch(r"[0-9a-f]{40}", sha) is not None
