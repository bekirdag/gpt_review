#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
===============================================================================
GPT‑Review ▸ Filesystem & Git Utilities
===============================================================================

Purpose
-------
A compact, dependency‑free toolbox for repository inspection and safe Git ops.
These helpers are used by the orchestrator to:
  • create/switch iteration branches,
  • classify files into *code‑like* vs *deferred* (docs/setup/examples),
  • detect binary files conservatively,
  • provide language census and compact tree summaries,
  • read text with normalized EOLs for stable prompts.

Design
------
* No side effects beyond logging.
* All paths returned to callers are absolute `Path` objects under *repo*.
* We prefer `git ls-files` when available for reproducibility; otherwise we
  walk the filesystem with sane ignores.
* Binary detection is conservative: known binary extensions, NUL‑byte sniff,
  and UTF‑8 decode fallback.

Logging
-------
Uses the project logger so formatters/handlers are consistent everywhere.
"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import Dict, Iterable, List, Sequence, Tuple

from gpt_review import get_logger

log = get_logger(__name__)

# =============================================================================
# Git helpers
# =============================================================================


def git(repo: Path, *args: str, capture: bool = False, check: bool = True) -> str:
    """
    Run `git` with *repo* as working tree.

    Parameters
    ----------
    repo : Path
        Repository root (must contain `.git/`).
    *args : str
        Git arguments (e.g., "status", "--porcelain").
    capture : bool
        If True, return stdout as string; otherwise return "".
    check : bool
        If True, raise on non‑zero exit.

    Returns
    -------
    str
        Stdout when capture=True; else empty string.
    """
    res = subprocess.run(
        ["git", "-C", str(repo), *args],
        text=True,
        capture_output=capture,
    )
    if check and res.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {res.stderr.strip()}")
    return (res.stdout or "") if capture else ""


def current_commit(repo: Path) -> str:
    """
    Return HEAD SHA or the literal string '<no-commits-yet>' on fresh repos.
    """
    try:
        out = git(repo, "rev-parse", "--verify", "-q", "HEAD", capture=True, check=False).strip()
        return out or "<no-commits-yet>"
    except Exception:
        return "<no-commits-yet>"


def ensure_clean_worktree(repo: Path) -> None:
    """
    Raise RuntimeError if repository has local changes (untracked or modified).
    """
    status = git(repo, "status", "--porcelain", capture=True)
    if status.strip():
        log.error("Dirty working tree:\n%s", status)
        raise RuntimeError(
            "Working tree has uncommitted changes. Commit/stash before running GPT‑Review."
        )


def checkout_branch(repo: Path, name: str) -> None:
    """
    Create (or reset) and switch to branch *name* anchored at current HEAD.

        git switch -C <name>
    """
    ensure_clean_worktree(repo)
    git(repo, "switch", "-C", name)
    log.info("Switched to branch: %s (base=%s)", name, current_commit(repo))


def list_tracked_files(repo: Path) -> List[Path]:
    """
    Return tracked files (via `git ls-files`). If Git is unavailable, fall back
    to walking the filesystem (excluding `.git/`).
    """
    try:
        out = git(repo, "ls-files", capture=True)
        files = [repo / p for p in out.splitlines() if p.strip()]
        return files
    except Exception:
        # Conservative fallback
        paths: List[Path] = []
        for root, dirs, files in os.walk(repo):
            if ".git" in dirs:
                dirs.remove(".git")
            for f in files:
                paths.append(Path(root) / f)
        return paths


# =============================================================================
# Classification / detection
# =============================================================================

# Documentation & meta
_DOC_PATTERNS: Sequence[str] = (
    r"(^|/)(README|CHANGELOG|CONTRIBUTING|SECURITY|CODE_OF_CONDUCT)\.(md|rst|txt)$",
    r"(^|/)docs/.*",
    r"(^|/)\.github/workflows/.*\.ya?ml$",
)

# Installation / packaging / setup
_INSTALL_SETUP_PATTERNS: Sequence[str] = (
    r"(^|/)setup\.py$",
    r"(^|/)pyproject\.toml$",
    r"(^|/)(install|update|cookie_login|software_review)\.sh$",
    r"(^|/)Dockerfile$",
    r"(^|/)(Makefile|requirements\.txt|Pipfile(\.lock)?|poetry\.lock)$",
)

# Examples
_EXAMPLE_PATTERNS: Sequence[str] = (
    r"(^|/)examples?/.*",
    r"(^|/)example_.*",
    r"(^|/)(samples?|sample_.*)",
)

# Common binary extensions — a heuristic; we still inspect bytes for \x00
_BINARY_EXTS: set[str] = {
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".webp", ".avif",
    ".pdf",
    ".zip", ".gz", ".tgz", ".xz", ".tar", ".7z", ".rar", ".bz2", ".zst",
    ".woff", ".woff2", ".ttf", ".otf", ".eot",
    ".mp3", ".aac", ".flac", ".wav",
    ".mp4", ".mov", ".avi", ".mkv", ".webm",
    ".bin", ".exe", ".dll", ".dylib", ".so", ".class",
    # Often text, but we treat conservatively in automation:
    ".svg",
}


def matches_any(patterns: Sequence[str], rel_posix_path: str) -> bool:
    """True if *rel_posix_path* matches any regex in *patterns* (case‑insensitive)."""
    return any(re.search(p, rel_posix_path, flags=re.IGNORECASE) for p in patterns)


def is_binary_file(p: Path) -> bool:
    """
    Heuristic binary detection:
      • Known extensions → binary
      • Contains NUL byte → binary
      • Fails utf‑8 decode → binary
    """
    try:
        if p.suffix.lower() in _BINARY_EXTS:
            return True
        chunk = p.read_bytes()[: 2048]
        if b"\x00" in chunk:
            return True
        # Attempt utf‑8 decode (best‑effort)
        _ = chunk.decode("utf-8")
        return False
    except Exception:
        # Be conservative on any IO/codec errors
        return True


def classify_paths(repo: Path) -> Tuple[List[Path], List[Path]]:
    """
    Split repository files into two buckets:

        (code_like, deferred_docs_setup_examples)

    Both lists contain absolute `Path` objects under *repo* (sorted).
    """
    all_files = list_tracked_files(repo)
    code_like: List[Path] = []
    deferred: List[Path] = []

    for p in all_files:
        try:
            rel = p.relative_to(repo).as_posix()
        except Exception:
            # Skip weird paths that cannot be relativized
            log.debug("Skipping non-relativizable path: %s", p)
            continue

        if matches_any(_DOC_PATTERNS, rel) or matches_any(_INSTALL_SETUP_PATTERNS, rel) or matches_any(
            _EXAMPLE_PATTERNS, rel
        ):
            deferred.append(p)
        else:
            code_like.append(p)

    code_like.sort()
    deferred.sort()
    log.info(
        "Path classification: %d total → %d code-like, %d deferred (docs/setup/examples).",
        len(all_files),
        len(code_like),
        len(deferred),
    )
    return code_like, deferred


# =============================================================================
# Summaries & language census
# =============================================================================

_LANG_BY_EXT: Dict[str, str] = {
    ".py": "python",
    ".pyi": "python",
    ".sh": "shell",
    ".md": "markdown",
    ".yml": "yaml",
    ".yaml": "yaml",
    ".json": "json",
    ".toml": "toml",
    ".ini": "ini",
    ".cfg": "ini",
    ".js": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".java": "java",
    ".kt": "kotlin",
    ".go": "go",
    ".rb": "ruby",
    ".rs": "rust",
    ".cpp": "cpp",
    ".cxx": "cpp",
    ".cc": "cpp",
    ".c": "c",
    ".h": "c-header",
    ".hpp": "cpp-header",
    ".html": "html",
    ".css": "css",
    ".scss": "scss",
}


def language_census(paths: Sequence[Path]) -> List[str]:
    """
    Return a compact census list: ["python:42", "shell:5", …] for prompt context.
    """
    counts: Dict[str, int] = {}
    for p in paths:
        lang = _LANG_BY_EXT.get(p.suffix.lower(), "other")
        counts[lang] = counts.get(lang, 0) + 1
    return [f"{k}:{v}" for k, v in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))]


def summarize_repo(repo: Path, max_lines: int = 400) -> str:
    """
    Return a newline‑joined list of relative paths (tracked files), trimmed to
    *max_lines* to keep prompts compact.
    """
    try:
        out = git(repo, "ls-files", capture=True)
        lines = [ln for ln in out.splitlines() if ln.strip()]
    except Exception:
        # Fallback: walk the tree
        lines = []
        for p in sorted(list_tracked_files(repo)):
            try:
                lines.append(p.relative_to(repo).as_posix())
            except Exception:
                pass

    if len(lines) > max_lines:
        head = "\n".join(lines[: max_lines // 2])
        tail = "\n".join(lines[-max_lines // 2 :])
        return f"{head}\n…\n{tail}"
    return "\n".join(lines)


# =============================================================================
# Text helpers
# =============================================================================


def read_text_normalized(p: Path) -> str:
    """
    Read a text file as UTF‑8 and normalize end‑of‑line to LF.

    The patch‑apply layer ensures a trailing newline and permission handling;
    here we only normalize CRLF/CR so prompts stay stable across platforms.
    """
    txt = p.read_text(encoding="utf-8")
    return txt.replace("\r\n", "\n").replace("\r", "\n")


# =============================================================================
# __all__
# =============================================================================

__all__ = [
    # git
    "git",
    "current_commit",
    "ensure_clean_worktree",
    "checkout_branch",
    "list_tracked_files",
    # classification
    "classify_paths",
    "is_binary_file",
    # summaries
    "language_census",
    "summarize_repo",
    # text
    "read_text_normalized",
]
