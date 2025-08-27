#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
===============================================================================
GPT‑Review ▸ Patch Applier (FULL‑FILE semantics)
===============================================================================

Usage
-----
Given **one validated patch JSON** and a repository path, perform the requested
file operation and commit the change:

    python apply_patch.py '<json-string>'  /path/to/repo
    echo "$json" | python apply_patch.py -  /path/to/repo

Operations
----------
| op      | Required keys                 | Notes                                      |
|---------|-------------------------------|--------------------------------------------|
| create  | file, body | body_b64         | Fails if *file* already exists             |
| update  | file, body | body_b64         | Fails if *file* missing or locally dirty   |
| delete  | file                             | Fails if missing or path is a directory    |
| rename  | file, target                     | Target must not exist                      |
| chmod   | file, mode (644 / 755)           | 3‑ or 4‑digit octal accepted (0755 ok)     |

Safety & Guarantees
-------------------
* **No traversal**: rejects any path escaping repo root (../ or symlink tricks).
* **.git guard**: refuses any operation inside `.git/`.
* **Local changes**: refuses destructive ops if the file differs from HEAD.
* **Full‑file only**: create/update always write full file bodies (no diffs).
* **Atomic writes**: data is written to a temp file then atomically replaced.
* **Precise staging**: only the affected paths are staged/committed.
* **Idempotent**: no‑op commits are skipped; repeated identical updates are ignored.
* **Safe chmod**: only 0644 / 0755 (or 3‑digit forms) are allowed on regular files.

Logging
-------
Uses the packaged rotating logger (`gpt_review.get_logger`). INFO for high‑level
actions; DEBUG for details.
"""
from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Iterable, Optional

from gpt_review import get_logger
from patch_validator import validate_patch  # schema validator (raises on error)

# ─────────────────────────────────────────────────────────────────────────────
# Constants & logger
# ─────────────────────────────────────────────────────────────────────────────
SAFE_MODES = {"644", "755"}  # normalized 3‑digit whitelist
log = get_logger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Git helpers
# ─────────────────────────────────────────────────────────────────────────────
def _git(repo: Path, *args: str, capture: bool = False, check: bool = True) -> str:
    """
    Run a git command inside *repo*. Return stdout if *capture* else "".
    """
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        text=True,
        capture_output=capture,
        check=check,
    )
    return proc.stdout if capture else ""


def _git_ok(repo: Path, *args: str) -> bool:
    """Return True if the git command exits with code 0 (never raises)."""
    return subprocess.run(["git", "-C", str(repo), *args], text=True).returncode == 0


def _has_local_changes(repo: Path, rel_path: str) -> bool:
    """
    True if *rel_path* is modified (staged or unstaged) w.r.t HEAD.
    """
    status = _git(repo, "status", "--porcelain", "--", rel_path, capture=True)
    return bool(status.strip())


def _is_tracked(repo: Path, rel_path: str) -> bool:
    """True if *rel_path* is tracked by Git (present in index)."""
    return _git_ok(repo, "ls-files", "--error-unmatch", "--", rel_path)


def _index_has_changes(repo: Path, paths: Iterable[str] | None = None) -> bool:
    """
    True if there are **staged** changes for the given *paths* (or any if None).
    Uses `git diff --cached --quiet` which exits 0 when no staged changes exist.
    """
    cmd = ["git", "-C", str(repo), "diff", "--cached", "--quiet"]
    path_list = [p for p in (paths or []) if p]
    if path_list:
        cmd.extend(["--", *path_list])
    return subprocess.run(cmd, text=True).returncode != 0


def _stage_exact(repo: Path, *paths: str) -> None:
    """
    Stage **only** the given file paths (no parent‑dir sweeping).

    • For deletions staged via `git rm`, re‑adding a missing path is a no‑op.
    """
    to_add: list[str] = []
    for p in dict.fromkeys(paths):  # de‑dupe while preserving order
        if p and (repo / p).exists():
            to_add.append(p)
    if to_add:
        _git(repo, "add", "--", *to_add)


def _commit(repo: Path, message: str, paths: Iterable[str]) -> None:
    """
    Stage *paths* precisely and commit with *message* restricted to those paths.
    """
    path_list = [p for p in paths if p]
    _stage_exact(repo, *path_list)

    if not _index_has_changes(repo, path_list):
        log.info("No changes detected for commit: %s (skipping)", message)
        return

    _git(repo, "commit", "-m", message, "--", *path_list)
    log.info("Committed: %s", message)

# ─────────────────────────────────────────────────────────────────────────────
# Path & content helpers
# ─────────────────────────────────────────────────────────────────────────────
def _ensure_inside(repo: Path, target: Path) -> None:
    """Reject any path outside *repo* via ValueError."""
    try:
        target.relative_to(repo)
    except ValueError as exc:
        raise ValueError("Patch path escapes repository root") from exc


def _is_under_dot_git(rel: str) -> bool:
    """True if a repo‑relative path refers to `.git` or a descendant."""
    s = rel.strip().lstrip("./")
    return bool(s) and (s == ".git" or s.startswith(".git/") or "/.git/" in s or s.endswith("/.git"))


def _normalize_text(text: str) -> str:
    """Normalize text payloads to LF and ensure a trailing newline."""
    t = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    return t if t.endswith("\n") else t + "\n"


def _same_contents_text(p: Path, new_text: str) -> bool:
    """
    Return True if file *p* is textually identical to *new_text* **after
    normalization** (EOLs and trailing newline). Avoids churny commits.
    """
    if not p.exists():
        return False
    try:
        current = p.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return False
    return _normalize_text(current) == _normalize_text(new_text)


def _same_contents_binary(p: Path, new_b64: str) -> bool:
    """Return True if file *p* already equals the decoded base64 bytes."""
    if not p.exists():
        return False
    try:
        return p.read_bytes() == base64.b64decode(new_b64)
    except Exception:
        return False


def _atomic_write_bytes(dest: Path, data: bytes) -> None:
    """
    Write *data* atomically into *dest* (same‑dir temp + replace). Ensures
    parent directories exist and fsyncs before replace.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=str(dest.parent), delete=False) as tmp:
        tmp.write(data)
        tmp.flush()
        os.fsync(tmp.fileno())
        tmp_path = Path(tmp.name)
    os.replace(tmp_path, dest)


def _write_file(dest: Path, *, body: Optional[str], body_b64: Optional[str]) -> tuple[int, int]:
    """
    Write *body* (text) or *body_b64* (binary) into *dest* atomically.

    Returns (written_bytes, previous_size).
    """
    prev_size = dest.stat().st_size if dest.exists() else 0

    if body_b64 is not None:
        data = base64.b64decode(body_b64)
        _atomic_write_bytes(dest, data)
        log.debug("Wrote binary file %s (%d bytes)", dest, len(data))
        return len(data), prev_size

    # text path
    text = _normalize_text(body or "")
    data = text.encode("utf-8")
    _atomic_write_bytes(dest, data)
    log.debug("Wrote text file %s (%d bytes utf‑8)", dest, len(data))
    return len(data), prev_size


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
        raise PermissionError("Unsafe chmod (allowed: 0644/644 or 0755/755)")
    return normalized

# ─────────────────────────────────────────────────────────────────────────────
# Core apply logic
# ─────────────────────────────────────────────────────────────────────────────
def apply_patch(patch_json: str, repo_path: str) -> None:
    """
    Validate patch payload, perform the operation, and commit precisely.
    """
    # Validate schema first (raises on error)
    validate_patch(patch_json)

    # Parse JSON into dict (supports being called with either JSON string or dict stringified)
    patch = json.loads(patch_json)
    repo = Path(repo_path).resolve()

    if not (repo / ".git").exists():
        raise FileNotFoundError(f"Not a git repo: {repo}")

    rel: str = patch.get("file") or ""
    if not rel:
        raise ValueError("Missing 'file' path in patch payload")
    if _is_under_dot_git(rel):
        raise PermissionError("Refusing to operate inside .git/")

    src = (repo / rel).resolve()
    _ensure_inside(repo, src)

    op = (patch.get("op") or "").strip().lower()
    log.info("Applying op=%s path=%s", op, rel)

    # Guard against accidental overwrite of locally modified files
    if op in {"update", "delete", "rename", "chmod"} and _has_local_changes(repo, rel):
        raise RuntimeError(f"Refusing to {op} '{rel}' – local modifications detected.")

    # ----------------------- create / update -------------------------------
    if op in {"create", "update"}:
        body: Optional[str] = patch.get("body")
        body_b64: Optional[str] = patch.get("body_b64")

        if body is None and body_b64 is None:
            raise ValueError("create/update requires 'body' (text) or 'body_b64' (binary)")

        if op == "create":
            if src.exists():
                raise FileExistsError(src)
        else:  # update
            if not src.exists():
                raise FileNotFoundError(src)
            # No‑op fast‑path
            if body is not None and _same_contents_text(src, body):
                log.info("No content change for %s – skipping update.", rel)
                return
            if body_b64 is not None and _same_contents_binary(src, body_b64):
                log.info("No binary change for %s – skipping update.", rel)
                return

        _write_file(src, body=body, body_b64=body_b64)
        _commit(repo, f"GPT {op}: {rel}", paths=[rel])
        return

    # ---------------------------- delete -----------------------------------
    if op == "delete":
        if not src.exists():
            raise FileNotFoundError(src)
        if src.is_dir():
            raise IsADirectoryError(src)

        if _is_tracked(repo, rel):
            _git(repo, "rm", "-f", "--", rel)  # stages deletion
            _commit(repo, f"GPT delete: {rel}", paths=[rel])
        else:
            src.unlink()
            log.info("Deleted untracked file %s (no commit).", rel)
        return

    # ---------------------------- rename -----------------------------------
    if op == "rename":
        target_rel: str = patch.get("target") or ""
        if not target_rel:
            raise ValueError("rename requires 'target'")
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
            # Accurate rename staging
            _git(repo, "mv", "--", rel, target_rel)
            log.debug("git mv: %s -> %s", rel, target_rel)
            _commit(repo, f"GPT rename: {rel} -> {target_rel}", paths=[rel, target_rel])
        else:
            shutil.move(src, target)
            log.debug("fs move: %s -> %s", rel, target_rel)
            _commit(repo, f"GPT add (rename of untracked): {target_rel}", paths=[target_rel])
        return

    # ---------------------------- chmod ------------------------------------
    if op == "chmod":
        mode_raw: str = patch.get("mode") or ""
        mode = _normalize_mode(mode_raw)
        if not src.exists():
            raise FileNotFoundError(src)
        if not src.is_file():
            raise IsADirectoryError(f"chmod only allowed on regular files: {rel}")

        desired = int(mode, 8)
        current = src.stat().st_mode & 0o777
        if current == desired:
            log.info("Mode for %s already %s – skipping chmod.", rel, mode)
            return

        os.chmod(src, desired)
        _commit(repo, f"GPT chmod {mode}: {rel}", paths=[rel])
        return

    # -----------------------------------------------------------------------
    raise ValueError(f"Unknown op '{op}' encountered.")

# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────
def _cli() -> None:
    """
    Small CLI for manual / scripted invocation.
    """
    if len(sys.argv) != 3:
        sys.exit("Usage: apply_patch.py <json-string | ->  <repo>")

    patch_arg, repo_arg = sys.argv[1:]
    payload = sys.stdin.read() if patch_arg == "-" else patch_arg
    apply_patch(payload, repo_arg)


if __name__ == "__main__":
    _cli()
