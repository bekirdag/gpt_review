#!/usr/bin/env python3
"""
apply_patch.py — apply a validated JSON patch to a git repository.

*Now detects local modifications* and refuses to overwrite them
to prevent silent loss of work.
"""
import base64
import os
import shutil
import subprocess
import sys
from pathlib import Path

from patch_validator import validate_patch

SAFE_MODES = {"644", "755"}


# ───────────────────────── git helpers ──────────────────────────
def _git(repo: Path, *args, capture=False) -> str:
    res = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=capture,
        text=True,
    )
    return res.stdout if capture else ""


def _has_local_changes(repo: Path, rel_path: str) -> bool:
    """
    Return True if *rel_path* is modified (staged or not) compared to HEAD.
    """
    out = _git(repo, "status", "--porcelain", "--", rel_path, capture=True)
    return bool(out.strip())


def _commit(repo: Path, msg: str):
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", msg)


def _inside(repo: Path, p: Path):
    try:
        p.relative_to(repo)
    except ValueError as e:
        raise ValueError("Path escapes repository root") from e


def _write_file(p: Path, body: str | None, body_b64: str | None):
    if body_b64 is not None:
        p.write_bytes(base64.b64decode(body_b64))
    else:
        text = body or ""
        if not text.endswith("\n"):
            text += "\n"
        p.write_text(text, encoding="utf-8")


# ───────────────────────── main apply logic ─────────────────────
def apply_patch(patch_json: str, repo_path: str) -> None:
    patch = validate_patch(patch_json)
    repo = Path(repo_path).resolve()
    if not repo.is_dir():
        raise FileNotFoundError(repo)

    rel = patch.get("file", "")
    path = repo / rel
    _inside(repo, path)

    # Guard against overwriting local edits
    if patch["op"] in {"update", "delete", "rename", "chmod"} and _has_local_changes(
        repo, rel
    ):
        raise RuntimeError(
            f"Refusing to {patch['op']} '{rel}' — local modifications detected."
        )

    op = patch["op"]
    if op in {"create", "update"}:
        if op == "create" and path.exists():
            raise FileExistsError(path)
        if op == "update" and not path.exists():
            raise FileNotFoundError(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        _write_file(path, patch.get("body"), patch.get("body_b64"))
        _commit(repo, f"GPT {op}: {rel}")

    elif op == "delete":
        if not path.exists():
            raise FileNotFoundError(path)
        if path.is_dir():
            raise IsADirectoryError(path)
        path.unlink()
        _commit(repo, f"GPT delete: {rel}")

    elif op == "rename":
        target = repo / patch["target"]
        _inside(repo, target)
        if target.exists():
            raise FileExistsError(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(path), str(target))
        _commit(repo, f"GPT rename: {rel} -> {patch['target']}")

    elif op == "chmod":
        mode = patch["mode"]
        if mode not in SAFE_MODES:
            raise PermissionError(f"Unsafe mode {mode}")
        if not path.exists():
            raise FileNotFoundError(path)
        os.chmod(path, int(mode, 8))
        _commit(repo, f"GPT chmod {mode}: {rel}")

    else:
        raise ValueError(f"Unknown op {op}")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit("Usage: apply_patch.py <json|-> <repo>")
    patch_arg, repo_arg = sys.argv[1:]
    data = sys.stdin.read() if patch_arg == "-" else patch_arg
    apply_patch(data, repo_arg)
