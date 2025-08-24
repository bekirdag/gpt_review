"""
===============================================================================
Selective staging guarantees for *apply_patch.py*
===============================================================================

Scope
-----
* Deleting a **tracked** file commits only that path (no parent dir sweep).
* Renaming a **tracked** file uses `git mv` (shows up as a rename in history).
* Unrelated working-tree changes remain uncommitted.

The tests operate in isolated temporary Git repos.
"""
from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path

from apply_patch import apply_patch

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")


# -----------------------------------------------------------------------------#
# Git helpers
# -----------------------------------------------------------------------------#
def _git(repo: Path, *args: str, capture: bool = False) -> str:
    res = subprocess.run(
        ["git", "-C", str(repo), *args],
        text=True,
        capture_output=capture,
        check=True,
    )
    return res.stdout if capture else ""


def _init_repo(tmp: Path) -> Path:
    repo = tmp / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "Test")
    # Seed content
    (repo / "tracked.txt").write_text("keep me for now\n", encoding="utf-8")
    (repo / "to-rename.txt").write_text("rename me\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "seed")
    log.info("Initialised repo at %s", repo)
    return repo


def _apply(patch: dict, repo: Path) -> None:
    apply_patch(json.dumps(patch), str(repo))


# -----------------------------------------------------------------------------#
# Tests
# -----------------------------------------------------------------------------#
def test_delete_tracked_is_precisely_staged(tmp_path: Path):
    """
    Deleting a tracked file should stage **only** that path.

    Also ensure unrelated changes remain in the working tree and are not
    silently swept into the deletion commit.
    """
    repo = _init_repo(tmp_path)

    # Create unrelated working-tree noise:
    #  * modified tracked file
    #  * untracked file
    (repo / "tracked.txt").write_text("modified locally\n", encoding="utf-8")
    (repo / "untracked.txt").write_text("new stuff\n", encoding="utf-8")

    # Delete a different tracked file via the SUT
    _apply(
        {"op": "delete", "file": "to-rename.txt", "status": "completed"},
        repo,
    )

    # Inspect the last commit: it must reference only 'to-rename.txt'
    last_names = _git(
        repo, "show", "--name-only", "--pretty=format:", "HEAD", capture=True
    ).strip().splitlines()
    assert last_names == ["to-rename.txt"], f"Unexpected commit paths: {last_names!r}"

    # The unrelated modifications should still be present and uncommitted
    porcelain = _git(repo, "status", "--porcelain", capture=True)
    assert " M tracked.txt" in porcelain or "M  tracked.txt" in porcelain
    assert "?? untracked.txt" in porcelain


def test_tracked_rename_uses_git_mv(tmp_path: Path):
    """
    Renaming a tracked file should be recorded as a proper rename in history.
    """
    repo = _init_repo(tmp_path)

    _apply(
        {
            "op": "rename",
            "file": "to-rename.txt",
            "target": "renamed.txt",
            "status": "in_progress",
        },
        repo,
    )

    # Verify the working tree reflects the rename
    assert not (repo / "to-rename.txt").exists()
    assert (repo / "renamed.txt").exists()

    # Check the last commit shows a rename (Rxxx) entry
    name_status = _git(
        repo, "show", "--name-status", "--pretty=format:", "HEAD", capture=True
    ).strip()
    # It may be e.g. "R100\tto-rename.txt\trenamed.txt"
    assert name_status.startswith("R"), f"Expected rename entry, got: {name_status!r}"
