"""
===============================================================================
Unit‑tests ▸ Scoped staging (no sibling sweep)
===============================================================================

Goal
----
Verify that applying a patch to *one* file stages/commits **only** that file,
even when a sibling is locally modified. We also cover update/create/delete
operations. Sibling changes must remain unstaged/working after the commit.
"""
from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path

import pytest

from apply_patch import apply_patch

# -----------------------------------------------------------------------------
# Logging for easier CI debugging
# -----------------------------------------------------------------------------
log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------
def _git(repo: Path, *args: str, capture: bool = False) -> str:
    """
    Run *git* in *repo*; optionally capture stdout.
    """
    res = subprocess.run(
        ["git", "-C", str(repo), *args],
        text=True,
        capture_output=capture,
        check=True,
    )
    return res.stdout if capture else ""


def _init_repo(tmp: Path) -> Path:
    """
    Initialise a small Git repo with two tracked siblings: dir/a.txt and dir/b.txt.
    """
    repo = tmp / "scoped"
    repo.mkdir()

    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "Test")

    (repo / "dir").mkdir()
    (repo / "dir/a.txt").write_text("A0\n", encoding="utf-8")
    (repo / "dir/b.txt").write_text("B0\n", encoding="utf-8")
    _git(repo, "add", "--all")
    _git(repo, "commit", "-m", "seed")

    log.info("Repo initialised at %s", repo)
    return repo


def _apply(patch: dict, repo: Path) -> None:
    """
    Convenience wrapper around apply_patch.apply_patch.
    """
    payload = json.dumps(patch)
    apply_patch(payload, str(repo))


def _last_commit_paths(repo: Path) -> list[str]:
    """
    Return the list of files changed in the last commit.
    """
    out = _git(repo, "show", "--name-only", "--pretty=format:", "HEAD", capture=True)
    return [line.strip() for line in out.splitlines() if line.strip()]


# -----------------------------------------------------------------------------
# Tests
# -----------------------------------------------------------------------------
def test_update_does_not_stage_siblings(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)

    # Make a sibling working‑tree modification that must remain unstaged
    (repo / "dir/b.txt").write_text("B1‑local\n", encoding="utf-8")

    # Apply an *update* to a.txt only
    _apply(
        {
            "op": "update",
            "file": "dir/a.txt",
            "body": "A1‑patched\n",
            "status": "in_progress",
        },
        repo,
    )

    # Only a.txt must be part of the new commit
    changed = _last_commit_paths(repo)
    assert changed == ["dir/a.txt"], f"Unexpected paths in commit: {changed}"

    # And b.txt must still be a local modification
    status = _git(repo, "status", "--porcelain", capture=True)
    assert " M dir/b.txt" in status, f"Sibling not left unstaged:\n{status}"
    log.info("Update scoped staging OK; commit paths: %s", changed)


def test_create_does_not_stage_siblings(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)

    # Local modification to b.txt should not be swept into the commit
    (repo / "dir/b.txt").write_text("B2‑local\n", encoding="utf-8")

    # Apply a *create* for a new file c.txt
    _apply(
        {
            "op": "create",
            "file": "dir/c.txt",
            "body": "C0\n",
            "status": "in_progress",
        },
        repo,
    )

    # Only c.txt must be part of the new commit
    changed = _last_commit_paths(repo)
    assert changed == ["dir/c.txt"], f"Unexpected paths in commit: {changed}"

    # b.txt remains modified in the working tree
    status = _git(repo, "status", "--porcelain", capture=True)
    assert " M dir/b.txt" in status, f"Sibling not left unstaged:\n{status}"
    log.info("Create scoped staging OK; commit paths: %s", changed)


@pytest.mark.parametrize(
    "op, mode",
    [
        ("chmod", "755"),
        ("chmod", "644"),
    ],
)
def test_chmod_only_records_mode_change(tmp_path: Path, op: str, mode: str) -> None:
    """
    Ensure chmod records only the target path and does not touch siblings.
    """
    repo = _init_repo(tmp_path)
    (repo / "dir/b.txt").write_text("B3‑local\n", encoding="utf-8")

    _apply(
        {
            "op": op,
            "file": "dir/a.txt",
            "mode": mode,
            "status": "in_progress",
        },
        repo,
    )

    changed = _last_commit_paths(repo)
    assert changed == ["dir/a.txt"], f"Unexpected paths in commit: {changed}"

    status = _git(repo, "status", "--porcelain", capture=True)
    assert " M dir/b.txt" in status
    log.info("Chmod scoped staging OK; commit paths: %s", changed)


def test_delete_does_not_stage_siblings(tmp_path: Path) -> None:
    """
    Deleting a tracked file must not stage modified siblings.
    """
    repo = _init_repo(tmp_path)

    # Modify b.txt locally; then delete a.txt via the tool
    (repo / "dir/b.txt").write_text("B4‑local\n", encoding="utf-8")

    _apply(
        {
            "op": "delete",
            "file": "dir/a.txt",
            "status": "in_progress",
        },
        repo,
    )

    changed = _last_commit_paths(repo)
    assert changed == ["dir/a.txt"], f"Unexpected paths in commit: {changed}"

    status = _git(repo, "status", "--porcelain", capture=True)
    assert " M dir/b.txt" in status
    log.info("Delete scoped staging OK; commit paths: %s", changed)
