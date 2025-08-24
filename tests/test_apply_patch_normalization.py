"""
===============================================================================
Normalization & chmod behavior tests for apply_patch
===============================================================================

Covers:
* Text EOL normalization: CRLF/CR → LF on write
* No-op update when only newline style differs
* Chmod safe-list accepts "0755"/"0644" (octal strings with leading 0)
"""
from __future__ import annotations

import logging
import subprocess
from pathlib import Path

from apply_patch import apply_patch

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")


# -----------------------------------------------------------------------------
# Small git helpers (mirrors style used elsewhere in tests)
# -----------------------------------------------------------------------------
def _git(repo: Path, *args, capture: bool = False) -> str:
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
    log.info("Initialised repo at %s", repo)
    return repo


def _apply(patch: dict, repo: Path) -> None:
    apply_patch(__import__("json").dumps(patch), str(repo))


def _commit_count(repo: Path) -> int:
    return int(_git(repo, "rev-list", "--count", "HEAD", capture=True).strip() or "0")


# -----------------------------------------------------------------------------
# Tests
# -----------------------------------------------------------------------------
def test_crlf_normalized_on_create_and_noop_on_equivalent_update(tmp_path: Path):
    """
    Create with CRLF, ensure file is LF-only with trailing newline.
    Then update with LF-equivalent payload → should be a no-op (no new commit).
    """
    repo = _init_repo(tmp_path)

    # Create with CRLF
    _apply(
        {
            "op": "create",
            "file": "docs/notes.txt",
            "body": "a\r\nb\r\n",
            "status": "in_progress",
        },
        repo,
    )
    # After first operation there should be exactly 1 commit
    assert _commit_count(repo) == 1

    txt = (repo / "docs/notes.txt").read_text(encoding="utf-8")
    # Must be LF-only, single trailing newline
    assert txt == "a\nb\n"

    # Update with LF-equivalent content → no commit should be created
    _apply(
        {
            "op": "update",
            "file": "docs/notes.txt",
            "body": "a\nb\n",
            "status": "in_progress",
        },
        repo,
    )
    assert _commit_count(repo) == 1  # still one commit (no-op update)


def test_chmod_accepts_octal_strings_with_leading_zero(tmp_path: Path):
    """
    Chmod safe-list accepts both '0755' and '0644' (octal strings).
    """
    repo = _init_repo(tmp_path)

    # Create a file
    _apply(
        {
            "op": "create",
            "file": "tool.sh",
            "body": "#!/bin/sh\necho ok\n",
            "status": "in_progress",
        },
        repo,
    )
    assert _commit_count(repo) == 1

    # chmod 0755 (octal with leading zero) → executable
    _apply(
        {"op": "chmod", "file": "tool.sh", "mode": "0755", "status": "in_progress"},
        repo,
    )
    mode = (repo / "tool.sh").stat().st_mode & 0o777
    assert mode == 0o755
    assert _commit_count(repo) == 2

    # chmod 0644 → back to non-executable
    _apply(
        {"op": "chmod", "file": "tool.sh", "mode": "0644", "status": "in_progress"},
        repo,
    )
    mode = (repo / "tool.sh").stat().st_mode & 0o777
    assert mode == 0o644
    assert _commit_count(repo) == 3
