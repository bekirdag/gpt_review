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
|---------|------------------------------|-----------------------------------------|
| create  | file, body \| body_b64       | Fails if *file* already exists          |
| update  | file, body \| body_b64       | Fails if *file* missing or locally dirty|
| delete  | file                         | Fails if missing or directory           |
| rename  | file, target                 | Target must not exist                   |
| chmod   | file, mode (644 / 755)       | Safe‑list prevents 777 etc.             |

Safety nets
-----------
* **Path traversal** – rejects any path escaping repo root (`../` tricks).
* **Local modifications** – refuses destructive ops when file differs from
  *HEAD* (staged *or* unstaged).
* **Safe chmod** – only `644` or `755` allowed (configurable).

Logging
-------
Uses the project‑wide daily rotating logger (`logger.get_logger`).  Each
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
def _git(repo: Path, *args: str, capture: bool = False) -> str:
    """
    Run a git command inside *repo*.  Raises if command fails.

    Returns stdout if *capture* = True, else empty string.
    """
    res = subprocess.run(
        ["git", "-C", str(repo), *args],
        text=True,
        capture_output=capture,
        check=True,
    )
    return res.stdout if capture else ""


def _has_local_changes(repo: Path, rel_path: str) -> bool:
    """
    True if *rel_path* is modified (staged or unstaged) w.r.t `HEAD`.
    """
    status = _git(repo, "status", "--porcelain", "--", rel_path, capture=True)
    return bool(status.strip())


def _commit(repo: Path, message: str) -> None:
    """
    Stage **all** changes and commit with *message*.
    """
    _git(repo, "add", "-A")
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


def _write_file(p: Path, body: str | None, body_b64: str | None) -> None:
    """
    Write *body* (text) or *body_b64* (binary) into *p*, ensuring parent dirs.
    """
    p.parent.mkdir(parents=True, exist_ok=True)
    if body_b64 is not None:
        p.write_bytes(base64.b64decode(body_b64))
        log.debug("Wrote binary file %s (%d bytes)", p, p.stat().st_size)
    else:
        text = body or ""
        if not text.endswith("\n"):
            text += "\n"
        p.write_text(text, encoding="utf-8")
        log.debug("Wrote text file %s (%d chars)", p, len(text))

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

    # Guard against accidental overwrite
    if op in {"update", "delete", "rename", "chmod"} and _has_local_changes(repo, rel):
        raise RuntimeError(
            f"Refusing to {op} '{rel}' – local modifications detected."
        )

    # ----------------------- create / update -------------------------------
    if op in {"create", "update"}:
        if op == "create" and src.exists():
            raise FileExistsError(src)
        if op == "update" and not src.exists():
            raise FileNotFoundError(src)

        _write_file(src, patch.get("body"), patch.get("body_b64"))
        _commit(repo, f"GPT {op}: {rel}")
        return

    # ---------------------------- delete -----------------------------------
    if op == "delete":
        if not src.exists():
            raise FileNotFoundError(src)
        if src.is_dir():
            raise IsADirectoryError(src)
        src.unlink()
        _commit(repo, f"GPT delete: {rel}")
        return

    # ---------------------------- rename -----------------------------------
    if op == "rename":
        target = (repo / patch["target"]).resolve()
        _ensure_inside(repo, target)

        if not src.exists():
            raise FileNotFoundError(src)
        if target.exists():
            raise FileExistsError(target)

        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(src, target)
        _commit(repo, f"GPT rename: {rel} -> {patch['target']}")
        return

    # ---------------------------- chmod ------------------------------------
    if op == "chmod":
        mode = patch["mode"]
        if mode not in SAFE_MODES:
            raise PermissionError(f"Unsafe chmod mode {mode} (allowed: {SAFE_MODES})")
        if not src.exists():
            raise FileNotFoundError(src)
        os.chmod(src, int(mode, 8))
        _commit(repo, f"GPT chmod {mode}: {rel}")
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
