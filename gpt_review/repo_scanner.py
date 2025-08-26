#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
===============================================================================
GPT‑Review ▸ Repository scanner & file classifier
===============================================================================

Purpose
-------
Provide a reliable, logged index of repository files, with categories that
support the multi‑iteration workflow:

  • Iteration 1 & 2 → operate on *code* (and tests) only
  • Iteration 3      → include docs (.md/.rst), install/setup, and examples

This module never mutates the repo; it only inspects it.  Writing happens
through the existing patch applier (apply_patch.py) to preserve invariants
like **path‑scoped staging**, normalization, and safety checks.

Key features
------------
* Fast recursive walk with ignore patterns (e.g., .git/, venv/, node_modules/)
* Robust binary sniff (null‑byte / control‑density heuristic)
* Reproducible, stable ordering (POSIX‑style paths, lexicographic sort)
* Clear classification buckets:
    - code_files
    - test_files
    - docs_files
    - setup_files
    - example_files
    - binary_files (superset across buckets)
* Iteration‑aware selection: `files_for_iteration(iter_no)`
* Human‑readable logged summary

Usage
-----
    from pathlib import Path
    from gpt_review.repo_scanner import RepoScanner

    scanner = RepoScanner(Path("/path/to/repo"))
    index = scanner.scan()
    files_for_iter1 = scanner.files_for_iteration(1)   # code + tests only
    files_for_iter3 = scanner.files_for_iteration(3)   # include docs/setup/examples
"""
from __future__ import annotations

import fnmatch
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, List, Set

from gpt_review import get_logger

log = get_logger(__name__)

# --------------------------------------------------------------------------- #
# Heuristics & constants
# --------------------------------------------------------------------------- #

# Directories to exclude entirely (top-level wildcards allowed)
_IGNORE_DIRS: Set[str] = {
    ".git", ".hg", ".svn",
    "__pycache__", ".mypy_cache", ".ruff_cache", ".pytest_cache",
    "node_modules", "dist", "build", "target", ".tox", "htmlcov",
    ".idea", ".vscode", ".DS_Store", ".cache", "logs", "docker-build",
    "venv", ".venv", "env", ".env",
}

# File globs to ignore (heavy artefacts, coverage, local state, etc.)
_IGNORE_FILE_GLOBS: Set[str] = {
    "*.pyc", "*.pyo", "*.pyd", "*.so", "*.dylib",
    "*.exe", "*.dll", "*.obj", "*.a", "*.o",
    "*.class", "*.jar",
    "*.log", "*.tmp", "*.swp", "*.swo", "*~",
    ".coverage", "coverage.xml",
}

# Docs / prose
_DOC_EXTS: Set[str] = {".md", ".rst", ".adoc", ".txt"}
_DOC_BASENAMES: Set[str] = {
    "README", "CHANGELOG", "CONTRIBUTING", "LICENSE", "SECURITY",
    "CODE_OF_CONDUCT", "CODE-OF-CONDUCT",
}

# Setup / install / CI
_SETUP_BASENAMES: Set[str] = {
    "setup.py", "pyproject.toml", "requirements.txt", "Pipfile",
    "Pipfile.lock", "poetry.lock", "Makefile", "Dockerfile",
    "docker-compose.yml", "docker-compose.yaml",
    "install.sh", "update.sh", "software_review.sh", "cookie_login.sh",
    ".pre-commit-config.yaml", ".pre-commit-config.yml",
}
_SETUP_DIR_HINTS: Set[str] = {".github/workflows", ".github/actions", "ci", ".ci"}

# Example assets & prompts
_EXAMPLE_HINTS: Set[str] = {
    "examples", "example", "sample", "samples",
}
_EXAMPLE_BASENAMES: Set[str] = {"example_instructions.txt"}

# Tests
_TEST_DIR_HINTS: Set[str] = {"tests", "test", "spec", "specs"}
_TEST_FILE_PATTERNS: Set[str] = {"test_*.py", "*_test.py", "*.spec.js", "*.spec.ts"}

# File types likely textual code/config (still validated via binary sniff)
_TEXT_CODE_EXTS: Set[str] = {
    # Languages
    ".py", ".pyi", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs",
    ".ts", ".tsx", ".go", ".rb", ".rs", ".c", ".cc", ".cpp", ".h", ".hpp",
    ".java", ".kt", ".kts", ".scala", ".swift", ".php", ".pl", ".cs",
    # Shell / scripting
    ".sh", ".bash", ".zsh", ".ps1", ".cmd", ".bat",
    # Data / config
    ".toml", ".ini", ".cfg", ".conf", ".yaml", ".yml", ".json", ".jsonc",
    ".graphql", ".proto", ".sql", ".env", ".editorconfig",
    # Markup/templates (often code-adjacent)
    ".html", ".htm", ".xhtml", ".xml", ".xsl", ".svg", ".css", ".scss", ".less",
    ".jinja", ".j2", ".ejs", ".hbs",
}

# Heuristic binary extensions (used as a short‑circuit before sniff)
_BINARY_EXTS: Set[str] = {
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".webp", ".avif",
    ".tar", ".gz", ".tgz", ".zip", ".7z", ".rar", ".xz", ".bz2", ".zst",
    ".pdf", ".woff", ".woff2", ".ttf", ".otf", ".eot",
    ".mp3", ".aac", ".flac", ".wav",
    ".mp4", ".mov", ".avi", ".mkv", ".webm",
    ".bin", ".exe", ".dll", ".dylib", ".so", ".class",
}

# Max bytes to read when sniffing binary/text
_SNIFF_BYTES = 4096


@dataclass
class RepoIndex:
    """
    Structured inventory of repository files (POSIX relative paths).
    """
    root: Path
    all_files: List[str] = field(default_factory=list)
    code_files: List[str] = field(default_factory=list)
    test_files: List[str] = field(default_factory=list)
    docs_files: List[str] = field(default_factory=list)
    setup_files: List[str] = field(default_factory=list)
    example_files: List[str] = field(default_factory=list)
    binary_files: List[str] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"{len(self.all_files)} files "
            f"(code={len(self.code_files)}, tests={len(self.test_files)}, "
            f"docs={len(self.docs_files)}, setup={len(self.setup_files)}, "
            f"examples={len(self.example_files)}, binary={len(self.binary_files)})"
        )


class RepoScanner:
    """
    Filesystem scanner with category heuristics and iteration‑aware views.
    """

    def __init__(self, repo_root: Path):
        self.root = Path(repo_root).expanduser().resolve()
        if not (self.root / ".git").exists():
            raise RuntimeError(f"Not a git repository: {self.root}")

    # ---------------------------- public API -------------------------------- #
    def scan(self) -> RepoIndex:
        """
        Walk the repository and build a `RepoIndex`.

        Returns
        -------
        RepoIndex
            Collected, sorted file lists by category.
        """
        log.info("Scanning repository: %s", self.root)
        files: List[Path] = []

        for p in self._iter_files(self.root):
            files.append(p)

        files_rel = [self._relposix(p) for p in files]
        files_rel.sort()

        idx = RepoIndex(root=self.root, all_files=files_rel)

        for rel in files_rel:
            p = self.root / rel
            cat = self._classify(rel)

            # Binary detection (short-circuit by extension, then sniff)
            is_bin = self._seems_binary(p)
            if is_bin:
                idx.binary_files.append(rel)

            if cat == "doc":
                idx.docs_files.append(rel)
            elif cat == "setup":
                idx.setup_files.append(rel)
            elif cat == "example":
                idx.example_files.append(rel)
            elif cat == "test":
                idx.test_files.append(rel)
                # Test files are also code for our purposes
                if not is_bin:
                    idx.code_files.append(rel)
            elif cat == "code":
                if not is_bin:
                    idx.code_files.append(rel)
            else:
                # Unknown/other → treat textual, non-binary as code by default,
                # so the review cycle doesn't miss config-like files.
                if not is_bin:
                    idx.code_files.append(rel)

        log.info("Scan summary: %s", idx.summary())
        return idx

    def files_for_iteration(self, iteration: int) -> List[str]:
        """
        Return ordered file list for the given iteration.

        Rules
        -----
        • Iterations 1 & 2: **code + tests** only
        • Iteration 3     : code + tests + **docs + setup + examples**

        Parameters
        ----------
        iteration : int
            1‑based iteration number.

        Returns
        -------
        list[str]
            POSIX‑relative file paths in deterministic order.
        """
        if not hasattr(self, "_cached_index"):
            self._cached_index = self.scan()  # type: ignore[attr-defined]

        idx: RepoIndex = self._cached_index  # type: ignore[assignment]

        if iteration >= 3:
            combined = (
                sorted(set(idx.code_files))
                + sorted(set(idx.test_files))
                + sorted(set(idx.docs_files))
                + sorted(set(idx.setup_files))
                + sorted(set(idx.example_files))
            )
            ordered = self._stable_unique(combined)
            log.info("Iteration %d → %d files (incl. docs/setup/examples).", iteration, len(ordered))
            return ordered

        # Iteration 1 or 2: code + tests only
        combined = sorted(set(idx.code_files)) + sorted(set(idx.test_files))
        ordered = self._stable_unique(combined)
        log.info("Iteration %d → %d files (code + tests).", iteration, len(ordered))
        return ordered

    # ------------------------ classification logic -------------------------- #
    def _classify(self, rel: str) -> str:
        """
        Classify a file path into broad categories: code, test, doc, setup, example.

        The decision uses directory hints, basenames, and extensions.
        """
        posix = rel  # already posix

        # Directory hints
        parts = posix.split("/")
        dirs = parts[:-1]
        base = parts[-1]
        stem, ext = os.path.splitext(base)
        ext = ext.lower()

        # Docs
        if ext in _DOC_EXTS:
            return "doc"
        if stem.upper() in _DOC_BASENAMES:
            return "doc"
        if "docs" in (d.lower() for d in dirs):
            # Treat files inside /docs as docs unless they are clear code (e.g., .py)
            if ext not in _TEXT_CODE_EXTS:
                return "doc"

        # Setup
        if base in _SETUP_BASENAMES:
            return "setup"
        for hint in _SETUP_DIR_HINTS:
            if posix.startswith(hint + "/") or f"/{hint}/" in posix:
                return "setup"

        # Examples
        if base in _EXAMPLE_BASENAMES:
            return "example"
        low_dirs = [d.lower() for d in dirs]
        if any(h in low_dirs for h in _EXAMPLE_HINTS):
            return "example"

        # Tests
        if any(h in low_dirs for h in _TEST_DIR_HINTS):
            return "test"
        if any(fnmatch.fnmatch(base, pat) for pat in _TEST_FILE_PATTERNS):
            return "test"

        # Code (by extension)
        if ext in _TEXT_CODE_EXTS:
            return "code"

        # Default bucket: 'other' (often text config). We'll treat as code later if textual.
        return "other"

    # ---------------------------- helpers ----------------------------------- #
    def _iter_files(self, root: Path) -> Iterable[Path]:
        """
        Yield files under *root*, respecting ignore rules.
        """
        for p in root.rglob("*"):
            # Skip directories
            if p.is_dir():
                if self._is_ignored_dir(p):
                    # Prune: skip walking into ignored directories
                    # rglob can't be pruned directly; we filter by path prefix.
                    pass
                continue
            # Skip non-regular files (symlinks etc. are allowed if they resolve to files)
            try:
                if not p.is_file():
                    continue
            except OSError:
                continue

            # Skip anything under ignored dirs
            if any(part in _IGNORE_DIRS for part in p.relative_to(root).parts):
                continue

            # Skip ignored file globs
            if any(fnmatch.fnmatch(p.name, pat) for pat in _IGNORE_FILE_GLOBS):
                continue

            # Never traverse into .git/
            if ".git" in p.parts:
                continue

            yield p

    def _is_ignored_dir(self, p: Path) -> bool:
        try:
            rel = p.relative_to(self.root)
        except Exception:
            return False
        return any(part in _IGNORE_DIRS for part in rel.parts)

    def _relposix(self, p: Path) -> str:
        return p.relative_to(self.root).as_posix()

    def _stable_unique(self, items: List[str]) -> List[str]:
        """
        Preserve first occurrence order while removing duplicates.
        """
        seen = set()
        out: List[str] = []
        for it in items:
            if it not in seen:
                out.append(it)
                seen.add(it)
        return out

    def _seems_binary(self, path: Path) -> bool:
        """
        Heuristic binary detection:
          • Extension short‑circuit (images, archives, media, etc.)
          • Otherwise read up to _SNIFF_BYTES and check for NUL bytes or
            excessive control characters.

        This is conservative; when in doubt, we return True (treat as binary).
        """
        ext = path.suffix.lower()
        if ext in _BINARY_EXTS:
            return True

        try:
            data = path.read_bytes()[:_SNIFF_BYTES]
        except Exception:
            # If unreadable, err on the safe side
            return True

        if not data:
            return False

        # Null byte → almost certainly binary
        if b"\x00" in data:
            return True

        # Control‑character density (excluding common whitespace)
        ctrl = sum(1 for b in data if b < 32 and b not in (9, 10, 13))
        ratio = ctrl / max(1, len(data))
        return ratio > 0.30  # >30% control → consider binary
