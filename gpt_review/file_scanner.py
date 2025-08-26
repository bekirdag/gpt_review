#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
===============================================================================
GPT‑Review ▸ Repository Scanner & Classifier
===============================================================================

Purpose
-------
Provide a resilient, Git‑aware view of the repository so the orchestrator can:
  • enumerate files relative to the repo root (POSIX paths),
  • classify each file (code, test, docs, install, setup, examples, binary),
  • filter files per **iteration rules**:
        - Iterations 1 & 2 → focus on code/tests; defer docs/install/setup/examples
        - Iteration 3       → include the deferred classes as well
  • (optionally) read text files with LF normalization (no CR/CRLF).

Design notes
------------
• We prefer `git ls-files` to respect .gitignore and avoid scanning bulky
  directories (node_modules/, build/, etc). If Git calls fail, we fall back to
  a guarded filesystem walk while applying a conservative exclude set.
• All paths returned are **POSIX** ("/") and **relative** to the repo root.
• This module is side‑effect free; no writes, no staging. It only observes.
• No external deps; relies on stdlib + Git CLI.

Integration points
------------------
• `scan_repository()` – core enumerator (returns `List[FileInfo]`)
• `files_for_iteration()` – filtered, ordered paths for a given iteration
• `classify_path()` – standalone classifier (used by prompts/orchestrator)
• `read_text_file()` – normalized text reader (LF line endings)

Logging
-------
INFO: summary counts by category; DEBUG: per‑file classification.
"""
from __future__ import annotations

import fnmatch
import logging
import os
import subprocess
from dataclasses import dataclass
from enum import Enum, auto
from pathlib import Path
from typing import Iterable, List, Optional, Sequence, Tuple

from gpt_review import get_logger

log = get_logger(__name__)


# =============================================================================
# Categories
# =============================================================================
class Category(Enum):
    """High‑level file classes used to drive iteration ordering."""

    CODE = auto()       # application/library source code
    TEST = auto()       # tests (unit/integration)
    DOCS = auto()       # markdown/rst + docs/ tree
    INSTALL = auto()    # install/update scripts, Dockerfiles, deployment helpers
    SETUP = auto()      # packaging/setup files (pyproject, setup.cfg, requirements, etc.)
    EXAMPLE = auto()    # example assets, sample configs
    DATA = auto()       # non‑code text data (json/yaml/txt not under docs/examples)
    BINARY = auto()     # images, archives, compiled blobs
    UNKNOWN = auto()    # fallback


# =============================================================================
# Data model
# =============================================================================
@dataclass(frozen=True)
class FileInfo:
    """Immutable descriptor for one repository file."""

    rel: str                # POSIX relative path (e.g., "src/app.py")
    category: Category
    size: int               # bytes
    is_text: bool           # heuristic
    # Optional hints (extensible)
    language: Optional[str] = None  # best‑effort, e.g. "python", "javascript"


# =============================================================================
# Heuristics & configuration
# =============================================================================
# Common binary extensions (non‑exhaustive; safe bias toward BINARY)
_BINARY_EXTS = {
    # images
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".svg", ".webp", ".ico",
    # archives / binaries
    ".zip", ".tar", ".gz", ".bz2", ".xz", ".7z", ".rar", ".iso",
    ".pdf", ".ttf", ".otf", ".woff", ".woff2",
    ".so", ".dll", ".dylib", ".exe", ".bin", ".class",
    # media
    ".mp3", ".wav", ".flac", ".ogg", ".mp4", ".mkv", ".mov", ".avi",
}

# Known language extensions (partial; add as needed)
_LANG_BY_EXT = {
    # Python
    ".py": "python",
    ".pyi": "python",
    # JavaScript / TypeScript
    ".js": "javascript",
    ".cjs": "javascript",
    ".mjs": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".jsx": "javascript",
    # Shell
    ".sh": "shell",
    ".bash": "shell",
    # Config / Data
    ".json": "json",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".toml": "toml",
    ".ini": "ini",
    ".cfg": "ini",
    ".conf": "ini",
    ".txt": "text",
    ".md": "markdown",
    ".rst": "rst",
    # Misc code
    ".go": "go",
    ".rs": "rust",
    ".java": "java",
    ".kt": "kotlin",
    ".c": "c",
    ".h": "c",
    ".cpp": "cpp",
    ".hpp": "cpp",
    ".cs": "csharp",
    ".php": "php",
    ".rb": "ruby",
    ".swift": "swift",
}

# Fall‑back filesystem excludes (only used when Git is unavailable)
_FS_EXCLUDES: Tuple[str, ...] = (
    ".git/**",
    ".git",
    "venv/**",
    ".venv/**",
    "node_modules/**",
    "dist/**",
    "build/**",
    ".tox/**",
    ".pytest_cache/**",
    "__pycache__/**",
    "coverage/**",
    "htmlcov/**",
    "docker-build/**",
    "logs/**",
    ".cache/**",
)

# Classification patterns
_DOC_PATTERNS: Tuple[str, ...] = (
    "README.*",
    "CHANGELOG.*",
    "CONTRIBUTING.*",
    "LICENSE*",
    "docs/**",
    "*.md",
    "*.rst",
)
_INSTALL_PATTERNS: Tuple[str, ...] = (
    "install.sh",
    "update.sh",
    "cookie_login.sh",
    "software_review.sh",
    "scripts/install*",
    "scripts/setup*",
    "Dockerfile",
    "docker/**",
    ".github/workflows/**",
)
_SETUP_PATTERNS: Tuple[str, ...] = (
    "pyproject.toml",
    "setup.cfg",
    "setup.py",
    "requirements*.txt",
    "Pipfile",
    "poetry.lock",
    ".flake8",
    ".editorconfig",
    ".pre-commit-config.yaml",
)
_EXAMPLE_PATTERNS: Tuple[str, ...] = (
    "examples/**",
    "example/**",
    "example_*",
    "example*.*",
    "docs/examples/**",
    "example_instructions.txt",
)
_TEST_PATTERNS: Tuple[str, ...] = (
    "tests/**",
    "test_*.*",
    "*_test.*",
)


# =============================================================================
# Utilities
# =============================================================================
def _to_posix(rel_path: Path) -> str:
    return rel_path.as_posix()


def _matches_any(path: str, patterns: Sequence[str]) -> bool:
    return any(fnmatch.fnmatch(path, pat) for pat in patterns)


def _git(repo: Path, *args: str, capture: bool = True) -> str:
    """
    Run git -C <repo> <args>. Raises on non‑zero unless capture=False is used.
    """
    res = subprocess.run(
        ["git", "-C", str(repo), *args],
        text=True,
        capture_output=capture,
        check=True,
    )
    return res.stdout if capture else ""


def _detect_text_file(p: Path, max_bytes: int = 4096) -> bool:
    """
    Light heuristic: try reading a small prefix in text mode with utf‑8 + 'replace'.
    If more than ~15% of the characters are the Unicode replacement char, treat as binary.
    """
    try:
        raw = p.read_bytes()[:max_bytes]
    except Exception:
        return False
    # Obvious binary signatures
    if b"\x00" in raw:
        return False
    try:
        txt = raw.decode("utf-8", errors="replace")
    except Exception:
        return False
    bad = txt.count("\uFFFD")
    return (len(txt) == 0) or (bad / max(1, len(txt)) <= 0.15)


def _guess_language(path: Path) -> Optional[str]:
    return _LANG_BY_EXT.get(path.suffix.lower())


def _classify(rel_posix: str, is_text: bool) -> Category:
    """
    Rule‑based classifier using glob patterns and filename heuristics.
    Pattern order matters for specificity.
    """
    name = rel_posix
    lower = name.lower()

    # Binaries first (by extension or explicit signal)
    if Path(name).suffix.lower() in _BINARY_EXTS or not is_text:
        return Category.BINARY

    # Docs
    if _matches_any(name, _DOC_PATTERNS):
        return Category.DOCS

    # Install / automation helpers
    if _matches_any(name, _INSTALL_PATTERNS):
        return Category.INSTALL

    # Setup / packaging
    if _matches_any(name, _SETUP_PATTERNS):
        return Category.SETUP

    # Examples
    if _matches_any(name, _EXAMPLE_PATTERNS):
        return Category.EXAMPLE

    # Tests (prefer explicit test patterns before generic code)
    if _matches_any(name, _TEST_PATTERNS):
        return Category.TEST

    # Generic data (json/yaml/txt) that didn't match docs/examples
    if Path(name).suffix.lower() in {".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf", ".txt"}:
        return Category.DATA

    # Fallback: code if extension looks like code; otherwise UNKNOWN
    if Path(name).suffix.lower() in _LANG_BY_EXT:
        return Category.CODE

    # Heuristic: top‑level Makefile, justfile → setup
    base = Path(name).name
    if base in {"Makefile", "makefile", "Justfile", "justfile"}:
        return Category.SETUP

    return Category.UNKNOWN


# =============================================================================
# Public API
# =============================================================================
def classify_path(repo: Path, rel_path: Path) -> Category:
    """
    Classify a single path relative to *repo*.
    """
    abs_path = (repo / rel_path).resolve()
    is_text = _detect_text_file(abs_path)
    category = _classify(_to_posix(rel_path), is_text)
    log.debug("Classified %s → %s (text=%s)", _to_posix(rel_path), category.name, is_text)
    return category


def scan_repository(repo: Path) -> List[FileInfo]:
    """
    Enumerate repository files (tracked + untracked but not ignored), classify,
    and return a list of `FileInfo` entries.

    Returns
    -------
    List[FileInfo]
        POSIX relative paths; no entries under `.git/`.
    """
    repo = repo.expanduser().resolve()
    if not (repo / ".git").exists():
        raise ValueError(f"Not a Git repository: {repo}")

    rel_paths: List[str] = []

    # Preferred: Git listing
    try:
        tracked = _git(repo, "ls-files").splitlines()
        others = _git(repo, "ls-files", "--others", "--exclude-standard").splitlines()
        rel_paths = tracked + others
        log.info("Git enumeration: %d tracked, %d untracked files.", len(tracked), len(others))
    except Exception as exc:
        log.warning("Git listing failed (%s). Falling back to filesystem walk.", exc)
        rel_paths = []
        for p in repo.rglob("*"):
            if p.is_dir():
                continue
            rel = p.relative_to(repo)
            posix = _to_posix(rel)
            if _matches_any(posix, _FS_EXCLUDES):
                continue
            if posix.startswith(".git/"):
                continue
            rel_paths.append(posix)

    # Deduplicate & sort
    rel_paths = sorted(dict.fromkeys(rel_paths))

    out: List[FileInfo] = []
    for posix in rel_paths:
        if posix.startswith(".git/") or posix == ".git":
            continue
        abs_p = (repo / posix)
        if not abs_p.exists():
            # Could be deleted since enumeration; skip.
            log.debug("Skipping vanished path: %s", posix)
            continue
        try:
            size = abs_p.stat().st_size
        except Exception:
            size = 0
        is_text = _detect_text_file(abs_p)
        cat = _classify(posix, is_text)
        lang = _guess_language(abs_p) if cat in (Category.CODE, Category.TEST) else None
        out.append(FileInfo(rel=posix, category=cat, size=size, is_text=is_text, language=lang))
        log.debug("FileInfo(%s) → cat=%s size=%d text=%s lang=%s",
                  posix, cat.name, size, is_text, lang or "-")

    # Summary
    counts = {}
    for fi in out:
        counts[fi.category] = counts.get(fi.category, 0) + 1
    summary = ", ".join(f"{k.name}={v}" for k, v in sorted(counts.items(), key=lambda kv: kv[0].name))
    log.info("Scan summary: %d files (%s)", len(out), summary or "no files")

    return out


def files_for_iteration(repo: Path, iteration: int) -> List[str]:
    """
    Return an ordered list of POSIX paths to process for *iteration*.

    Rules
    -----
    • Iterations 1 & 2:
        - include CODE, TEST, DATA, UNKNOWN
        - exclude DOCS, INSTALL, SETUP, EXAMPLE
        - exclude BINARY (we do not ask the API to edit binaries directly)
    • Iteration 3:
        - include **all** classes except BINARY by default
          (binaries may be created via body_b64 when requested explicitly)

    Ordering
    --------
    • Code first (language alphabetical, then path)
    • Tests next
    • Data/Unknown
    • Docs/Install/Setup/Examples (iteration 3 only)
    """
    infos = scan_repository(repo)

    def allowed(fi: FileInfo) -> bool:
        if fi.category == Category.BINARY:
            return False
        if iteration >= 3:
            return True  # except binaries (already filtered)
        # iteration 1 & 2
        return fi.category in {Category.CODE, Category.TEST, Category.DATA, Category.UNKNOWN}

    selected = [fi for fi in infos if allowed(fi)]

    # Ordering key
    def _key(fi: FileInfo) -> Tuple[int, str, str]:
        # group order
        group = {
            Category.CODE: 0,
            Category.TEST: 1,
            Category.DATA: 2,
            Category.UNKNOWN: 3,
            Category.DOCS: 4,
            Category.INSTALL: 5,
            Category.SETUP: 6,
            Category.EXAMPLE: 7,
        }.get(fi.category, 9)
        lang = fi.language or "~"
        return (group, lang, fi.rel)

    ordered = sorted(selected, key=_key)
    log.info("Iteration %d: %d files selected (of %d scanned).", iteration, len(ordered), len(infos))
    return [fi.rel for fi in ordered]


def read_text_file(repo: Path, rel_posix: str, *, max_bytes: int = 1024 * 1024) -> str:
    """
    Read a text file relative to *repo* with LF normalization.

    • CRLF/CR newlines are normalized to LF.
    • Appends a final newline if missing (consistent with our writer).
    • Raises ValueError if the target appears binary.

    Parameters
    ----------
    max_bytes : int
        Safety cap to avoid loading extremely large files into a prompt.

    Returns
    -------
    str
        Normalized UTF‑8 text.
    """
    p = (repo / rel_posix).resolve()
    if not p.exists() or not p.is_file():
        raise FileNotFoundError(rel_posix)
    if not _detect_text_file(p):
        raise ValueError(f"Refusing to read binary file as text: {rel_posix}")

    raw = p.read_bytes()[:max_bytes]
    txt = raw.decode("utf-8", errors="replace")
    # Normalize newlines to LF
    txt = txt.replace("\r\n", "\n").replace("\r", "\n")
    if not txt.endswith("\n"):
        txt += "\n"
    log.debug("Read text %s (%d bytes → %d chars normalized).", rel_posix, len(raw), len(txt))
    return txt


# =============================================================================
# Convenience: quick language census
# =============================================================================
def languages_present(repo: Path) -> List[Tuple[str, int]]:
    """
    Return a list of (language, file_count) pairs sorted by count desc,
    for CODE and TEST categories.
    """
    infos = scan_repository(repo)
    counts: dict[str, int] = {}
    for fi in infos:
        if fi.category in (Category.CODE, Category.TEST) and fi.language:
            counts[fi.language] = counts.get(fi.language, 0) + 1
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    log.info("Language census: %s", ", ".join(f"{k}:{v}" for k, v in ranked) or "<none>")
    return ranked
