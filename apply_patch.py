#!/usr/bin/env python3
# -*- coding: utf-8 -*-
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
| chmod   | file, mode (644 / 755)       | Accepts 3‑ or 4‑digit octal (0755 ok)   |

Safety nets
-----------
* **Path traversal** – rejects any path escaping repo root (../ tricks).
* **Local modifications** – refuses destructive ops when file differs from HEAD.
* **Safe chmod** – only 0644 or 0755 allowed (accept both 3/4‑digit forms).
* **Precise staging** – stage only the affected path(s); never parent dirs.
* **No‑op commits** – skip commit when there is nothing to stage (idempotent).
* **No writes under `.git/`** – any attempt to read/write/move into `.git/` is rejected.

Logging
-------
Uses the project‑wide daily rotating logger (logger.get_logger). Each high‑level
step logs an INFO banner; low‑level details log at DEBUG.
"""
from __future__ import annotations

import base64
import json
import os
import re
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
SAFE_MODES = {"644", "755"}  # normalized (no leading zero) whitelist
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


def _index_has_changes(repo: Path, paths: Iterable[str] | None = None) -> bool:
    """
    True if there are **staged** changes for the given *paths* (or any, if None).
    Uses `git diff --cached --quiet` which exits 0 when no staged changes exist.
    """
    cmd = ["git", "-C", str(repo), "diff", "--cached", "--quiet"]
    path_list = [p for p in (paths or []) if p]
    if path_list:
        cmd.extend(["--", *path_list])
    res = subprocess.run(cmd, text=True)
    return res.returncode != 0


def _stage_exact(repo: Path, *paths: str) -> None:
    """
    Stage **only** the given file paths (no parent‑dir sweeping, no -A).

    Notes
    -----
    • For deletions staged via `git rm`, re‑adding a missing path is a no‑op.
    • We skip missing paths here to avoid accidental adds of non-existent files.
    """
    to_add: list[str] = []
    for p in dict.fromkeys(paths):  # de‑dupe while preserving order
        if not p:
            continue
        if (repo / p).exists():
            to_add.append(p)
    if to_add:
        _git(repo, "add", "--", *to_add)


def _commit(repo: Path, message: str, paths: Iterable[str]) -> None:
    """
    Stage *paths* precisely and commit with *message* **restricted** to those paths.
    """
    path_list = [p for p in paths if p]
    _stage_exact(repo, *path_list)

    if not _index_has_changes(repo, path_list):
        log.info("No changes detected for commit: %s (skipping)", message)
        return

    # Restrict commit to the exact pathspecs so unrelated staged changes never bleed in.
    _git(repo, "commit", "-m", message, "--", *path_list)
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


def _is_under_dot_git(rel: str) -> bool:
    """
    Return True if a repo‑relative POSIX path refers to `.git` or a descendant.
    """
    s = rel.strip().lstrip("./")
    if not s:
        return False
    return (
        s == ".git"
        or s.startswith(".git/")
        or "/.git/" in s
        or s.endswith("/.git")
    )


def _normalize_text(text: str) -> str:
    """
    Normalize text payloads:

    * Convert CRLF/CR → LF
    * Ensure a trailing newline (POSIX)
    """
    t = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    return t if t.endswith("\n") else t + "\n"


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


def _normalize_mode(mode: str) -> str:
    """
    Accept both 3‑digit and 4‑digit octal strings and normalize to 3‑digit.

    Examples
    --------
    "0755" → "755", "644" → "644"

    Raises
    ------
    PermissionError if the normalized mode is not in SAFE_MODES.
    """
    s = (mode or "").strip()
    if not re.fullmatch(r"[0-7]{3,4}", s):
        raise PermissionError(f"Invalid chmod mode {mode!r} (must be octal)")

    normalized = s.lstrip("0") or "0"
    if normalized not in SAFE_MODES:
        raise PermissionError(
            f"Unsafe chmod mode {mode!r} (allowed: 0644/644 or 0755/755)"
        )
    return normalized


# -----------------------------------------------------------------------------
# Core apply logic
# -----------------------------------------------------------------------------
def apply_patch(patch_json: str, repo_path: str) -> None:
    """
    Main entry – validate, perform operation, commit.
    """
    patch = validate_patch(patch_json)  # schema-level validation; raises on error
    repo = Path(repo_path).resolve()

    if not (repo / ".git").exists():
        raise FileNotFoundError(f"Not a git repo: {repo}")

    rel = patch.get("file", "")
    if _is_under_dot_git(rel):
        raise PermissionError("Refusing to operate inside .git/")

    src = (repo / rel).resolve()
    _ensure_inside(repo, src)

    op = patch["op"]
    log.info("Applying %s → %s", op, rel)

    # Guard against accidental overwrite of locally modified files
    if op in {"update", "delete", "rename", "chmod"} and _has_local_changes(repo, rel):
        raise RuntimeError(f"Refusing to {op} '{rel}' – local modifications detected.")

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

        if _is_tracked(repo, rel):
            # Precise staging of deletion (removes from index and working tree)
            _git(repo, "rm", "-f", "--", rel)
            _commit(repo, f"GPT delete: {rel}", paths=[rel])
        else:
            # Untracked file – remove from working tree only, no commit produced.
            src.unlink()
            log.info("Deleted untracked file %s (no commit needed).", rel)
        return

    # ---------------------------- rename -----------------------------------
    if op == "rename":
        target_rel = patch["target"]
        if _is_under_dot_git(target_rel):
            raise PermissionError("Refusing to move a path into .git/")

        target = (repo / target_rel).resolve()
        _ensure_inside(repo, target)

        if not src.exists():
            raise FileNotFoundError(src)
        if target.exists():
            raise FileExistsError(target)

        target.parent.mkdir(parents=True, exist_ok=True)

        if _is_tracked(repo, rel):
            # Use git mv for accurate rename staging
            _git(repo, "mv", "--", rel, target_rel)
            log.debug("Used git mv for rename: %s -> %s", rel, target_rel)
            # Both sides are already staged by git mv; commit them precisely.
            _commit(repo, f"GPT rename: {rel} -> {target_rel}", paths=[rel, target_rel])
        else:
            # Filesystem move, then stage the new path only (old path was untracked)
            shutil.move(src, target)
            log.debug("Filesystem move performed: %s -> %s", rel, target_rel)
            _commit(repo, f"GPT add (rename of untracked): {target_rel}", paths=[target_rel])
        return

    # ---------------------------- chmod ------------------------------------
    if op == "chmod":
        mode_raw = patch["mode"]
        mode = _normalize_mode(mode_raw)  # accept "0755" or "755"
        if not src.exists():
            raise FileNotFoundError(src)

        desired = int(mode, 8)
        current = src.stat().st_mode & 0o777
        if current == desired:
            log.info("Mode for %s already %s – skipping chmod.", rel, mode)
            return

        os.chmod(src, desired)
        # Stage and commit only this path so the mode bit change is recorded.
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
