#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
===============================================================================
GPT‑Review ▸ Filesystem & Git Utilities
===============================================================================

Purpose
-------
Helper utilities used by the orchestrator and other modules:

* Git helpers:
    - git(...)                – thin, logged wrapper around subprocess git
    - checkout_branch(...)    – create/switch branch (idempotent)
    - current_commit(...)     – HEAD SHA (short), resilient

* Repository scanning & classification:
    - classify_paths(...)     – split files into code-like vs deferred
    - is_binary_file(...)     – fast binary detector
    - read_text_normalized(...) – LF-normalized UTF‑8 text (lossy on errors)
    - language_census(...)    – ["python:42", "typescript:3", ...]
    - summarize_repo(...)     – compact tree-like snapshot for prompts

Design
------
* Pure stdlib (no external deps).
* Resilient to partial repositories and non-UTF8 files.
* POSIX paths for all relative paths (stable in prompts & patches).
"""
from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path
from typing import Iterable, List, Sequence, Tuple

from gpt_review import get_logger

log = get_logger(__name__)

# -----------------------------------------------------------------------------
# Git helpers
# -----------------------------------------------------------------------------

def git(repo: Path, *args: str, check: bool = False) -> subprocess.CompletedProcess[str]:
    """
    Run a git command in *repo* and return CompletedProcess.

    Parameters
    ----------
    repo : Path
        Path to the repository root.
    *args : str
        git subcommands and arguments, e.g., "status", "--porcelain".
    check : bool
        If True, raise CalledProcessError on non-zero exit.

    Returns
    -------
    subprocess.CompletedProcess[str]
    """
    cmd = ["git", "-C", str(repo), *args]
    log.debug("git: %s", " ".join(cmd))
    proc = subprocess.run(cmd, text=True, capture_output=True)
    if check and proc.returncode != 0:
        log.error("git failed (rc=%s): %s\n%s", proc.returncode, " ".join(cmd), proc.stderr.strip())
        proc.check_returncode()
    return proc


def checkout_branch(repo: Path, branch: str) -> None:
    """
    Create/switch to *branch* in *repo*. Safe if branch already exists.
    """
    try:
        # Does branch exist?
        exists = git(repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}").returncode == 0
        if exists:
            git(repo, "switch", branch, check=True)
            log.info("Checked out existing branch '%s'.", branch)
        else:
            git(repo, "switch", "-c", branch, check=True)
            log.info("Created and switched to new branch '%s'.", branch)
    except Exception as exc:
        log.exception("Failed to checkout/switch branch '%s': %s", branch, exc)
        raise


def current_commit(repo: Path) -> str:
    """
    Return short HEAD SHA; '<no-commits-yet>' if none.
    """
    try:
        out = git(repo, "rev-parse", "--short", "HEAD").stdout.strip()
        return out or "<no-commits-yet>"
    except Exception:
        return "<no-commits-yet>"


# -----------------------------------------------------------------------------
# File classification & reading
# -----------------------------------------------------------------------------

# Extensions considered "documentation-ish" (deferred to iteration 3)
_DOC_EXT = {
    ".md", ".rst", ".adoc", ".org", ".txt",
    ".markdown", ".mdown", ".mkdn", ".mkd",
}

# Installation / setup / packaging / CI markers (also deferred until iteration 3)
_DEFERRED_BASENAMES = {
    # Python packaging
    "pyproject.toml", "setup.cfg", "setup.py", "requirements.txt",
    "requirements-dev.txt", "Pipfile", "Pipfile.lock", "poetry.lock",
    # JS/TS packaging
    "package.json", "package-lock.json", "yarn.lock", "pnpm-lock.yaml",
    # Containers / build
    "Dockerfile", "docker-compose.yml", "docker-compose.yaml",
    "Makefile",  # often build/installation glue
    # CI
    ".gitlab-ci.yml", "azure-pipelines.yml",
}

# Deferred directories (prefix match on first path segment)
_DEFERRED_DIRS = {
    ".github", "docs", "doc", "documentation", ".gpt-review", "examples", "example",
    "samples", "sample", "site", "book", "mkdocs", "guides",
}

# Transient / build / vendor directories to skip from scans/summaries
_SKIP_DIRS = {
    ".git", ".svn", ".hg", ".idea", ".vscode", ".pytest_cache",
    "__pycache__", "dist", "build", "node_modules", ".venv", "venv", ".mypy_cache",
    ".tox", ".cache", ".next", ".nuxt", "coverage", ".ruff_cache",
}

# File extensions to consider "text code-like" even if config-ish
_TEXT_CODE_EXT = {
    # Core languages
    ".py", ".pyi", ".ipynb",
    ".js", ".jsx", ".ts", ".tsx",
    ".go", ".rs", ".java", ".kt", ".swift", ".rb",
    ".c", ".h", ".cpp", ".hpp", ".cc",
    ".cs", ".m", ".mm",
    ".php",
    # Scripts / config that we still treat as code-like
    ".sh", ".bash", ".zsh", ".ps1",
    ".toml", ".ini", ".cfg",
    ".json", ".yaml", ".yml",
    ".env", ".properties",
    ".gradle", ".groovy",
    ".xml", ".xsd",
    ".proto",
    ".sql",
}


def _first_segment(rel: Path) -> str:
    try:
        return rel.parts[0]
    except Exception:
        return ""


def is_binary_file(path: Path, sniff_bytes: int = 4096) -> bool:
    """
    Heuristic binary detector: returns True if NUL byte present in the first
    *sniff_bytes* or file mode is executable non-text in some edge cases.

    This is intentionally simple and fast.
    """
    try:
        with path.open("rb") as f:
            chunk = f.read(sniff_bytes)
        if b"\x00" in chunk:
            return True
        # If it is very high-ASCII density and no newline, likely binary
        if chunk and (sum(b > 127 for b in chunk) / len(chunk) > 0.95) and b"\n" not in chunk:
            return True
        # Executable bit alone does not imply binary; keep it textual.
        return False
    except Exception:
        # If unreadable, err on the side of non-binary to allow inspection.
        return False


def read_text_normalized(path: Path) -> str:
    """
    Read file as UTF‑8 (lossy on errors), normalize EOL to '\n'.
    """
    data = path.read_bytes()
    text = data.decode("utf-8", errors="replace")
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _is_deferred(rel: Path) -> bool:
    """
    Return True if *rel* is a documentation/setup/example/CI file we defer until iteration 3.
    """
    # Directory-based quick checks
    if _first_segment(rel) in _DEFERRED_DIRS:
        return True

    # Basename match
    if rel.name in _DEFERRED_BASENAMES:
        return True

    # Extension-based docs
    if rel.suffix.lower() in _DOC_EXT:
        return True

    # CI workflows
    if rel.as_posix().startswith(".github/workflows/"):
        return True

    return False


def classify_paths(repo: Path) -> Tuple[List[Path], List[Path]]:
    """
    Walk *repo* and return (code_like, deferred) path lists.

    * Skips transient/vendor dirs.
    * Treats binary files as code-excluded unless explicitly whitelisted.
    * Defers docs/setup/examples/CI until iteration 3.
    """
    code_like: List[Path] = []
    deferred: List[Path] = []

    repo = repo.resolve()
    for root, dirs, files in os.walk(repo):
        # Prune skip directories in-place to avoid walking into them
        dirs[:] = [d for d in dirs if d not in _SkipSet()]
        base = Path(root)

        for name in files:
            p = base / name
            # Compute POSIX-relative path
            try:
                rel = p.relative_to(repo)
            except Exception:
                # Should not happen; skip if outside repo
                continue

            # Skip git-specific internals and patch tool itself
            if rel.as_posix().startswith(".git/"):
                continue

            # Classify deferred
            if _is_deferred(rel):
                deferred.append(p)
                continue

            # Binary files are not code-like for review steps
            if is_binary_file(p):
                deferred.append(p)
                continue

            # Treat as code-like if extension is known, otherwise default to code-like
            if rel.suffix.lower() in _TEXT_CODE_EXT or rel.suffix:
                code_like.append(p)
            else:
                code_like.append(p)

    code_like.sort()
    deferred.sort()
    log.debug("classify_paths: code=%d deferred=%d", len(code_like), len(deferred))
    return code_like, deferred


def _SkipSet() -> set:
    """
    Build skip set once (function to avoid module import-time surprises).
    """
    return set(_SKIP_DIRS)


# -----------------------------------------------------------------------------
# Language census & repo summary
# -----------------------------------------------------------------------------

def _lang_for_extension(ext: str) -> str:
    e = ext.lower()
    return {
        ".py": "python",
        ".pyi": "python",
        ".ipynb": "python",
        ".js": "javascript",
        ".jsx": "javascript",
        ".ts": "typescript",
        ".tsx": "typescript",
        ".go": "go",
        ".rs": "rust",
        ".java": "java",
        ".kt": "kotlin",
        ".swift": "swift",
        ".rb": "ruby",
        ".c": "c",
        ".h": "c",
        ".cpp": "cpp",
        ".hpp": "cpp",
        ".cc": "cpp",
        ".cs": "csharp",
        ".m": "objc",
        ".mm": "objc++",
        ".php": "php",
        ".sh": "shell",
        ".bash": "shell",
        ".zsh": "shell",
        ".ps1": "powershell",
        ".toml": "toml",
        ".ini": "ini",
        ".cfg": "ini",
        ".json": "json",
        ".yaml": "yaml",
        ".yml": "yaml",
        ".xml": "xml",
        ".proto": "proto",
        ".sql": "sql",
        ".md": "markdown",
        ".rst": "rst",
        ".txt": "text",
    }.get(e, e.lstrip(".") or "other")


def language_census(files: Sequence[Path]) -> List[str]:
    """
    Return a compact census like ["python:42", "typescript:3", "yaml:7"].

    * Counts by extension → language mapping.
    * Sorted by descending count, then alphabetically.
    """
    counts: dict[str, int] = {}
    for p in files:
        lang = _lang_for_extension(p.suffix)
        counts[lang] = counts.get(lang, 0) + 1

    items = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return [f"{lang}:{n}" for lang, n in items]


def summarize_repo(repo: Path, *, max_entries: int = 400) -> str:
    """
    Produce a compact, stable summary of the repository for prompts.

    - Lists up to *max_entries* files (POSIX relative paths).
    - Skips vendor/transient directories.
    - Distinguishes deferred files with a trailing " (deferred)" marker.
    """
    repo = repo.resolve()
    entries: List[str] = []
    code_like, deferred = classify_paths(repo)

    def rels(paths: Iterable[Path], suffix: str = "") -> List[str]:
        out: List[str] = []
        for p in paths:
            try:
                out.append(p.relative_to(repo).as_posix() + suffix)
            except Exception:
                continue
        return out

    lines = rels(code_like) + rels(deferred, " (deferred)")
    lines.sort()
    if len(lines) > max_entries:
        head = lines[: max_entries // 2]
        tail = lines[-(max_entries - len(head)) :]
        lines = head + ["… (truncated) …"] + tail

    summary = "\n".join(lines)
    log.debug("summarize_repo: %d entries (max=%d)", len(lines), max_entries)
    return summary
