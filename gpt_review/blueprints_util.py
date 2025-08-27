#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
===============================================================================
GPT‑Review ▸ Blueprint Documents Utilities
===============================================================================

Purpose
-------
Provide a single, reusable place for handling the **four blueprint documents**:
    1) Whitepaper & Engineering Blueprint
    2) Build Guide
    3) Software Design Specifications (SDS)
    4) Project Code Files & Instructions

This module encapsulates:
    • Canonical file names and keys
    • Path resolution under `.gpt-review/blueprints/`
    • Existence checks and "what's missing"
    • Safe text reading and normalization
    • A compact, human-readable **summary** for prompts

Notes
-----
* **No Git side-effects** in this module: it does not stage/commit changes.
  The orchestrator (or API driver) remains responsible for writing via the
  patch pipeline and for committing one file per change.
* All paths are **repo-root relative** POSIX style for consistency.

Usage
-----
    from pathlib import Path
    from gpt_review.blueprints_util import (
        BLUEPRINT_KEYS, blueprint_paths, blueprints_exist, missing_blueprints,
        summarize_blueprints, ensure_blueprint_dir,
    )

    repo = Path("/path/to/repo")
    ensure_blueprint_dir(repo)
    if not blueprints_exist(repo):
        # Call your model to generate docs, then write them via apply_patch.
        pass

    summary = summarize_blueprints(repo, max_chars_per_doc=1500)

"""
from __future__ import annotations

from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Tuple

from gpt_review import get_logger

log = get_logger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Canonical keys, labels and file names
# ─────────────────────────────────────────────────────────────────────────────

# Stable keys used throughout the codebase and by the orchestrator/tooling.
BLUEPRINT_KEYS: Tuple[str, str, str, str] = (
    "whitepaper",
    "build_guide",
    "sds",
    "project_instructions",
)

# Human labels used in summaries and logs (keep short and clear).
BLUEPRINT_LABELS: Mapping[str, str] = {
    "whitepaper": "Whitepaper",
    "build_guide": "Build Guide",
    "sds": "SDS",
    "project_instructions": "Project Instructions",
}

# Canonical on-disk Markdown file names (under .gpt-review/blueprints/).
BLUEPRINT_FILENAMES: Mapping[str, str] = {
    "whitepaper": "WHITEPAPER.md",
    "build_guide": "BUILD_GUIDE.md",
    "sds": "SDS.md",
    "project_instructions": "PROJECT_INSTRUCTIONS.md",
}


# ─────────────────────────────────────────────────────────────────────────────
# Path helpers
# ─────────────────────────────────────────────────────────────────────────────
def blueprint_dir(repo: Path) -> Path:
    """
    Return the directory where blueprint documents live:
        <repo>/.gpt-review/blueprints
    """
    return (repo / ".gpt-review" / "blueprints").resolve()


def ensure_blueprint_dir(repo: Path) -> Path:
    """
    Ensure the blueprint directory exists (idempotent). Returns the path.
    """
    bp = blueprint_dir(repo)
    bp.mkdir(parents=True, exist_ok=True)
    return bp


def blueprint_paths(repo: Path) -> Dict[str, Path]:
    """
    Return a mapping {key → absolute Path} for all blueprint documents.
    """
    base = blueprint_dir(repo)
    return {k: (base / BLUEPRINT_FILENAMES[k]).resolve() for k in BLUEPRINT_KEYS}


def blueprints_exist(repo: Path) -> bool:
    """
    True iff **all** blueprint documents exist on disk.
    """
    paths = blueprint_paths(repo)
    exist = all(p.exists() for p in paths.values())
    log.debug("Blueprints exist=%s (%s)", exist, ", ".join(f"{k}={p.exists()}" for k, p in paths.items()))
    return exist


def missing_blueprints(repo: Path) -> List[str]:
    """
    Return a list of blueprint keys that do not exist yet.
    """
    paths = blueprint_paths(repo)
    missing = [k for k, p in paths.items() if not p.exists()]
    if missing:
        log.info("Missing blueprint documents: %s", ", ".join(missing))
    else:
        log.debug("No missing blueprint documents detected.")
    return missing


# ─────────────────────────────────────────────────────────────────────────────
# Text helpers
# ─────────────────────────────────────────────────────────────────────────────
def _read_text_safe(p: Path) -> str:
    """
    Read text with UTF‑8 and normalize EOL to LF. Returns '' on failure.
    """
    try:
        data = p.read_text(encoding="utf-8")
    except Exception:
        return ""
    # Normalize CRLF/CR to LF for downstream prompts/diffs.
    return data.replace("\r\n", "\n").replace("\r", "\n")


def normalize_markdown(text: str) -> str:
    """
    Normalize a Markdown payload for deterministic commits/prompts:

    * Convert CRLF/CR → LF
    * Ensure a single trailing newline (POSIX convention)
    """
    t = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    return t if t.endswith("\n") else t + "\n"


# ─────────────────────────────────────────────────────────────────────────────
# Prompt-friendly summary
# ─────────────────────────────────────────────────────────────────────────────
def summarize_blueprints(repo: Path, *, max_chars_per_doc: int = 1500) -> str:
    """
    Build a compact, concatenated summary of blueprint documents suitable for
    inclusion in prompts. Each section starts with an H2 header and contains
    either an abridged body or '<missing>'.

    Parameters
    ----------
    repo : Path
        Repository root.
    max_chars_per_doc : int
        Hard cap per document; the text is trimmed to this many characters.

    Returns
    -------
    str
        Concatenated Markdown sections. Example:

            ## Whitepaper
            (trimmed contents…)

            ## Build Guide
            <missing>
    """
    paths = blueprint_paths(repo)
    parts: List[str] = []
    for key in BLUEPRINT_KEYS:
        label = BLUEPRINT_LABELS[key]
        path = paths[key]
        body = _read_text_safe(path).strip()
        if not body:
            parts.append(f"## {label}\n<missing>\n")
            continue
        trimmed = body if len(body) <= max_chars_per_doc else (body[:max_chars_per_doc] + "\n…\n")
        parts.append(f"## {label}\n{trimmed}\n")

    summary = "\n".join(parts).strip()
    log.debug("Prepared blueprints summary (%d chars).", len(summary))
    return summary


# ─────────────────────────────────────────────────────────────────────────────
# Utility helpers for external callers
# ─────────────────────────────────────────────────────────────────────────────
def to_posix_paths(paths: Mapping[str, Path]) -> Dict[str, str]:
    """
    Convert {key → Path} into {key → 'posix/relative/path'} for tool payloads.

    The returned paths are **relative to the repo root** and use forward slashes.
    """
    result: Dict[str, str] = {}
    for key, p in paths.items():
        try:
            # Attempt to make the path repo‑relative by trimming up to '.gpt-review'.
            # Callers usually need the *relative* POSIX path for patch operations.
            rel = p
            # Find the repo root by walking up until we see '.git' or root.
            cur = p
            root = None
            while cur != cur.parent:
                if (cur / ".git").exists():
                    root = cur
                    break
                cur = cur.parent
            if root:
                rel = p.relative_to(root)
            result[key] = rel.as_posix()
        except Exception:
            # Fall back to a POSIX string; better than raising in helpers.
            result[key] = p.as_posix()
    return result


def validate_docs_payload(docs: Mapping[str, str]) -> List[str]:
    """
    Validate a docs payload resembles the expected shape for the four blueprints.
    Returns a list of **problems** (empty list means OK).
    """
    problems: List[str] = []
    for k in BLUEPRINT_KEYS:
        if k not in docs:
            problems.append(f"missing key: {k}")
        else:
            v = docs[k]
            if not isinstance(v, str) or not v.strip():
                problems.append(f"empty or non‑string value for key: {k}")
    unexpected = [k for k in docs.keys() if k not in BLUEPRINT_KEYS]
    if unexpected:
        problems.append(f"unexpected keys: {', '.join(sorted(unexpected))}")
    return problems
