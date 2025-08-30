#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
===============================================================================
GPT‑Review ▸ File Scanner (compatibility shim over RepoScanner)
===============================================================================

Why this file?
--------------
Some parts of the system (e.g., `gpt_review.workflow`) import a module named
`gpt_review.file_scanner` that exposes a small facade:

    • RepoScan dataclass (lightweight manifest with helpful groupings)
    • scan_repository(repo, ignores=...) -> RepoScan
    • classify_for_iteration(scan, iteration) -> list[str]
    • RepoScan.manifest_text() -> str

Historically we also exposed utility helpers such as:
    • classify_path(repo, rel_path) -> Category
    • read_text_file(repo, rel_posix) -> str (LF-normalized)
    • languages_present(repo) -> list[(language, count)]

The project now includes a richer scanner at `gpt_review.repo_scanner.RepoScanner`.
To avoid churn and keep backward compatibility, this module wraps the newer
scanner behind the old facade and re‑implements the helpers using its index.

Behavioral contract (aligned with requirements)
-----------------------------------------------
• Iterations 1–2: operate on **code + tests** only; defer docs/setup/examples.
• Iteration 3    : include **all** non‑binary files (docs/setup/examples too).
• All paths are **repo‑relative POSIX**; binary files are **excluded** from
  iteration lists (binary creation/updates still possible via body_b64).
• Pure read‑only: this module never mutates the repository.

Logging
-------
INFO for summary counts and iteration selections; DEBUG for detailed decisions.
"""
from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field
from enum import Enum, auto
from pathlib import Path
from typing import List, Sequence, Tuple

from gpt_review import get_logger

# Prefer the robust underlying scanner if available; otherwise fall back.
try:  # pragma: no cover - import availability depends on installation mode
    from gpt_review.repo_scanner import RepoScanner  # type: ignore
    _HAVE_REPO_SCANNER = True
except Exception:  # pragma: no cover
    RepoScanner = None  # type: ignore[assignment]
    _HAVE_REPO_SCANNER = False

# Reuse shared helpers from fs_utils for fallback paths
from gpt_review.fs_utils import (
    is_binary_file as _is_binary_file,
    classify_paths as _classify_paths,
)

log = get_logger(__name__)


# =============================================================================
# Categories (kept for backwards compatibility with earlier caller code)
# =============================================================================
class Category(Enum):
    CODE = auto()
    TEST = auto()
    DOCS = auto()
    INSTALL = auto()   # historically separate; we alias into SETUP where needed
    SETUP = auto()
    EXAMPLE = auto()
    DATA = auto()
    BINARY = auto()
    UNKNOWN = auto()


# =============================================================================
# Public datamodel
# =============================================================================
@dataclass
class RepoScan:
    """
    Lightweight manifest returned by `scan_repository`.

    Attributes
    ----------
    root : Path
        Absolute repository root.
    all_files : list[str]
        All **non‑binary** tracked files (POSIX‑relative).
    code_and_config : list[str]
        Files to process during iterations 1 & 2 (code + tests; non‑binary).
    docs_and_extras : list[str]
        Deferred class: docs/setup/examples (iteration 3 only; non‑binary).
    """
    root: Path
    all_files: List[str] = field(default_factory=list)
    code_and_config: List[str] = field(default_factory=list)
    docs_and_extras: List[str] = field(default_factory=list)

    def manifest_text(self, max_lines: int = 400) -> str:
        """
        Return a concise, human‑readable manifest for prompts/logs.
        """
        total = len(self.all_files)
        lines = self.all_files
        if total > max_lines:
            half = max_lines // 2
            head = "\n".join(lines[:half])
            tail = "\n".join(lines[-half:])
            body = f"{head}\n…\n{tail}"
        else:
            body = "\n".join(lines)

        summary = (
            f"{total} files "
            f"(iter12={len(self.code_and_config)}, deferred={len(self.docs_and_extras)})"
        )
        return f"{summary}\n{body}"


# =============================================================================
# Facade functions
# =============================================================================
def scan_repository(repo_root: Path, *, ignores: Sequence[str] | None = None) -> RepoScan:
    """
    Walk the repository and build a `RepoScan` manifest.

    Parameters
    ----------
    repo_root : Path
        Repository root (must contain `.git`).
    ignores : Sequence[str] | None
        Accepted for compatibility with older callers. The underlying
        `RepoScanner` already applies robust ignore rules. Values here
        are ignored intentionally to keep behaviour deterministic.

    Returns
    -------
    RepoScan
        A deterministic, non‑binary manifest suitable for iteration logic.
    """
    if ignores:  # keep users aware without changing behaviour
        log.debug("scan_repository: 'ignores' parameter is accepted but unused (%s)", list(ignores))

    root = Path(repo_root).expanduser().resolve()

    # Preferred path: rich RepoScanner API
    if _HAVE_REPO_SCANNER:
        try:
            scanner = RepoScanner(root)  # type: ignore[call-arg]
            idx = scanner.scan()
            binaries = set(idx.binary_files)
            docs = set(idx.docs_files)
            setup = set(idx.setup_files)
            examples = set(idx.example_files)
            deferred = sorted((docs | setup | examples) - binaries)
            code = sorted(set(idx.code_files) - binaries)
            tests = sorted(set(idx.test_files) - binaries)
            non_binary_all = sorted(set(idx.all_files) - binaries)
            manifest = RepoScan(
                root=root,
                all_files=_stable_unique(non_binary_all),
                code_and_config=_stable_unique(code + tests),
                docs_and_extras=_stable_unique(deferred),
            )
            log.info(
                "Scanned repo (RepoScanner): total=%s non‑binary=%s iter12=%s deferred=%s",
                len(idx.all_files),
                len(manifest.all_files),
                len(manifest.code_and_config),
                len(manifest.docs_and_extras),
            )
            return manifest
        except Exception as exc:
            log.warning("RepoScanner unavailable or failed (%s); falling back to fs_utils.", exc)

    # Fallback path: reuse fs_utils classification + simple globs for sub‑classes
    code_like, deferred_paths = _classify_paths(root)
    rel_code = [p.relative_to(root).as_posix() for p in code_like]
    rel_deferred = [p.relative_to(root).as_posix() for p in deferred_paths]

    tests = [rp for rp in rel_code if _matches_any(rp, _TEST_GLOBS)]
    code = [rp for rp in rel_code if rp not in tests]
    # Split deferred between docs/setup/examples (best‑effort)
    docs = [rp for rp in rel_deferred if _matches_any(rp, _DOC_GLOBS)]
    setup = [rp for rp in rel_deferred if _matches_any(rp, _SETUP_GLOBS) or _matches_any(rp, _INSTALL_GLOBS)]
    examples = [rp for rp in rel_deferred if _matches_any(rp, _EXAMPLE_GLOBS)]
    # Union for ordering (allow overlaps to appear once)
    deferred = _stable_unique(docs + setup + examples)

    non_binary_all = _stable_unique(rel_code + rel_deferred)

    manifest = RepoScan(
        root=root,
        all_files=non_binary_all,
        code_and_config=_stable_unique(code + tests),
        docs_and_extras=deferred,
    )
    log.info(
        "Scanned repo (fallback): non‑binary=%s iter12=%s deferred=%s",
        len(manifest.all_files),
        len(manifest.code_and_config),
        len(manifest.docs_and_extras),
    )
    return manifest


def classify_for_iteration(scan: RepoScan, *, iteration: int) -> List[str]:
    """
    Return the ordered list of files to process for the given *iteration*.

    • Iterations 1–2 → code & tests only (scan.code_and_config)
    • Iteration 3    → **all** non‑binary files, including docs/setup/examples
    """
    if iteration >= 3:
        ordered = _stable_unique(scan.code_and_config + scan.docs_and_extras)
        log.info("Iteration %d → %d files (incl. docs/setup/examples).", iteration, len(ordered))
        return ordered

    ordered = list(scan.code_and_config)
    log.info("Iteration %d → %d files (code + tests).", iteration, len(ordered))
    return ordered


# =============================================================================
# Back‑compat helpers (mirror earlier API surface)
# =============================================================================
# Minimal language map (kept local to avoid tight coupling)
_LANG_BY_EXT = {
    ".py": "python", ".pyi": "python",
    ".js": "javascript", ".cjs": "javascript", ".mjs": "javascript",
    ".ts": "typescript", ".tsx": "typescript", ".jsx": "javascript",
    ".sh": "shell", ".bash": "shell",
    ".json": "json", ".yaml": "yaml", ".yml": "yaml",
    ".toml": "toml", ".ini": "ini", ".cfg": "ini", ".conf": "ini", ".txt": "text",
    ".md": "markdown", ".rst": "rst",
    ".go": "go", ".rs": "rust", ".java": "java", ".kt": "kotlin",
    ".c": "c", ".h": "c", ".cpp": "cpp", ".hpp": "cpp",
    ".cs": "csharp", ".php": "php", ".rb": "ruby", ".swift": "swift",
}

# Patterns broadly aligned with prior classifier (used only for fallback)
_DOC_GLOBS: Tuple[str, ...] = (
    "README.*", "CHANGELOG.*", "CONTRIBUTING.*", "LICENSE*",
    "docs/**", "*.md", "*.rst",
)
_INSTALL_GLOBS: Tuple[str, ...] = (
    "install.sh", "update.sh", "cookie_login.sh", "software_review.sh",
    "scripts/install*", "scripts/setup*", "Dockerfile", "docker/**",
    ".github/workflows/**",
)
_SETUP_GLOBS: Tuple[str, ...] = (
    "pyproject.toml", "setup.cfg", "setup.py",
    "requirements*.txt", "Pipfile", "poetry.lock",
    ".flake8", ".editorconfig", ".pre-commit-config.yaml", ".pre-commit-config.yml",
)
_EXAMPLE_GLOBS: Tuple[str, ...] = (
    "examples/**", "example/**", "example_*", "example*.*",
    "docs/examples/**", "example_instructions.txt",
)
_TEST_GLOBS: Tuple[str, ...] = ("tests/**", "test_*.*", "*_test.*")


def _matches_any(path: str, patterns: Sequence[str]) -> bool:
    """
    fnmatch does not treat '**' as "any directories", so we treat any pattern
    containing '/**' as a simple prefix rule:
        'docs/**'              → path startswith 'docs/'
        'docs/examples/**'     → path startswith 'docs/examples/'
    All other patterns are evaluated with fnmatch.
    """
    for pat in patterns:
        if "/**" in pat:
            prefix = pat.split("/**", 1)[0]
            # Ensure prefix match aligns to a path segment boundary
            if prefix and (path == prefix or path.startswith(prefix.rstrip("/") + "/")):
                return True
        else:
            if fnmatch.fnmatch(path, pat):
                return True
    return False


def classify_path(repo: Path, rel_path: Path | str) -> Category:
    """
    Classify a single *repo‑relative* path into a broad Category.

    Implementation:
    • Prefer the fresh `RepoScanner` index; if the file appears in its category
      lists we return that category (INSTALL is merged into SETUP).
    • If not present (e.g., untracked/new) or the scanner is unavailable,
      fall back to earlier glob/extension heuristics to keep behavior stable.
    """
    root = Path(repo).expanduser().resolve()
    rel = Path(rel_path).as_posix()

    if _HAVE_REPO_SCANNER:
        try:
            idx = RepoScanner(root).scan()  # type: ignore[call-arg]
            if rel in getattr(idx, "binary_files", []):
                return Category.BINARY
            if rel in getattr(idx, "test_files", []):
                return Category.TEST
            if rel in getattr(idx, "code_files", []):
                return Category.CODE
            if rel in getattr(idx, "docs_files", []):
                return Category.DOCS
            if rel in getattr(idx, "example_files", []):
                return Category.EXAMPLE
            if rel in getattr(idx, "setup_files", []):
                return Category.SETUP  # INSTALL merged into SETUP
        except Exception:
            # Indexing failed; continue to heuristic fallback.
            pass

    # Heuristic fallback (similar to earlier implementation)
    if _is_binary_file(root / rel):
        return Category.BINARY

    p = Path(rel)
    ext = p.suffix.lower()

    if _matches_any(rel, _DOC_GLOBS):
        return Category.DOCS
    # Align with documented behavior: treat "install" as SETUP for callers.
    if _matches_any(rel, _INSTALL_GLOBS):
        return Category.SETUP
    if _matches_any(rel, _SETUP_GLOBS):
        return Category.SETUP
    if _matches_any(rel, _EXAMPLE_GLOBS):
        return Category.EXAMPLE
    if _matches_any(rel, _TEST_GLOBS):
        return Category.TEST

    if ext in {".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf", ".txt"}:
        return Category.DATA
    if ext in _LANG_BY_EXT:
        return Category.CODE

    return Category.UNKNOWN


def read_text_file(repo: Path, rel_posix: str, *, max_bytes: int = 1024 * 1024) -> str:
    """
    Read a text file relative to *repo* with LF normalization.
    Raises ValueError for files that appear binary.
    """
    p = (Path(repo).expanduser().resolve() / rel_posix).resolve()
    if not p.exists() or not p.is_file():
        raise FileNotFoundError(rel_posix)
    if _is_binary_file(p):
        raise ValueError(f"Refusing to read binary file as text: {rel_posix}")

    # Defensive size guard to avoid oversized prompt payloads.
    data = p.read_bytes()[:max_bytes]
    text = data.decode("utf-8", errors="replace")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if not text.endswith("\n"):
        text += "\n"

    log.debug("read_text_file: %s (%d bytes → %d chars normalized).", rel_posix, len(data), len(text))
    return text


def languages_present(repo: Path) -> List[Tuple[str, int]]:
    """
    Return a list of (language, file_count) pairs sorted by count desc,
    considering only CODE and TEST categories.
    """
    root = Path(repo).expanduser().resolve()
    if _HAVE_REPO_SCANNER:
        try:
            idx = RepoScanner(root).scan()  # type: ignore[call-arg]
            rels = list(getattr(idx, "code_files", [])) + list(getattr(idx, "test_files", []))
        except Exception:
            rels = []
    else:
        # Fallback: classify_paths returns non‑binary “code‑like” + deferred; treat code‑like as code/tests.
        code_like, _ = _classify_paths(root)
        rels = [p.relative_to(root).as_posix() for p in code_like]

    counts: dict[str, int] = {}
    for rel in rels:
        lang = _LANG_BY_EXT.get(Path(rel).suffix.lower())
        if lang:
            counts[lang] = counts.get(lang, 0) + 1
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    log.info("Language census: %s", ", ".join(f"{k}:{v}" for k, v in ranked) or "<none>")
    return ranked


# =============================================================================
# Small helpers
# =============================================================================
def _stable_unique(items: Sequence[str]) -> List[str]:
    """
    Preserve first‑occurrence order while removing duplicates.
    """
    seen: set[str] = set()
    out: List[str] = []
    for it in items:
        if it not in seen:
            out.append(it)
            seen.add(it)
    return out


# =============================================================================
# __all__
# =============================================================================
__all__ = [
    "Category",
    "RepoScan",
    "scan_repository",
    "classify_for_iteration",
    # back‑compat helpers
    "classify_path",
    "read_text_file",
    "languages_present",
]
