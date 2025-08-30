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

This module never mutates the repo; it only inspects it. Writing happens
through the existing patch applier (apply_patch.py) to preserve invariants
like **path‑scoped staging**, normalization, and safety checks.

Key features
------------
* Fast recursive walk with pruning of heavy directories (e.g., .git/, venv/, node_modules/)
* Robust binary sniff (NUL‑byte / control‑density heuristic)
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
    ".idea", ".vscode", ".cache", "logs", "docker-build",
    "venv", ".venv", "env",
}

# File globs to ignore (heavy artifacts, coverage, local state, etc.)
_IGNORE_FILE_GLOBS: Set[str] = {
    "*.pyc", "*.pyo", "*.pyd", "*.so", "*.dylib",
    "*.exe", "*.dll", "*.obj", "*.a", "*.o",
    "*.class", "*.jar",
    "*.log", "*.tmp", "*.swp", "*.swo", "*~",
    ".coverage", "coverage.xml",
    ".DS_Store", "Thumbs.db",
}

# Docs / prose
_DOC_EXTS: Set[str] = {".md", ".rst", ".adoc", ".txt"}
_DOC_BASENAMES: Set[str] = {
    "README", "CHANGELOG", "CONTRIBUTING", "LICENSE", "SECURITY",
    "CODE_OF_CONDUCT", "CODE-OF-CONDUCT",
}
# Common doc trees (aligned with fs_utils; include blueprint dir)
_DOC_DIR_HINTS: Set[str] = {
    "docs", "doc", "documentation", "guides", "mkdocs", "site", "book", ".gpt-review"
}

# Setup / install / CI
_SETUP_BASENAMES: Set[str] = {
    "setup.py", "pyproject.toml", "requirements.txt", "requirements-dev.txt",
    "dev-requirements.txt", "Pipfile", "Pipfile.lock", "poetry.lock",
    "Makefile", "Dockerfile", "docker-compose.yml", "docker-compose.yaml",
    "install.sh", "update.sh", "software_review.sh", "cookie_login.sh",
    ".pre-commit-config.yaml", ".pre-commit-config.yml",
    "MANIFEST.in", ".flake8", ".editorconfig",
    "package.json", "package-lock.json", "yarn.lock", "pnpm-lock.yaml",
    ".gitlab-ci.yml", "azure-pipelines.yml",
}
# Globs for families of setup files (kept small & explicit)
_SETUP_FILE_GLOBS: Set[str] = {"requirements*.txt"}

_SETUP_DIR_HINTS: Set[str] = {".github/workflows", ".github/actions", "ci", ".ci"}

# Example assets & prompts
_EXAMPLE_HINTS: Set[str] = {"examples", "example", "sample", "samples"}
_EXAMPLE_BASENAMES: Set[str] = {"example_instructions.txt"}

# Tests
_TEST_DIR_HINTS: Set[str] = {"tests", "test", "spec", "specs"}
_TEST_FILE_PATTERNS: Set[str] = {
    "test_*.py", "*_test.py",
    "*.spec.js", "*.spec.ts",
    "*_test.go", "*_test.rs", "*_test.rb", "*_test.ts", "*_test.js",
    "*_spec.rb",
}

# File types likely textual code/config (still validated via binary sniff)
_TEXT_CODE_EXTS: Set[str] = {
    # Languages
    ".py", ".pyi", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs",
    ".go", ".rb", ".rs", ".c", ".cc", ".cpp", ".h", ".hpp",
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
        files: List[Path] = [p for p in self._iter_files(self.root)]

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
                # Treat tests as code for iteration grouping (dedup later).
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

        Binary files are **always excluded** from these iteration lists.
        """
        if not hasattr(self, "_cached_index"):
            self._cached_index = self.scan()  # type: ignore[attr-defined]

        idx: RepoIndex = self._cached_index  # type: ignore[assignment]
        bset = set(idx.binary_files)

        def _stable_unique(seq: List[str]) -> List[str]:
            seen: set[str] = set()
            out: List[str] = []
            for s in seq:
                if s in bset:
                    continue  # drop binaries from iteration lists
                if s not in seen:
                    out.append(s)
                    seen.add(s)
            return out

        if iteration >= 3:
            combined = (
                idx.code_files + idx.test_files + idx.docs_files + idx.setup_files + idx.example_files
            )
            ordered = _stable_unique(combined)
            log.info(
                "Iteration %d → %d files (incl. docs/setup/examples; binaries excluded).",
                iteration, len(ordered)
            )
            return ordered

        combined = idx.code_files + idx.test_files
        ordered = _stable_unique(combined)
        log.info(
            "Iteration %d → %d files (code + tests; binaries excluded).",
            iteration, len(ordered)
        )
        return ordered

    # ------------------------ classification logic -------------------------- #
    def _classify(self, rel: str) -> str:
        """
        Classify a file path into broad categories: code, test, doc, setup, example.

        Precedence notes
        ----------------
        • Explicit example/test/setup basenames/dirs win over generic doc-by-extension
          (so e.g. 'example_instructions.txt' is classified as example, not doc).
        """
        posix = rel  # already posix

        # Directory + basename features
        parts = posix.split("/")
        dirs = parts[:-1]
        base = parts[-1]
        stem, ext = os.path.splitext(base)
        ext = ext.lower()
        low_dirs = [d.lower() for d in dirs]

        # --- Setup / CI (basenames, small glob family, and directory hints) ---
        if base in _SETUP_BASENAMES or any(fnmatch.fnmatch(base, pat) for pat in _SETUP_FILE_GLOBS):
            return "setup"
        for hint in _SETUP_DIR_HINTS:
            if posix.startswith(hint + "/") or f"/{hint}/" in posix:
                return "setup"

        # --- Tests (dir hints or filename patterns) ---
        if any(h in low_dirs for h in _TEST_DIR_HINTS):
            return "test"
        if any(fnmatch.fnmatch(base, pat) for pat in _TEST_FILE_PATTERNS):
            return "test"

        # --- Examples (explicit basenames or directory hints) ---
        if base in _EXAMPLE_BASENAMES:
            return "example"
        if any(h in low_dirs for h in _EXAMPLE_HINTS):
            return "example"

        # --- Docs (extensions, basenames, doc directory hints) ---
        if ext in _DOC_EXTS:
            return "doc"
        if stem.upper() in _DOC_BASENAMES:
            return "doc"
        if any(d.lower() in _DOC_DIR_HINTS for d in dirs):
            # Treat files inside docs/doc trees as docs unless they are clear code (e.g., .py)
            if ext not in _TEXT_CODE_EXTS:
                return "doc"

        # --- Code (by extension) ---
        if ext in _TEXT_CODE_EXTS:
            return "code"

        # Default bucket: 'other' (often text config). We'll treat as code later if textual.
        return "other"

    # ---------------------------- helpers ----------------------------------- #
    def _iter_files(self, root: Path) -> Iterable[Path]:
        """
        Yield files under *root*, **pruning** ignored directories to avoid
        traversing heavyweight trees (e.g., node_modules).
        """
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            # Compute rel parts once
            try:
                rel_parts = Path(dirpath).resolve().relative_to(root).parts
            except Exception:
                rel_parts = ()

            # Prune ignored directories in-place (prevents descent)
            dirnames[:] = [d for d in dirnames if d not in _IGNORE_DIRS and d != ".git"]
            # If current path is already inside an ignored dir, skip entirely
            if any(part in _IGNORE_DIRS for part in rel_parts) or ".git" in rel_parts:
                continue

            for name in filenames:
                if any(fnmatch.fnmatch(name, pat) for pat in _IGNORE_FILE_GLOBS):
                    continue
                p = Path(dirpath) / name
                # Skip non-regular files defensively
                try:
                    if not p.is_file():
                        continue
                except OSError:
                    continue
                yield p

    def _relposix(self, p: Path) -> str:
        return p.relative_to(self.root).as_posix()

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
