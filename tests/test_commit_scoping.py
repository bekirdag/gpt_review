"""
===============================================================================
Regression tests ▸ Commit path‑scoping (no sibling sweep)
===============================================================================

Purpose
-------
Ensure that apply_patch commits are *precisely* scoped to the intended paths,
even when there are unrelated staged changes present in the index.

We cover:
  • update – unrelated staged files must not be included
  • delete – only the deleted path is committed
  • rename – commit includes the rename, not unrelated staged files

Implementation notes
--------------------
We inspect the last commit with:
    git diff-tree --no-commit-id --name-only -r HEAD

This yields the set of paths recorded in the most recent commit. The tests
assert that "b.txt" never appears when only "a.txt" was targeted.
"""
from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path

import pytest

from apply_patch import apply_patch

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------
def _git(repo: Path, *args: str, capture: bool = False) -> str:
    res = subprocess.run(["git", "-C", str(repo), *args], text=True, capture_output=capture, check=True)
    return res.stdout if capture else ""


def _init_repo(tmp: Path) -> Path:
    repo = tmp / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "Test")

    # Baseline files
    (repo / "a.txt").write_text("A0\n")
    (repo / "b.txt").write_text("B0\n")
    _git(repo, "add", "a.txt", "b.txt")
    _git(repo, "commit", "-m", "baseline")
    return repo


def _last_commit_paths(repo: Path) -> set[str]:
    out = _git(repo, "diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD", capture=True)
    return {line.strip() for line in out.splitlines() if line.strip()}


# =============================================================================
# Tests
# =============================================================================
def test_update_does_not_sweep_unrelated_staged(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)

    # Stage unrelated change in b.txt
    (repo / "b.txt").write_text("B1\n")
    _git(repo, "add", "b.txt")

    # Apply update patch for a.txt only
    patch = {"op": "update", "file": "a.txt", "body": "A1", "status": "in_progress"}
    apply_patch(json.dumps(patch), str(repo))

    changed = _last_commit_paths(repo)
    log.info("Changed paths in last commit (update): %s", changed)
    assert "a.txt" in changed
    assert "b.txt" not in changed, "Unrelated staged file was incorrectly swept into the commit"


def test_delete_is_precise(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)

    # Stage unrelated change in b.txt
    (repo / "b.txt").write_text("B1\n")
    _git(repo, "add", "b.txt")

    # Delete a.txt only
    patch = {"op": "delete", "file": "a.txt", "status": "completed"}
    apply_patch(json.dumps(patch), str(repo))

    changed = _last_commit_paths(repo)
    log.info("Changed paths in last commit (delete): %s", changed)
    assert "a.txt" in changed
    assert "b.txt" not in changed


def test_rename_is_precise(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)

    # Stage unrelated change in b.txt
    (repo / "b.txt").write_text("B1\n")
    _git(repo, "add", "b.txt")

    # Rename a.txt -> a2.txt
    patch = {"op": "rename", "file": "a.txt", "target": "a2.txt", "status": "in_progress"}
    apply_patch(json.dumps(patch), str(repo))

    changed = _last_commit_paths(repo)
    log.info("Changed paths in last commit (rename): %s", changed)
    # git diff-tree --name-only shows the *new* path for renames
    assert "a2.txt" in changed
    assert "b.txt" not in changed
