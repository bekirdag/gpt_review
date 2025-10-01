#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
===============================================================================
GPT‑Review ▸ Full‑File API Driver (propose complete file replacements)
===============================================================================

Purpose
-------
For a given file (path + bytes), call the GPT-Codex API and obtain a
**single, decisive action**:

  • keep              – no change required
  • update            – full new text content (UTF‑8)
  • update_binary     – full new bytes (Base64)
  • create            – new file with full text (UTF‑8)
  • create_binary     – new file with bytes (Base64)
  • delete            – remove the file

The result is returned as a structured `FullFileDecision` and can be converted
to a **single JSON patch** compatible with our existing, battle‑tested
`apply_patch.py` safety layer (path‑scoped staging, .git guard, normalization).

Why "full file"?
----------------
Your requirement states the assistant must output **complete files** instead of
diffs/patches to avoid partial, inconsistent edits. This driver enforces that
via a tool call schema the model must return.

Integration points
------------------
* Orchestrator:
    - Enumerate files via `RepoScanner` (code/tests for Iteration 1–2).
    - For each file, call `review_file_with_api(...)`.
    - Convert the decision to a JSON patch via `decision_to_patch(...)`.
    - Apply with `apply_patch.py` (subprocess) to preserve invariants.
* Error‑fix loop:
    - Send failing logs; ask the model which files to replace; produce decisions.

Environment & defaults
-----------------------
* `GPT_CODEX_API_KEY`           – required (unless a client is injected). Falls back to `OPENAI_API_KEY`.
* `GPT_CODEX_BASE_URL`          – optional custom endpoint (aliases: `GPT_CODEX_API_BASE`, `OPENAI_BASE_URL`, `OPENAI_API_BASE`).
* `GPT_REVIEW_MODEL`            – default model name (e.g., "gpt-5-codex")
* `GPT_REVIEW_API_TIMEOUT`      – per‑request timeout (seconds; default 120)
* `GPT_REVIEW_MAX_PROMPT_BYTES` – truncate text prompts with head+tail when larger (default 200_000)
* `GPT_REVIEW_HEAD_TAIL_BYTES`  – bytes for head and tail slices (default 60_000)

Notes
-----
This module **does not** mutate the repository.
"""
from __future__ import annotations

import base64
import json
import os
import textwrap
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Dict, Optional

from gpt_review import get_logger
from gpt_review.codex_client import (
    create_client as create_codex_client,
    resolve_api_key as resolve_codex_api_key,
)

log = get_logger(__name__)

# --------------------------------------------------------------------------- #
# Env‑backed defaults (aligned with orchestrator/api_driver)
# --------------------------------------------------------------------------- #
DEFAULT_MODEL = os.getenv("GPT_REVIEW_MODEL", "gpt-5-codex")
DEFAULT_API_TIMEOUT = int(os.getenv("GPT_REVIEW_API_TIMEOUT", "120"))
MAX_PROMPT_BYTES = int(os.getenv("GPT_REVIEW_MAX_PROMPT_BYTES", str(200_000)))
HEAD_TAIL_BYTES = int(os.getenv("GPT_REVIEW_HEAD_TAIL_BYTES", str(60_000)))

# --------------------------------------------------------------------------- #
# Result type
# --------------------------------------------------------------------------- #
@dataclass
class FullFileDecision:
    """
    The model's decision for a single file.

    Fields
    ------
    path: str
        Repo‑relative POSIX path.
    action: str
        One of {"keep","update","update_binary","create","create_binary","delete"}.
    reason: Optional[str]
        Short natural‑language rationale (useful for commit messages).
    content: Optional[str]
        Full UTF‑8 text when action ∈ {"update","create"}.
    content_b64: Optional[str]
        Base64 bytes when action ∈ {"update_binary","create_binary"}.
    """
    path: str
    action: str
    reason: Optional[str] = None
    content: Optional[str] = None
    content_b64: Optional[str] = None


# --------------------------------------------------------------------------- #
# Tool schema – forces a clear, machine‑readable reply
# --------------------------------------------------------------------------- #
def _propose_fullfile_tool() -> Dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": "propose_fullfile",
            "description": (
                "Return a decisive action for EXACTLY ONE file. "
                "If changing, provide the FULL replacement content."
            ),
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Repo‑relative POSIX path for the file under review."
                    },
                    "action": {
                        "type": "string",
                        "enum": ["keep", "update", "update_binary", "create", "create_binary", "delete"],
                        "description": "Choose exactly one."
                    },
                    "reason": {
                        "type": "string",
                        "description": "Short explanation. One sentence is sufficient."
                    },
                    "content": {
                        "type": "string",
                        "description": "Required IFF action ∈ {update, create}. Full UTF‑8 text."
                    },
                    "content_b64": {
                        "type": "string",
                        "description": "Required IFF action ∈ {update_binary, create_binary}. Base64 bytes."
                    }
                },
                "required": ["path", "action"]
            },
        },
    }


# --------------------------------------------------------------------------- #
# Iteration deferral patterns (docs/setup/examples/CI)
# --------------------------------------------------------------------------- #
_DOC_EXTS = {".md", ".rst", ".adoc", ".txt"}
_SETUP_BASENAMES = {
    "setup.py", "pyproject.toml", "requirements.txt", "Pipfile", "Pipfile.lock",
    "poetry.lock", "Makefile", "Dockerfile", "docker-compose.yml", "docker-compose.yaml",
    ".pre-commit-config.yaml", ".pre-commit-config.yml",
    "install.sh", "update.sh", "software_review.sh", "cookie_login.sh",
}
# Include both single‑segment directory hints (e.g., "docs") and composite
# subpaths (e.g., ".github/workflows") which require substring checks.
_DEFER_DIR_HINTS = {
    "docs", "doc", "examples", "example", "ci", ".ci",
    ".github/workflows", ".github/actions",
}


def _path_deferred_before_iter3(rel: str) -> bool:
    """
    True if *rel* looks like docs/setup/examples/CI that we defer until iteration 3.
    """
    p = PurePosixPath(rel)
    # Extension-based docs
    if p.suffix.lower() in _DOC_EXTS:
        return True
    # Specific setup/install basenames
    if p.name in _SETUP_BASENAMES:
        return True

    # Directory hints
    parts = [seg.lower() for seg in p.parts[:-1]]
    posix_lower = p.as_posix().lower()

    # Single‑segment directory names
    for hint in (h for h in _DEFER_DIR_HINTS if "/" not in h):
        if hint in parts:
            return True

    # Composite subpaths (contain '/'): match by substring/prefix within the path
    for hint in (h for h in _DEFER_DIR_HINTS if "/" in h):
        if posix_lower.startswith(hint + "/") or f"/{hint}/" in posix_lower:
            return True

    return False


# --------------------------------------------------------------------------- #
# Helpers: path guard & excerpting
# --------------------------------------------------------------------------- #
def _is_safe_repo_rel_posix(path: str) -> bool:
    """
    Defensive path guard:
      - POSIX separators only; not absolute; no backslashes; no '..'
      - not under '.git/' and not '.git' itself; no empty segments; not '.'
    """
    if not isinstance(path, str) or not path.strip():
        return False
    if path in {".", "./"} or path.endswith("/"):
        return False
    if "\\" in path or path.startswith("/"):
        return False
    if path == ".git" or path.startswith(".git/") or "/.git/" in path:
        return False
    if ".." in path.split("/"):
        return False
    p = PurePosixPath(path)
    return str(p) == path and all(seg for seg in p.parts)


def _excerpt_bytes_to_text(data: bytes) -> str:
    """
    Convert bytes to UTF‑8 text (replace errors), truncating with head+tail marker
    when larger than MAX_PROMPT_BYTES. Mirrors orchestrator behaviour.
    """
    if len(data) <= MAX_PROMPT_BYTES:
        return data.decode("utf-8", errors="replace")
    head = data[:HEAD_TAIL_BYTES].decode("utf-8", errors="replace")
    tail = data[-HEAD_TAIL_BYTES:].decode("utf-8", errors="replace")
    return f"<<EXCERPT: file too large ({len(data)} bytes); sending head+tail>>\n{head}\n…\n{tail}"


# --------------------------------------------------------------------------- #
# Prompts
# --------------------------------------------------------------------------- #
def _system_prompt(iteration: int) -> str:
    """
    Strict, iteration‑aware instruction. Keep this compact to reduce tokens.
    """
    return (
        "You are GPT‑Review, a software reviewer/refactorer.\n"
        "Respond ONLY by calling the function `propose_fullfile` for the file provided.\n"
        "Rules:\n"
        "  1) For any change you MUST return a **COMPLETE FILE** (no diffs/patches). "
        "Use `content` for text or `content_b64` for binary.\n"
        "  2) Keep changes minimal and behavior‑preserving unless the instructions demand otherwise.\n"
        "  3) If no change is necessary, choose action='keep'.\n"
        "  4) Use 'update_binary'/'create_binary' ONLY for non‑text/binary content.\n"
        "  5) Iteration gates:\n"
        "     - Iterations 1–2: focus on code/tests; do NOT introduce or modify docs/setup/examples/CI.\n"
        "     - Iteration 3   : ensure global consistency; THEN docs/install/setup/examples may change.\n"
        f"Current iteration: {iteration}.\n"
    )


def _language_hint_for_path(path: str) -> Optional[str]:
    ext = Path(path).suffix.lower()
    return {
        ".py": "python",
        ".js": "javascript",
        ".ts": "typescript",
        ".tsx": "tsx",
        ".jsx": "jsx",
        ".mjs": "javascript",
        ".cjs": "javascript",
        ".sh": "bash",
        ".bash": "bash",
        ".zsh": "bash",
        ".toml": "toml",
        ".yaml": "yaml",
        ".yml": "yaml",
        ".json": "json",
        ".ini": "ini",
        ".cfg": "ini",
        ".md": "markdown",
        ".rst": "restructuredtext",
        ".html": "html",
        ".css": "css",
        ".scss": "scss",
        ".go": "go",
        ".rb": "ruby",
        ".rs": "rust",
        ".java": "java",
        ".kt": "kotlin",
        ".c": "c",
        ".cc": "c++",
        ".cpp": "c++",
        ".h": "c/c++ header",
        ".hpp": "c++ header",
    }.get(ext)


def _build_user_prompt(
    *,
    repo_root: Path,
    path: str,
    content_preview: str,
    is_binary: bool,
    language_hint: Optional[str],
    global_instructions: str,
    iteration: int,
) -> str:
    """
    Compose a single, compact user message that carries all necessary context.
    """
    lang_note = f" (language: {language_hint})" if language_hint else ""
    kind = "binary" if is_binary else "text"
    rules = (
        "You will review ONE file and either keep it or return a FULL replacement.\n"
        "Do NOT reply with prose. You must call `propose_fullfile`.\n"
        "Honor existing public APIs, imports, and behavior unless the objective requires a change.\n"
        "Small, targeted, well‑commented code preferred.\n"
    )
    iteration_goals = (
        "Iteration goals:\n"
        "  • Iters 1–2: refactor/modernize code & tests only; do not touch docs/setup/examples/CI.\n"
        "  • Iter 3   : finalized cross‑file consistency; then docs/setup/examples may be updated.\n"
    )
    safety = (
        "Safety:\n"
        "  • If the file is already good, choose action='keep'.\n"
        "  • If changing a textual file, provide `content` (UTF‑8) for the FULL file.\n"
        "  • If changing a binary file, provide `content_b64` and use update_binary/create_binary.\n"
    )
    header = textwrap.dedent(
        f"""\
        Objective
        ---------
        {global_instructions.strip()}

        Repository
        ----------
        Root: {repo_root}

        File under review{lang_note}
        ----------------------------
        Path     : {path}
        Detected : {kind}

        Rules
        -----
        {rules}{iteration_goals}{safety}
        """
    )

    if is_binary:
        # Never include raw binary bytes in the prompt; we only tell the model it's binary.
        body = "Binary content omitted in prompt. If you decide to change it, return Base64 bytes."
    else:
        # Provide the FULL textual content (possibly excerpted with head+tail).
        body = f"```\n{content_preview}\n```"

    return header + "\nCurrent content:\n" + body


# --------------------------------------------------------------------------- #
# GPT-Codex client (lazy)
# --------------------------------------------------------------------------- #
def _ensure_client(client: Any | None, api_timeout: int):
    """
    Permit dependency injection for tests; instantiate official SDK otherwise.
    """
    if client is not None:
        return client
    if not resolve_codex_api_key():
        raise RuntimeError(
            "GPT_CODEX_API_KEY is not set (legacy OPENAI_API_KEY is also checked)."
        )
    return create_codex_client(api_timeout)


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #
def review_file_with_api(
    *,
    repo: Path,
    path: str,
    content: bytes,
    is_binary: bool,
    global_instructions: str,
    iteration: int,
    model: str = DEFAULT_MODEL,
    api_timeout: int = DEFAULT_API_TIMEOUT,
    client: Any | None = None,
) -> FullFileDecision:
    """
    Ask the model for a **full‑file** decision for exactly one file.

    Parameters
    ----------
    repo : Path
        Repository root (for logging/context).
    path : str
        Repo‑relative POSIX path of the file under review.
    content : bytes
        Raw file content (exact on disk).
    is_binary : bool
        True if the file is considered binary (do not embed bytes in prompt).
    global_instructions : str
        The overall review objectives & constraints (user instructions).
    iteration : int
        1‑based iteration number (influences gates for docs/setup/examples).
    model : str
        Model name for the GPT-Codex API.
    api_timeout : int
        Per‑request timeout (seconds).
    client : Any
        Optional already‑constructed GPT-Codex client (facilitates testing).

    Returns
    -------
    FullFileDecision
        The model's decision in a machine‑readable form.
    """
    client = _ensure_client(client, api_timeout)

    # Path sanity (fail closed early)
    if not _is_safe_repo_rel_posix(path):
        log.warning("Unsafe or invalid review path %r; forcing keep.", path)
        return FullFileDecision(path=path, action="keep", reason="invalid path")

    language_hint = _language_hint_for_path(path)

    # Decide textual vs binary based on flag + decoding capability
    text_preview = ""
    if not is_binary:
        try:
            text_preview = _excerpt_bytes_to_text(content)
        except UnicodeDecodeError:
            # Treat as binary if decoding fails
            is_binary = True

    sys_msg = _system_prompt(iteration)
    usr_msg = _build_user_prompt(
        repo_root=repo,
        path=path,
        content_preview=text_preview,
        is_binary=is_binary,
        language_hint=language_hint,
        global_instructions=global_instructions,
        iteration=iteration,
    )

    tools = [_propose_fullfile_tool()]
    tool_name = tools[0]["function"]["name"]

    log.debug(
        "Full‑file review request | iter=%s | model=%s | path=%s | binary=%s | size=%d",
        iteration, model, path, is_binary, len(content)
    )

    # Issue the API call – we force the function call to guarantee structure
    try:
        resp = client.chat.completions.create(  # type: ignore[attr-defined]
            model=model,
            temperature=0,
            timeout=api_timeout,  # type: ignore[call-arg]
            messages=[
                {"role": "system", "content": sys_msg},
                {"role": "user", "content": usr_msg},
            ],
            tools=tools,
            tool_choice={"type": "function", "function": {"name": tool_name}},
        )
    except Exception as exc:
        log.exception("GPT-Codex API request failed for %s: %s", path, exc)
        # Fail closed: keep the file if the request fails
        return FullFileDecision(path=path, action="keep", reason=f"API error: {exc}")

    try:
        msg = resp.choices[0].message
        tool_calls = getattr(msg, "tool_calls", None) or []
    except Exception as exc:  # pragma: no cover
        log.error("Malformed API response for %s: %s", path, exc)
        return FullFileDecision(path=path, action="keep", reason="Malformed API response")

    if not tool_calls:
        log.warning("Assistant returned no tool call for %s; defaulting to keep.", path)
        return FullFileDecision(path=path, action="keep", reason="No tool call")

    tc = tool_calls[0]
    fn = getattr(tc, "function", None)
    raw_args = getattr(fn, "arguments", "") or "{}"

    try:
        args = json.loads(raw_args)
    except Exception as exc:
        log.warning("Tool args JSON parse failed for %s: %s", path, exc)
        return FullFileDecision(path=path, action="keep", reason="Unparseable tool args")

    # Normalize/validate fields
    action = str(args.get("action", "keep")).strip()
    out = FullFileDecision(
        path=str(args.get("path", path)),
        action=action,
        reason=(args.get("reason") or None),
        content=(args.get("content") or None),
        content_b64=(args.get("content_b64") or None),
    )

    # Path safety: if the assistant tried to move/rename, pin to the reviewed path.
    if not _is_safe_repo_rel_posix(out.path) or out.path != path:
        log.warning("Unsafe or mismatched path from model (%r). Using original path %r.", out.path, path)
        out.path = path

    # Sanity enforcement
    valid_actions = {"keep", "update", "update_binary", "create", "create_binary", "delete"}
    if out.action not in valid_actions:
        log.warning("Invalid action '%s' for %s; defaulting to keep.", out.action, path)
        out.action = "keep"

    # Cross‑field requirements
    if out.action in {"update", "create"} and not out.content:
        log.warning("Action '%s' without text content for %s; defaulting to keep.", out.action, path)
        out.action = "keep"

    if out.action in {"update_binary", "create_binary"}:
        # Ensure Base64 is present and decodable
        if not out.content_b64:
            log.warning("Binary action '%s' without content_b64 for %s; defaulting to keep.", out.action, path)
            out.action = "keep"
        else:
            try:
                _ = base64.b64decode(out.content_b64, validate=True)
            except Exception:
                log.warning("Invalid Base64 for %s; defaulting to keep.", path)
                out.action = "keep"

    # Defer docs/setup/examples/CI until iteration 3 (defensive – orchestrator enforces this too)
    if iteration < 3 and _path_deferred_before_iter3(out.path):
        log.info("Deferring docs/setup/examples/CI change for %s until iteration 3 → keep.", out.path)
        out.action = "keep"
        out.reason = (out.reason or "") + " (deferred until iter 3)"

    # Reconcile create/update choice with on‑disk existence to avoid apply errors
    exists = (Path(repo) / out.path).exists()
    if exists and out.action in {"create", "create_binary"}:
        mapped = "update" if out.action == "create" else "update_binary"
        log.info("Mapping %s → %s for existing file %s.", out.action, mapped, out.path)
        out.action = mapped
    if (not exists) and out.action in {"update", "update_binary"}:
        mapped = "create" if out.action == "update" else "create_binary"
        log.info("Mapping %s → %s for missing file %s.", out.action, mapped, out.path)
        out.action = mapped
    if (not exists) and out.action == "delete":
        log.info("Requested delete for non‑existent file %s → keep.", out.path)
        out.action = "keep"
        out.reason = (out.reason or "") + " (no such file)"

    log.info("Decision for %s → %s%s", out.path, out.action, f"  · {out.reason}" if out.reason else "")
    return out


# --------------------------------------------------------------------------- #
# Patch conversion helper (to feed apply_patch.py)
# --------------------------------------------------------------------------- #
def decision_to_patch(decision: FullFileDecision) -> Optional[Dict[str, Any]]:
    """
    Convert a `FullFileDecision` to a single JSON patch understood by apply_patch.py.

    Returns
    -------
    dict | None
        Patch dict with the canonical fields (op, file, [body|body_b64], status),
        or None if the decision implies no change (keep).
    """
    action = decision.action
    path = decision.path

    if action == "keep":
        return None
    if action == "delete":
        return {"op": "delete", "file": path, "status": "in_progress"}

    if action in {"update", "create"}:
        assert decision.content is not None
        op = "update" if action == "update" else "create"
        return {
            "op": op,
            "file": path,
            "body": decision.content,
            "status": "in_progress",
        }

    if action in {"update_binary", "create_binary"}:
        assert decision.content_b64 is not None
        op = "update" if action == "update_binary" else "create"
        return {
            "op": op,
            "file": path,
            "body_b64": decision.content_b64,
            "status": "in_progress",
        }

    # Should not reach here (guarded above), but fail closed.
    log.warning("Unknown decision action '%s' for %s; ignoring.", action, path)
    return None


__all__ = [
    "FullFileDecision",
    "review_file_with_api",
    "decision_to_patch",
]
