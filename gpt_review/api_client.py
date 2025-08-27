#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
===============================================================================
GPT‑Review ▸ OpenAI API Client Wrapper (Strict Tools + Plan‑First Support)
===============================================================================

Purpose
-------
A small, dependency‑light wrapper around the OpenAI Chat Completions API that
the orchestrator (or other drivers) can use to:

  • keep a short rolling history (token‑aware),
  • force a structured **tool call** returning a *complete file* patch that
    matches our canonical JSON schema (gpt_review/schema.json),
  • request strict **JSON arrays** (no prose),
  • explicitly support a **plan‑first** step (description + run/test commands),
  • drive **error‑fix** edits using complete file replacements.

Design notes
------------
• We prefer *tools* for file edits (same shape as schema.json). This keeps a
  single contract across API and browser modes.
• For lists (e.g., “new files to create”), we ask for a raw **JSON array** and
  parse defensively: try JSON first, then a crude first‑[`[ .. ]`] extraction.
• Context pruning keeps cost down; callers can add an overview message via
  `.note(...)` before iteration 1.

Environment
-----------
OPENAI_API_KEY           – required
OPENAI_BASE_URL|API_BASE – optional (OpenAI‑compatible base)
GPT_REVIEW_CTX_TURNS     – max assistant/tool “turn pairs” to retain (default 6)

Compatibility
-------------
This module preserves the legacy helper functions:

    strict_json_array(client, prompt) -> list[dict]
    submit_patch_call(client, prompt, *, rel_path, expected_kind="update") -> dict

and an object interface:

    OpenAIClient(...).ask_json_array(...)
    OpenAIClient(...).call_submit_patch(...)
    OpenAIClient(...).call_propose_review_plan(...)
    OpenAIClient(...).call_propose_error_fixes(...)

Logging
-------
INFO for high‑level flow; DEBUG for detailed diagnostics.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from gpt_review import get_logger

log = get_logger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Tunables
# ─────────────────────────────────────────────────────────────────────────────
DEFAULT_CTX_TURNS = int(os.getenv("GPT_REVIEW_CTX_TURNS", "6"))
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL") or os.getenv("OPENAI_API_BASE")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")


# ─────────────────────────────────────────────────────────────────────────────
# Tool schemas (kept consistent with gpt_review/schema.json & api_driver.py)
# ─────────────────────────────────────────────────────────────────────────────
def _submit_patch_tool() -> Dict[str, Any]:
    """
    OpenAI tool/function schema for `submit_patch`.

    Enforces complete file bodies for create/update, supports delete/rename/chmod,
    and constrains enums to match the canonical JSON‑Schema.
    """
    return {
        "type": "function",
        "function": {
            "name": "submit_patch",
            "description": (
                "Create, update, delete, rename or chmod **exactly one file**. "
                "Always return a complete file for create/update (never a diff). "
                "Use status='in_progress' until the last patch, then 'completed'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "op": {
                        "type": "string",
                        "enum": ["create", "update", "delete", "rename", "chmod"],
                    },
                    "file": {"type": "string"},
                    "body": {"type": "string"},
                    "body_b64": {"type": "string"},
                    "target": {"type": "string"},
                    "mode": {
                        "type": "string",
                        "pattern": "^[0-7]{3,4}$",
                        "description": "Octal permission bits (e.g. '755' or '0755').",
                    },
                    "status": {
                        "type": "string",
                        "enum": ["in_progress", "completed"],
                    },
                },
                "required": ["op", "status"],
                "additionalProperties": False,
            },
        },
    }


def _propose_review_plan_tool() -> Dict[str, Any]:
    """
    Tool for the plan‑first step: how to run/test + short description and hints.
    """
    return {
        "type": "function",
        "function": {
            "name": "propose_review_plan",
            "description": (
                "Summarize how to run/test this repository on a clean machine. "
                "Return actionable commands and a short description."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "description": {"type": "string"},
                    "run_commands": {"type": "array", "items": {"type": "string"}},
                    "test_commands": {"type": "array", "items": {"type": "string"}},
                    "hints": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["description", "run_commands"],
                "additionalProperties": False,
            },
        },
    }


def _propose_error_fixes_tool() -> Dict[str, Any]:
    """
    Tool for error‑fix rounds: return complete file replacements for impacted files.
    """
    return {
        "type": "function",
        "function": {
            "name": "propose_error_fixes",
            "description": (
                "Given error logs from running the software, return COMPLETE file replacements "
                "for affected files (create/update/delete)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "edits": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "required": ["path", "action"],
                            "additionalProperties": False,
                            "properties": {
                                "path": {"type": "string"},
                                "action": {"type": "string", "enum": ["create", "update", "delete"]},
                                "content": {"type": "string"},
                                "notes": {"type": "string"},
                            },
                        },
                    },
                    "rationale": {"type": "string"},
                },
                "required": ["edits"],
                "additionalProperties": False,
            },
        },
    }


# ─────────────────────────────────────────────────────────────────────────────
# System prompt (compact & directive)
# ─────────────────────────────────────────────────────────────────────────────
def _system_prompt() -> str:
    """
    Minimal, directive system message to keep tokens down. Iteration‑level
    framing (e.g., deferral rules) is handled by the orchestrator prompts.
    """
    return (
        "You are GPT‑Review. For file changes, respond **only** by calling the tool "
        "`submit_patch` and return a **complete file** for create/update operations. "
        "For lists, reply with a strict JSON array only (no prose, no code fences). "
        "For planning, call `propose_review_plan` with concise, actionable commands. "
        "For runtime errors, call `propose_error_fixes` with COMPLETE file replacements. "
        "Keep changes minimal and self‑contained; use status='in_progress' until the last patch, "
        "then 'completed'."
    )


# ─────────────────────────────────────────────────────────────────────────────
# Helpers – context pruning & array extraction
# ─────────────────────────────────────────────────────────────────────────────
def _prune_messages(msgs: List[Dict[str, Any]], max_turn_pairs: int) -> List[Dict[str, Any]]:
    """
    Keep system + initial user notes, plus the last *approximate* set of
    assistant/tool pairs. This is an approximation that works well for our bounded flows.
    """
    if len(msgs) <= 2:
        return msgs
    head = msgs[:2]
    tail = msgs[2:]

    # Count assistant/tool markers in tail; keep ~2 * max_turn_pairs (with slack)
    indices = [i for i, m in enumerate(tail) if m.get("role") in ("assistant", "tool")]
    if not indices:
        return msgs

    slack = 2
    approx = 2 * max_turn_pairs + slack
    pruned_tail = tail[-approx:]
    return head + pruned_tail


def _extract_json_array(text: str) -> List[Any]:
    """
    Best‑effort extraction of a JSON array from *text*.

    Strategy:
      1) If the entire content parses to a list → return it.
      2) Otherwise, scan for the **first** '[' and the **last** ']' and try to
         parse that slice. This addresses common stray prose cases.
      3) On failure, raise ValueError with a concise snippet.
    """
    # 1) Straight parse
    try:
        val = json.loads(text)
        if isinstance(val, list):
            return val
    except Exception:
        pass

    # 2) Substring try
    first = text.find("[")
    last = text.rfind("]")
    if first != -1 and last != -1 and last > first:
        blob = text[first : last + 1]
        try:
            val = json.loads(blob)
            if isinstance(val, list):
                return val
        except Exception:
            pass

    # 3) Fail with context for debugging
    snippet = text.strip().replace("\n", " ")
    if len(snippet) > 240:
        snippet = snippet[:240] + "…"
    raise ValueError(f"Assistant did not return a valid JSON array. Got: {snippet!r}")


# ─────────────────────────────────────────────────────────────────────────────
# Client
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class OpenAIClient:
    """
    Thin wrapper around the OpenAI Chat Completions API.

    Attributes
    ----------
    model : str
        Model name (e.g., "gpt-5-pro").
    timeout_s : int
        Per‑request timeout in seconds.
    max_turn_pairs : int
        Rolling history window (assistant/tool pairs retained).
    messages : list[dict]
        Conversation buffer. Starts with a system prompt; `.note(...)`
        appends a user message.
    """

    model: str
    timeout_s: int = 120
    max_turn_pairs: int = DEFAULT_CTX_TURNS
    messages: List[Dict[str, Any]] = field(default_factory=list)

    # Internal: SDK client instance (lazy)
    _sdk: Any | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        self.messages = [{"role": "system", "content": _system_prompt()}]
        log.info(
            "OpenAI client initialised | model=%s | timeout=%ss | base=%s",
            self.model,
            self.timeout_s,
            OPENAI_BASE_URL or "<default>",
        )

    # --- SDK bootstrap ----------------------------------------------------- #
    def _ensure_sdk(self) -> Any:
        """
        Import and instantiate the official OpenAI client on first use.
        """
        if self._sdk is not None:
            return self._sdk
        if not OPENAI_API_KEY:
            raise RuntimeError("OPENAI_API_KEY is not set in the environment.")

        try:
            from openai import OpenAI  # type: ignore
        except Exception as exc:  # pragma: no cover
            log.error("OpenAI SDK not installed. Run: pip install 'openai>=1.0.0'")
            raise

        self._sdk = OpenAI(base_url=OPENAI_BASE_URL, api_key=OPENAI_API_KEY)
        return self._sdk

    # --- Conversation helpers --------------------------------------------- #
    def note(self, user_content: str) -> None:
        """
        Append a *user* message (e.g., an overview prompt) to the buffer.
        """
        self.messages.append({"role": "user", "content": user_content})
        self.messages = _prune_messages(self.messages, self.max_turn_pairs)
        log.debug(
            "Added overview/user note (%d chars); messages=%d",
            len(user_content or ""),
            len(self.messages),
        )

    # --- Internal: generic tool call -------------------------------------- #
    def _call_tool_only(self, tool_schema: Dict[str, Any]) -> Tuple[Dict[str, Any], str]:
        """
        Force a single tool call with the last user message already present.
        Returns (tool_args_dict, call_id).
        """
        sdk = self._ensure_sdk()
        tool_name = tool_schema["function"]["name"]
        try:
            resp = sdk.chat.completions.create(
                model=self.model,
                messages=self.messages,
                temperature=0,
                tools=[tool_schema],
                tool_choice={"type": "function", "function": {"name": tool_name}},
                timeout=self.timeout_s,  # type: ignore[call-arg]
            )
        except Exception as exc:
            log.exception("OpenAI request (tool=%s) failed: %s", tool_name, exc)
            raise

        try:
            choice = resp.choices[0]
            msg = choice.message
            calls = getattr(msg, "tool_calls", None) or []
        except Exception as exc:
            raise RuntimeError(f"Malformed API response (tool={tool_name}): {exc}") from exc

        if not calls:
            # Record assistant content to aid debugging
            self.messages.append({"role": "assistant", "content": msg.content or ""})
            self.messages = _prune_messages(self.messages, self.max_turn_pairs)
            raise RuntimeError(f"Assistant did not call the required tool '{tool_name}'.")

        tc = calls[0]
        fn = getattr(tc, "function", None)
        fn_name = getattr(fn, "name", None)
        raw_args = getattr(fn, "arguments", "") or ""
        call_id = getattr(tc, "id", None) or "call_0"

        # Keep assistant message (with tool_calls) in the transcript
        self.messages.append(
            {"role": "assistant", "content": msg.content or "", "tool_calls": calls}
        )
        self.messages = _prune_messages(self.messages, self.max_turn_pairs)

        if fn_name != tool_name:
            raise RuntimeError(f"Unexpected function name: {fn_name}")

        try:
            args = json.loads(raw_args)
            log.info("Tool '%s' returned keys=%s", tool_name, sorted(args.keys()))
        except Exception as exc:
            raise RuntimeError(f"Failed to decode tool arguments as JSON: {exc}") from exc

        return args, call_id

    # --- Calls: strict JSON array ----------------------------------------- #
    def ask_json_array(self, prompt: str) -> List[dict]:
        """
        Ask the assistant to return a strict JSON array (no prose).
        The prompt should *explicitly* repeat that requirement.
        """
        sdk = self._ensure_sdk()
        self.messages.append({"role": "user", "content": prompt})
        self.messages = _prune_messages(self.messages, self.max_turn_pairs)

        try:
            resp = sdk.chat.completions.create(
                model=self.model,
                messages=self.messages,
                temperature=0,
                timeout=self.timeout_s,  # type: ignore[call-arg]
            )
        except Exception as exc:
            log.exception("OpenAI request for JSON array failed: %s", exc)
            raise

        try:
            msg = resp.choices[0].message
            content = msg.content or ""
        except Exception as exc:
            raise RuntimeError(f"Malformed API response (json array): {exc}") from exc

        arr = _extract_json_array(content)
        # Append assistant message to history; avoid clutter with huge arrays.
        self.messages.append({"role": "assistant", "content": "[…JSON array…]"})
        self.messages = _prune_messages(self.messages, self.max_turn_pairs)
        log.info("Strict JSON array received with %d entries.", len(arr))

        # Enforce dict items (most callers expect array[dict])
        out: List[dict] = []
        for i, item in enumerate(arr, 1):
            if isinstance(item, dict):
                out.append(item)
            else:
                log.warning("Array item %d is not an object; coercing via wrapper.", i)
                out.append({"value": item})
        return out

    # --- Calls: submit_patch ------------------------------------------------ #
    def call_submit_patch(self, user_prompt: str) -> Dict[str, Any]:
        """
        Force a tool call to `submit_patch` and return the decoded arguments
        as a plain dict. Schema validation is performed by the caller.
        """
        self.messages.append({"role": "user", "content": user_prompt})
        self.messages = _prune_messages(self.messages, self.max_turn_pairs)
        args, _ = self._call_tool_only(_submit_patch_tool())
        return args

    # --- Calls: plan‑first -------------------------------------------------- #
    def call_propose_review_plan(self, user_prompt: str) -> Dict[str, Any]:
        """
        Force a tool call to `propose_review_plan` (plan‑first step).
        """
        self.messages.append({"role": "user", "content": user_prompt})
        self.messages = _prune_messages(self.messages, self.max_turn_pairs)
        args, _ = self._call_tool_only(_propose_review_plan_tool())
        return args

    # --- Calls: error fixes ------------------------------------------------- #
    def call_propose_error_fixes(self, user_prompt: str) -> Dict[str, Any]:
        """
        Force a tool call to `propose_error_fixes` for runtime errors.
        """
        self.messages.append({"role": "user", "content": user_prompt})
        self.messages = _prune_messages(self.messages, self.max_turn_pairs)
        args, _ = self._call_tool_only(_propose_error_fixes_tool())
        return args


# ─────────────────────────────────────────────────────────────────────────────
# Public helpers (backward‑compatible)
# ─────────────────────────────────────────────────────────────────────────────
def strict_json_array(client: OpenAIClient, prompt: str) -> List[dict]:
    """
    Convenience wrapper that delegates to `client.ask_json_array`.
    """
    return client.ask_json_array(prompt)


def submit_patch_call(
    client: OpenAIClient,
    prompt: str,
    *,
    rel_path: str,
    expected_kind: str = "update",  # "update" or "create" – sanity checks only
) -> Dict[str, Any]:
    """
    Send `prompt` and force a `submit_patch` tool call. Perform light sanity
    checks against the expected action for the given file path.

    The caller should run `patch_validator.validate_patch(...)` on the returned
    dict to enforce the canonical schema.
    """
    patch = client.call_submit_patch(prompt)

    # Sanity fill: file path must be set and consistent.
    file_from_model = (patch.get("file") or "").strip()
    if not file_from_model:
        log.warning("Assistant omitted 'file' → setting it to %s", rel_path)
        patch["file"] = rel_path
    elif file_from_model != rel_path:
        log.warning(
            "Assistant returned mismatched file %r (expected %r) → overriding.",
            file_from_model,
            rel_path,
        )
        patch["file"] = rel_path

    # For create/update, **full file content** must be present.
    if patch.get("op") in {"create", "update"}:
        if "body" not in patch and "body_b64" not in patch:
            raise RuntimeError(
                "Expected a full‑file body/body_b64 in the patch but none was provided."
            )

    # Light expected_kind check (the orchestrator may further enforce).
    if expected_kind == "create" and patch.get("op") not in {"create", "update"}:
        log.warning("Expected a create/update for new file, got op=%s", patch.get("op"))
    if expected_kind == "update" and patch.get("op") not in {"update", "create"}:
        log.warning(
            "Expected an update/create for existing file, got op=%s",
            patch.get("op"),
        )

    return patch


__all__ = [
    "OpenAIClient",
    "strict_json_array",
    "submit_patch_call",
]
