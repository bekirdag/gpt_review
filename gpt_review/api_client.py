#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
===============================================================================
GPT‑Review ▸ OpenAI API Client Wrapper
===============================================================================

Purpose
-------
A small, dependency‑light wrapper around the OpenAI Chat Completions API that
our orchestrator can use to:
  • keep a short rolling history (token‑aware),
  • force a structured **tool call** (`submit_patch`) that returns a *full file*
    patch compatible with our canonical schema (gpt_review/schema.json),
  • request strict **JSON arrays** (no prose; robust parsing).

Design notes
------------
• We prefer *tools* for patches (same fields as schema.json). This keeps a
  single contract across API and browser modes.
• For lists (e.g., “new files to create”), we ask for a raw **JSON array**.
  We still defensively parse: first try `json.loads()`, then attempt to find
  the first balanced array substring if there is accidental prose.
• Context is pruned to keep cost down; the orchestrator can call `.note(...)`
  to add a one‑time overview message before iteration 1.

Environment
-----------
OPENAI_API_KEY    – required
OPENAI_BASE_URL   – optional (a compatible gateway/base)
GPT_REVIEW_CTX_TURNS – max assistant/tool “turn pairs” to retain (default 6)
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from gpt_review import get_logger

log = get_logger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Tunables
# ─────────────────────────────────────────────────────────────────────────────
DEFAULT_CTX_TURNS = int(os.getenv("GPT_REVIEW_CTX_TURNS", "6"))
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL") or os.getenv("OPENAI_API_BASE")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")


# ─────────────────────────────────────────────────────────────────────────────
# Tool schema – mirrors gpt_review/schema.json (kept in sync manually)
# ─────────────────────────────────────────────────────────────────────────────
def _submit_patch_tool() -> Dict[str, Any]:
    """
    OpenAI tool/function schema for `submit_patch`.

    We accept 3-/4-digit octal for chmod modes, and we constrain operation enums
    and status enums to match the canonical JSON‑Schema.
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


# ─────────────────────────────────────────────────────────────────────────────
# System prompt
# ─────────────────────────────────────────────────────────────────────────────
def _system_prompt() -> str:
    """
    Minimal, directive system message to keep tokens down. Detailed iteration
    framing lives in higher‑level prompts (gpt_review.prompts).
    """
    return (
        "You are GPT‑Review. Respond **only** by calling the function "
        "`submit_patch` when asked to modify a file, returning a **complete file** "
        "for create/update operations. When asked for lists, reply with a strict "
        "JSON array only (no prose, no code fences). Keep changes minimal and "
        "self‑contained; use status='in_progress' until the last patch, then 'completed'."
    )


# ─────────────────────────────────────────────────────────────────────────────
# Helpers – context pruning & array extraction
# ─────────────────────────────────────────────────────────────────────────────
def _prune_messages(msgs: List[Dict[str, Any]], max_turn_pairs: int) -> List[Dict[str, Any]]:
    """
    Keep system + initial user notes, plus the last *approximate* set of
    assistant/tool pairs. This is an approximation that works well enough for
    bounded conversations in this tool.
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
       parse that slice. This catches common cases where the model added stray
       prose (despite the strict prompt).
    3) On failure, raise ValueError with a concise snippet.
    """
    # 1) Straight parse
    try:
        val = json.loads(text)
        if isinstance(val, list):
            return val
    except Exception:
        pass

    # 2) Substring try (naive but effective for flat arrays)
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
        log.info("OpenAI client initialised | model=%s | timeout=%ss | base=%s",
                 self.model, self.timeout_s, OPENAI_BASE_URL or "<default>")

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
        log.debug("Added overview/user note (%d chars); messages=%d",
                  len(user_content or ""), len(self.messages))

    # --- Calls: JSON array ------------------------------------------------- #
    def ask_json_array(self, prompt: str) -> List[dict]:
        """
        Ask the assistant to return a strict JSON array (no prose). The prompt
        should *explicitly* repeat that requirement (our prompts do).
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
        # Append the assistant message to history; avoid clutter with huge arrays.
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

    # --- Calls: tool‑forced full‑file patch -------------------------------- #
    def call_submit_patch(self, user_prompt: str) -> Dict[str, Any]:
        """
        Force a tool call to `submit_patch` and return the decoded arguments
        as a plain dict. This does **not** validate against the JSON‑Schema;
        the orchestrator does that immediately afterwards.
        """
        sdk = self._ensure_sdk()
        tools = [_submit_patch_tool()]
        tool_name = tools[0]["function"]["name"]

        self.messages.append({"role": "user", "content": user_prompt})
        self.messages = _prune_messages(self.messages, self.max_turn_pairs)

        try:
            resp = sdk.chat.completions.create(
                model=self.model,
                messages=self.messages,
                temperature=0,
                tools=tools,
                tool_choice={"type": "function", "function": {"name": tool_name}},
                timeout=self.timeout_s,  # type: ignore[call-arg]
            )
        except Exception as exc:
            log.exception("OpenAI request (submit_patch tool) failed: %s", exc)
            raise

        try:
            choice = resp.choices[0]
            msg = choice.message
            tool_calls = getattr(msg, "tool_calls", None) or []
        except Exception as exc:
            raise RuntimeError(f"Malformed API response (tool call): {exc}") from exc

        if not tool_calls:
            # Record the assistant content to aid debugging
            self.messages.append({"role": "assistant", "content": msg.content or ""})
            self.messages = _prune_messages(self.messages, self.max_turn_pairs)
            raise RuntimeError("Assistant did not call the required tool 'submit_patch'.")

        tc = tool_calls[0]
        fn = getattr(tc, "function", None)
        fn_name = getattr(fn, "name", None)
        raw_args = getattr(fn, "arguments", "") or ""
        call_id = getattr(tc, "id", None) or "call_0"

        # Keep assistant message (with tool_calls) in the transcript
        self.messages.append({"role": "assistant", "content": msg.content or "", "tool_calls": tool_calls})
        self.messages = _prune_messages(self.messages, self.max_turn_pairs)

        if fn_name != tool_name:
            raise RuntimeError(f"Unexpected function name: {fn_name}")

        try:
            patch = json.loads(raw_args)
            log.info("Tool call returned op=%s file=%s status=%s",
                     patch.get("op"), patch.get("file"), patch.get("status"))
        except Exception as exc:
            raise RuntimeError(f"Failed to decode tool arguments as JSON: {exc}") from exc

        # We do **not** send a tool result message here (one‑shot call). If you
        # want to continue the tool‑call chain, you could append a "tool" role
        # message linked via tool_call_id=call_id. The orchestrator applies the
        # patch locally and starts a new user turn instead.
        log.debug("submit_patch call id=%s captured; returning args to caller.", call_id)
        return patch


# ─────────────────────────────────────────────────────────────────────────────
# Public helpers used by the orchestrator
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
    expected_kind: str = "update",  # "update" or "create" – only for sanity checks
) -> Dict[str, Any]:
    """
    Send `prompt` and force a `submit_patch` tool call. Perform light sanity
    checks against the expected action for the given file path.

    The caller (orchestrator) must still run `validate_patch(...)` on the
    returned dict to enforce the canonical schema.
    """
    patch = client.call_submit_patch(prompt)

    # Sanity fill: file path must be set and consistent.
    file_from_model = (patch.get("file") or "").strip()
    if not file_from_model:
        log.warning("Assistant omitted 'file' → setting it to %s", rel_path)
        patch["file"] = rel_path
    elif file_from_model != rel_path:
        log.warning("Assistant returned mismatched file %r (expected %r) → overriding.",
                    file_from_model, rel_path)
        patch["file"] = rel_path

    # For create/update, **full file content** must be present.
    if patch.get("op") in {"create", "update"}:
        if "body" not in patch and "body_b64" not in patch:
            raise RuntimeError("Expected a full‑file body/body_b64 in the patch but none was provided.")

    # Light expected_kind check (we won't mutate here; the orchestrator may).
    if expected_kind == "create" and patch.get("op") not in {"create", "update"}:
        log.warning("Expected a create/update for new file, got op=%s", patch.get("op"))
    if expected_kind == "update" and patch.get("op") not in {"update", "create"}:
        log.warning("Expected an update/create for existing file, got op=%s", patch.get("op"))

    return patch
