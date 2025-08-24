"""
===============================================================================
Unit‑tests for *apply_patch.py*
===============================================================================

Goals
-----
* Exercise every supported patch operation:
    • create / update               (text file)
    • delete
    • rename
    • chmod                         (safe + unsafe)
    • binary create                 (body_b64)
* Verify repository invariants:
    • commits created as expected
    • path‑traversal attempts blocked
    • local‑change protection works
    • **no writes inside .git/** (high‑impact safety)
    • **path‑scoped staging**: unrelated files are not swept into commits

All tests run inside a **temporary Git repository** created via the
tmp_path fixture (pytest).

The module prints INFO‑level messages so failures are easier to debug in
CI job logs.
"""
from __future__ import annotations

import base64
import json
import logging
import subprocess
from pathlib import Path

import pytest

# System‑under‑test
from apply_patch import apply_patch

# -----------------------------------------------------------------------------
# Logging – helpful when Git commands fail in CI
# -----------------------------------------------------------------------------
log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")


# =============================================================================
# Helpers
# =============================================================================
def _git(repo: Path, *args, capture: bool = False) -> str:
    """
    Run *git* in *repo*, optionally capturing stdout.
    """
    res = subprocess.run(
        ["git", "-C", str(repo), *args],
        text=True,
        capture_output=capture,
        check=True,
    )
    return res.stdout if capture else ""


def _init_repo(tmp: Path, initial_file: bool = False) -> Path:
    """
    Initialise a barebones Git repository.

    * Writes 'baseline.txt' & commits it if *initial_file* is True.
    """
    repo = tmp / "repo"
    repo.mkdir()

    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "Test")

    if initial_file:
        (repo / "baseline.txt").write_text("baseline\n")
        _git(repo, "add", "baseline.txt")
        _git(repo, "commit", "-m", "baseline")

    log.info("Initialised repo at %s", repo)
    return repo


def _apply(patch: dict, repo: Path) -> None:
    """
    Convenience wrapper around apply_patch.apply_patch.

    Accepts a Python dict, serialises to JSON, and invokes the SUT.
    """
    apply_patch(json.dumps(patch), str(repo))


def _commit_count(repo: Path) -> int:
    """
    Return the number of commits in *repo*.
    """
    return int(_git(repo, "rev-list", "--count", "HEAD", capture=True).strip())


def _last_commit_paths(repo: Path) -> list[str]:
    """
    Return the paths modified in the last commit (name-only).
    """
    out = _git(repo, "diff", "--name-only", "HEAD~1..HEAD", capture=True)
    return [ln.strip() for ln in out.splitlines() if ln.strip()]


# =============================================================================
# Tests
# =============================================================================
def test_full_lifecycle(tmp_path: Path):
    """
    Happy‑path: create → update → chmod → rename → delete.
    """
    repo = _init_repo(tmp_path)

    # --- create ------------------------------------------------------------
    _apply(
        {
            "op": "create",
            "file": "docs/hello.txt",
            "body": "hi",
            "status": "in_progress",
        },
        repo,
    )
    assert (repo / "docs/hello.txt").read_text() == "hi\n"

    # --- update ------------------------------------------------------------
    _apply(
        {
            "op": "update",
            "file": "docs/hello.txt",
            "body": "world",
            "status": "in_progress",
        },
        repo,
    )
    assert (repo / "docs/hello.txt").read_text() == "world\n"

    # --- chmod -------------------------------------------------------------
    _apply(
        {
            "op": "chmod",
            "file": "docs/hello.txt",
            "mode": "755",
            "status": "in_progress",
        },
        repo,
    )
    assert (repo / "docs/hello.txt").stat().st_mode & 0o777 == 0o755

    # --- rename ------------------------------------------------------------
    _apply(
        {
            "op": "rename",
            "file": "docs/hello.txt",
            "target": "docs/greetings.txt",
            "status": "in_progress",
        },
        repo,
    )
    assert not (repo / "docs/hello.txt").exists()
    assert (repo / "docs/greetings.txt").exists()

    # --- delete ------------------------------------------------------------
    _apply(
        {
            "op": "delete",
            "file": "docs/greetings.txt",
            "status": "completed",
        },
        repo,
    )
    assert not (repo / "docs/greetings.txt").exists()

    # five commits = 5 operations
    assert _commit_count(repo) == 5
    log.info("Full lifecycle test passed.")


def test_binary_create(tmp_path: Path):
    """
    Create a binary PNG header via body_b64.
    """
    repo = _init_repo(tmp_path)
    png_header = b"\x89PNG\r\n\x1a\n\0\0\0\rIHDR"

    _apply(
        {
            "op": "create",
            "file": "assets/logo.png",
            "body_b64": base64.b64encode(png_header).decode(),
            "status": "completed",
        },
        repo,
    )
    data = (repo / "assets/logo.png").read_bytes()
    assert data.startswith(b"\x89PNG")
    log.info("Binary create test passed.")


def test_refuse_local_overwrite(tmp_path: Path):
    """
    Local modification protection: update should fail when file is dirty.
    """
    repo = _init_repo(tmp_path, initial_file=True)

    # user edits baseline.txt without committing
    (repo / "baseline.txt").write_text("local edit\n")

    patch = {
        "op": "update",
        "file": "baseline.txt",
        "body": "gpt\n",
        "status": "in_progress",
    }
    with pytest.raises(RuntimeError):
        _apply(patch, repo)

    log.info("Local overwrite protection test passed.")


def test_unsafe_chmod_rejected(tmp_path: Path):
    """
    chmod with mode 777 must raise PermissionError.

    NOTE:
    -----
    We create the file **via the SUT** (create op) to avoid double‑create
    conflicts. Previously the test wrote the file *and* sent a create patch,
    which would rightfully fail before chmod was exercised.
    """
    repo = _init_repo(tmp_path)

    # Create the file through apply_patch (SUT)
    _apply(
        {
            "op": "create",
            "file": "tool.sh",
            "body": "#!/bin/sh\necho ok\n",
            "status": "in_progress",
        },
        repo,
    )
    mode_before = (repo / "tool.sh").stat().st_mode & 0o777

    # Unsafe chmod should be rejected by SAFE_MODES check
    bad_patch = {
        "op": "chmod",
        "file": "tool.sh",
        "mode": "777",
        "status": "in_progress",
    }
    with pytest.raises(PermissionError):
        _apply(bad_patch, repo)

    # Sanity: mode must be unchanged after the failed chmod
    mode_after = (repo / "tool.sh").stat().st_mode & 0o777
    assert mode_after == mode_before
    log.info("Unsafe chmod rejection test passed.")


def test_path_traversal_blocked(tmp_path: Path):
    """
    Attempt to create a file outside the repo; must raise ValueError.
    """
    repo = _init_repo(tmp_path)
    malicious = {
        "op": "create",
        "file": "../escape.txt",
        "body": "hack",
        "status": "in_progress",
    }
    with pytest.raises(ValueError):
        _apply(malicious, repo)

    log.info("Path traversal protection test passed.")


# =============================================================================
# New high‑impact safety tests: **block any writes inside .git/**
# =============================================================================
def test_reject_create_inside_dot_git(tmp_path: Path):
    """
    Creating a file inside `.git/` must be rejected with PermissionError.
    """
    repo = _init_repo(tmp_path)
    bad = {
        "op": "create",
        "file": ".git/hook.sh",
        "body": "#!/bin/sh\necho hacked\n",
        "status": "in_progress",
    }
    with pytest.raises(PermissionError):
        _apply(bad, repo)
    # Nothing under working tree should have changed
    assert not (repo / "hook.sh").exists()
    log.info("Create inside .git/ correctly rejected.")


def test_reject_update_inside_dot_git(tmp_path: Path):
    """
    Updating a file under `.git/` (e.g., config) must be rejected.
    """
    repo = _init_repo(tmp_path)
    bad = {
        "op": "update",
        "file": ".git/config",
        "body": "[core]\n\teditor = vim\n",
        "status": "in_progress",
    }
    with pytest.raises(PermissionError):
        _apply(bad, repo)
    log.info("Update inside .git/ correctly rejected.")


def test_reject_rename_target_into_dot_git(tmp_path: Path):
    """
    Renaming a normal file **into** `.git/` must be rejected.
    """
    repo = _init_repo(tmp_path)

    # Create a safe file first (through the SUT) so the rename source exists
    _apply(
        {"op": "create", "file": "safe.txt", "body": "ok", "status": "in_progress"},
        repo,
    )
    assert (repo / "safe.txt").exists()

    bad = {
        "op": "rename",
        "file": "safe.txt",
        "target": ".git/evil.txt",
        "status": "in_progress",
    }
    with pytest.raises(PermissionError):
        _apply(bad, repo)

    # Ensure source remains intact after rejection
    assert (repo / "safe.txt").exists()
    assert not (repo / ".git/evil.txt").exists()
    log.info("Rename target into .git/ correctly rejected.")


# =============================================================================
# New high‑impact correctness test: **path‑scoped staging**
# =============================================================================
def test_staging_is_path_scoped(tmp_path: Path):
    """
    When creating a file, any unrelated modified files must **not** be pulled
    into the commit. This guards against parent‑dir 'git add -A' staging.
    """
    repo = _init_repo(tmp_path, initial_file=True)

    # Make an unrelated local change (unstaged)
    (repo / "unrelated.txt").write_text("dirty\n")

    # Apply a create patch for a different path
    _apply(
        {"op": "create", "file": "docs/only_me.txt", "body": "data", "status": "in_progress"},
        repo,
    )

    changed = set(_last_commit_paths(repo))
    assert changed == {"docs/only_me.txt"}, f"Unexpected paths in commit: {changed}"

    # The unrelated change remains untracked/unstaged
    status = _git(repo, "status", "--porcelain", capture=True)
    assert "unrelated.txt" in status
    log.info("Path‑scoped staging verified: no collateral files committed.")
