#!/usr/bin/env python3
"""
===============================================================================
GPT‑Review ▸ Patch applier
===============================================================================

Given **one validated patch JSON string** and a repository path, perform the
requested file operation and commit the change:

    python apply_patch.py '<json-string>'  /path/to/repo
    echo "$json" | python apply_patch.py -  /path/to/repo

Supported operations
--------------------
| op      | Required keys                 | Notes                                   |
|---------|-------------------------------|-----------------------------------------|
| create  | file, body | body_b64         | Fails if *file* already exists          |
| update  | file, body | body_b64         | Fails if *file* missing or locally dirty|
| delete  | file                         | Fails if missing or directory           |
| rename  | file, target                 | Target must not exist                   |
| chmod   | file, mode (644 / 755)       | Safe‑list prevents 777 etc.             |

Safety nets
-----------
* **Path traversal** – rejects any path escaping repo root (../ tricks).
* **Local modifications** – refuses destructive ops when file differs from
  *HEAD* (staged *or* unstaged).
* **Safe chmod** – only 644 or 755 allowed (configurable).
* **No‑op commits** – skips commit when there is nothing to stage (idempotent).

Logging
-------
Uses the project‑wide daily rotating logger (logger.get_logger).  Each
high‑level step logs an INFO banner; low‑level details at DEBUG.

"""
from __future__ import annotations

import base64
import json
import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Iterable, Optional

from patch_validator import validate_patch
from logger import get_logger

# -----------------------------------------------------------------------------
# Constants & logger
# -----------------------------------------------------------------------------
SAFE_MODES = {"644", "755"}
log = get_logger(__name__)

# -----------------------------------------------------------------------------
# Git helpers
# -----------------------------------------------------------------------------
def _git(repo: Path, *args: str, capture: bool = False, check: bool = True) -> str:
    """
    Run a git command inside *repo*.

    Returns stdout if *capture* = True, else empty string.
    """
    res = subprocess.run(
        ["git", "-C", str(repo), *args],
        text=True,
        capture_output=capture,
        check=check,
    )
    return res.stdout if capture else ""


def _git_ok(repo: Path, *args: str) -> bool:
    """Return True if the git command exits with code 0 (never raises)."""
    res = subprocess.run(["git", "-C", str(repo), *args], text=True)
    return res.returncode == 0


def _has_local_changes(repo: Path, rel_path: str) -> bool:
    """
    True if *rel_path* is modified (staged or unstaged) w.r.t HEAD.
    """
    status = _git(repo, "status", "--porcelain", "--", rel_path, capture=True)
    return bool(status.strip())


def _is_tracked(repo: Path, rel_path: str) -> bool:
    """
    True if *rel_path* is tracked by Git (present in index).
    """
    return _git_ok(repo, "ls-files", "--error-unmatch", "--", rel_path)


def _index_has_changes(repo: Path) -> bool:
    """
    True if there are staged changes in the index (pending commit).
    """
    # `git diff --cached --quiet` → exit 0 when no staged changes.
    res = subprocess.run(
        ["git", "-C", str(repo), "diff", "--cached", "--quiet"],
        text=True,
    )
    return res.returncode != 0


def _stage(repo: Path, *paths: str) -> None:
    """
    Stage only the given *paths* (or their parent dirs for deletions).
    Uses `git add` to avoid picking up unrelated local changes.
    """
    # Filter duplicates and empties
    unique: list[str] = [p for p in dict.fromkeys(paths) if p]
    for p in unique:
        # For deletions, the exact file path may no longer exist. `git add -A`
        # with the parent directory is the reliable way to capture the removal
        # without touching unrelated parts of the tree.
        parent = str(Path(p).parent) or "."
        # Use `-A` so removals are also noticed under that parent.
        _git(repo, "add", "-A", "--", parent)


def _commit(repo: Path, message: str, paths: Iterable[str]) -> None:
    """
    Stage *paths* and commit with *message* if there is anything to commit.
    """
    _stage(repo, *paths)

    if not _index_has_changes(repo):
        log.info("No changes detected for commit: %s (skipping)", message)
        return

    _git(repo, "commit", "-m", message)
    log.info("Committed: %s", message)


# -----------------------------------------------------------------------------
# Path & content helpers
# -----------------------------------------------------------------------------
def _ensure_inside(repo: Path, target: Path) -> None:
    """
    Reject paths outside *repo* via ValueError.
    """
    try:
        target.relative_to(repo)
    except ValueError as exc:
        raise ValueError("Patch path escapes repository root") from exc


def _normalize_text(text: str) -> str:
    """Ensure trailing newline (POSIX)."""
    return text if text.endswith("\n") else text + "\n"


def _write_file(p: Path, body: Optional[str], body_b64: Optional[str]) -> tuple[int, int]:
    """
    Write *body* (text) or *body_b64* (binary) into *p*, ensuring parent dirs.

    Returns
    -------
    (written_bytes, previous_size)
    """
    p.parent.mkdir(parents=True, exist_ok=True)
    prev_size = p.stat().st_size if p.exists() else 0

    if body_b64 is not None:
        data = base64.b64decode(body_b64)
        p.write_bytes(data)
        log.debug("Wrote binary file %s (%d bytes)", p, len(data))
        return len(data), prev_size

    # text
    text = _normalize_text(body or "")
    p.write_text(text, encoding="utf-8")
    log.debug("Wrote text file %s (%d chars)", p, len(text))
    return len(text.encode("utf-8")), prev_size


def _same_contents_text(p: Path, new_text: str) -> bool:
    """Return True if file *p* already equals *new_text* (after normalization)."""
    if not p.exists():
        return False
    try:
        current = p.read_text(encoding="utf-8")
        return current == _normalize_text(new_text)
    except UnicodeDecodeError:
        return False


def _same_contents_binary(p: Path, new_b64: str) -> bool:
    """Return True if file *p* already equals the decoded base64 bytes."""
    if not p.exists():
        return False
    try:
        current = p.read_bytes()
        return current == base64.b64decode(new_b64)
    except Exception:
        return False


# -----------------------------------------------------------------------------
# Core apply logic
# -----------------------------------------------------------------------------
def apply_patch(patch_json: str, repo_path: str) -> None:
    """
    Main entry – validate, perform operation, commit.
    """
    patch = validate_patch(patch_json)  # raises jsonschema.ValidationError
    repo = Path(repo_path).resolve()

    if not (repo / ".git").exists():
        raise FileNotFoundError(f"Not a git repo: {repo}")

    rel = patch.get("file", "")
    src = (repo / rel).resolve()
    _ensure_inside(repo, src)
    op = patch["op"]
    log.info("Applying %s → %s", op, rel)

    # Guard against accidental overwrite of locally modified files
    if op in {"update", "delete", "rename", "chmod"} and _has_local_changes(repo, rel):
        raise RuntimeError(
            f"Refusing to {op} '{rel}' – local modifications detected."
        )

    # ----------------------- create / update -------------------------------
    if op in {"create", "update"}:
        body: Optional[str] = patch.get("body")
        body_b64: Optional[str] = patch.get("body_b64")

        if op == "create":
            if src.exists():
                raise FileExistsError(src)
        else:  # update
            if not src.exists():
                raise FileNotFoundError(src)

            # No‑op fast‑path: if contents are identical, skip writing + commit
            if body is not None and _same_contents_text(src, body):
                log.info("No content change for %s – skipping update.", rel)
                return
            if body_b64 is not None and _same_contents_binary(src, body_b64):
                log.info("No binary change for %s – skipping update.", rel)
                return

        # Write and commit (stage only the target path)
        _write_file(src, body, body_b64)
        _commit(repo, f"GPT {op}: {rel}", paths=[rel])
        return

    # ---------------------------- delete -----------------------------------
    if op == "delete":
        if not src.exists():
            raise FileNotFoundError(src)
        if src.is_dir():
            raise IsADirectoryError(src)

        # Remove from working tree, then stage deletion precisely.
        src.unlink()
        # Use git rm --cached only if path still tracked; otherwise `git add -A`
        # on parent will notice the removal. Prefer an exact rm to avoid
        # sweeping unrelated paths.
        if _is_tracked(repo, rel):
            _git(repo, "rm", "--cached", "--force", "--", rel)
        _commit(repo, f"GPT delete: {rel}", paths=[rel])
        return

    # ---------------------------- rename -----------------------------------
    if op == "rename":
        target_rel = patch["target"]
        target = (repo / target_rel).resolve()
        _ensure_inside(repo, target)

        if not src.exists():
            raise FileNotFoundError(src)
        if target.exists():
            raise FileExistsError(target)

        target.parent.mkdir(parents=True, exist_ok=True)

        # Prefer `git mv` when the source is tracked to stage the rename cleanly.
        if _is_tracked(repo, rel):
            _git(repo, "mv", rel, target_rel)
            log.debug("Used git mv for rename: %s -> %s", rel, target_rel)
        else:
            # Fallback to filesystem move + stage both sides (parent dirs).
            shutil.move(src, target)
            log.debug("Filesystem move performed: %s -> %s", rel, target_rel)

        _commit(repo, f"GPT rename: {rel} -> {target_rel}", paths=[rel, target_rel])
        return

    # ---------------------------- chmod ------------------------------------
    if op == "chmod":
        mode = patch["mode"]
        if mode not in SAFE_MODES:
            raise PermissionError(f"Unsafe chmod mode {mode} (allowed: {SAFE_MODES})")
        if not src.exists():
            raise FileNotFoundError(src)

        desired = int(mode, 8)
        current = src.stat().st_mode & 0o777
        if current == desired:
            log.info("Mode for %s already %s – skipping chmod.", rel, mode)
            return

        os.chmod(src, desired)
        # Stage the path to ensure mode change is recorded (git tracks mode bits)
        _commit(repo, f"GPT chmod {mode}: {rel}", paths=[rel])
        return

    # -----------------------------------------------------------------------
    raise ValueError(f"Unknown op '{op}' encountered.")


# =============================================================================
# CLI wrapper
# =============================================================================
def _cli() -> None:
    """
    Small CLI for manual / scripted invocation.
    """
    if len(sys.argv) != 3:
        sys.exit("Usage: apply_patch.py <json-string | ->  <repo>")

    patch_arg, repo_arg = sys.argv[1:]
    json_payload = sys.stdin.read() if patch_arg == "-" else patch_arg
    apply_patch(json_payload, repo_arg)


if __name__ == "__main__":
    _cli()
