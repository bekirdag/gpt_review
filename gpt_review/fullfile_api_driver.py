#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
===============================================================================
GPT‑Review ▸ Full‑File API Driver (propose complete file replacements)
===============================================================================

Purpose
-------
For a given file (path + bytes), call an OpenAI‑compatible API and obtain a
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
----------------------
* `OPENAI_API_KEY`           – required (unless a client is injected)
* `OPENAI_BASE_URL`          – optional custom endpoint
* `GPT_REVIEW_MODEL`         – default model name (e.g., "gpt-5-pro")
* `GPT_REVIEW_API_TIMEOUT`   – per‑request timeout (seconds; default 120)

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
from pathlib import Path
from typing import Any, Dict, Optional

from gpt_review import get_logger

log = get_logger(__name__)

# --------------------------------------------------------------------------- #
# Env-backed defaults (keep aligned with gpt_review/api_driver.py)
# --------------------------------------------------------------------------- #
DEFAULT_MODEL = os.getenv("GPT_REVIEW_MODEL", "gpt-5-pro")
DEFAULT_API_TIMEOUT = int(os.getenv("GPT_REVIEW_API_TIMEOUT", "120"))
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")


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
# Tool schema (OpenAI "function") – forces a clear, machine‑readable reply
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
        "  1) Output must reflect a FULL file (no patches/diffs) when changing.\n"
        "  2) Keep changes minimal and behavior‑preserving unless the instructions demand otherwise.\n"
        "  3) If no change is necessary, choose action='keep'.\n"
        "  4) Use 'update_binary'/'create_binary' ONLY for non‑text/binary content.\n"
        "  5) Iteration gates:\n"
        "     - Iterations 1–2: focus on code/tests; do NOT introduce new docs/setup/examples.\n"
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
        "  • Iters 1–2: refactor/modernize code & tests only; do not touch docs/setup/examples.\n"
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
        # Provide the FULL textual content.
        body = f"```\n{content_preview}\n```"

    return header + "\nCurrent content:\n" + body


# --------------------------------------------------------------------------- #
# OpenAI client (lazy)
# --------------------------------------------------------------------------- #
def _ensure_client(client: Any | None):
    """
    Permit dependency injection for tests; instantiate official SDK otherwise.
    """
    if client is not None:
        return client
    try:
        from openai import OpenAI  # type: ignore
    except Exception as exc:  # pragma: no cover
        log.error("OpenAI client is not installed. `pip install openai`")
        raise
    if not OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY is not set.")
    return OpenAI(base_url=OPENAI_BASE_URL, api_key=OPENAI_API_KEY)


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
        Model name for OpenAI‑compatible API.
    api_timeout : int
        Per‑request timeout (seconds).
    client : Any
        Optional already‑constructed OpenAI client (facilitates testing).

    Returns
    -------
    FullFileDecision
        The model's decision in a machine‑readable form.
    """
    client = _ensure_client(client)

    language_hint = _language_hint_for_path(path)
    text_preview = ""
    if not is_binary:
        try:
            text_preview = content.decode("utf-8")
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
        log.exception("OpenAI API request failed for %s: %s", path, exc)
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

    # Defer docs/setup/examples until iteration 3 (defensive check – the orchestrator already enforces this)
    if iteration < 3:
        lower = out.path.lower()
        if any(seg in lower for seg in ("/docs/", "/doc/", "/examples/", "/example/")) \
           or any(lower.endswith(sfx) for sfx in (".md", ".rst")):
            log.info("Deferring docs/examples change for %s until iteration 3 → keep.", out.path)
            out.action = "keep"
            out.reason = (out.reason or "") + " (deferred docs/examples until iter 3)"

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
