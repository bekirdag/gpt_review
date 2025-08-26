#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
===============================================================================
GPT‑Review ▸ API Driver (no browser)
===============================================================================

Purpose
-------
Drive the same "edit → run → fix" loop using the OpenAI API instead of
automating a browser. The assistant returns patches via a **function call**
named `submit_patch`. We validate and apply that patch locally, optionally run
a shell command (tests/linter), and send the outcome back as the tool's result.

Design notes
------------
• **Token‑aware**:
  - Minimal system prompt (explicitly requires FULL FILE bodies).
  - Rolling history limited by GPT_REVIEW_CTX_TURNS.
  - Only the tail of failing logs is returned (GPT_REVIEW_LOG_TAIL_CHARS).

• **Compatibility**:
  - The `submit_patch` function schema mirrors gpt_review/schema.json so we can
    reuse `patch_validator.validate_patch` before applying.

• **Dependency‑light**:
  - Accepts an injected `client` (used by offline tests).
  - Otherwise, instantiates `OpenAI` using OPENAI_API_KEY / OPENAI_BASE_URL.

Public API
----------
run(
    instructions_path: Path,
    repo: Path,
    cmd: Optional[str],
    auto: bool,
    timeout: int,
    model: str,
    api_timeout: int,
    client: Optional[Any] = None,
) -> None
"""
from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import textwrap
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from gpt_review import get_logger
from patch_validator import validate_patch

log = get_logger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Environment‑backed tunables (API mode only)
# ─────────────────────────────────────────────────────────────────────────────
DEFAULT_CTX_TURNS = int(os.getenv("GPT_REVIEW_CTX_TURNS", "6"))
LOG_TAIL_CHARS = int(os.getenv("GPT_REVIEW_LOG_TAIL_CHARS", "20000"))

OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL")  # optional
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")    # required at runtime if client not injected


# ─────────────────────────────────────────────────────────────────────────────
# Utilities
# ─────────────────────────────────────────────────────────────────────────────
def _now_iso_utc() -> str:
    """Current UTC timestamp as ISO‑8601 to seconds (stable for logs)."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _run_cmd(cmd: str, repo: Path, timeout: int) -> Tuple[bool, str, int]:
    """
    Execute *cmd* in *repo*; return (success, combined output, exit_code).
    """
    try:
        res = subprocess.run(
            cmd,
            cwd=repo,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        out = (res.stdout or "") + (res.stderr or "")
        return res.returncode == 0, out, res.returncode
    except subprocess.TimeoutExpired as exc:
        out = (exc.stdout or "") + (exc.stderr or "")
        banner = f"TIMEOUT: command exceeded {timeout}s\n"
        return False, banner + out, 124


def _current_commit(repo: Path) -> str:
    """
    Return HEAD SHA; "<no-commits-yet>" if none.
    """
    try:
        res = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--verify", "-q", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
        )
        sha = (res.stdout or "").strip()
        return sha if res.returncode == 0 and sha else "<no-commits-yet>"
    except Exception:
        return "<no-commits-yet>"


def _tail(text: str, n_chars: int = LOG_TAIL_CHARS) -> str:
    """Return *text* limited to its last *n_chars* characters."""
    if len(text) <= n_chars:
        return text
    return text[-n_chars:]


# ─────────────────────────────────────────────────────────────────────────────
# Tool schema – mirrors gpt_review/schema.json (kept in sync manually)
# ─────────────────────────────────────────────────────────────────────────────
def _submit_patch_tool() -> Dict[str, Any]:
    """
    OpenAI tool/function schema for `submit_patch`.
    """
    return {
        "type": "function",
        "function": {
            "name": "submit_patch",
            "description": (
                "Create, update, delete, rename or chmod exactly one file in the repository. "
                "For create/update you MUST return a COMPLETE FILE in 'body' (or 'body_b64' for binary) — never a diff. "
                "Return one patch at a time and set status to 'in_progress' until the last patch, "
                "then 'completed'."
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
# Conversation scaffolding
# ─────────────────────────────────────────────────────────────────────────────
def _system_prompt() -> str:
    # Short and directive to keep tokens down; the full contract is enforced via tool.
    return (
        "You are GPT‑Review. Respond **only** by calling the function `submit_patch` "
        "with a single, minimal patch for exactly one file. "
        "For create/update operations you MUST return a **COMPLETE FILE** (not a diff) in `body` "
        "or `body_b64` for binary content. "
        "After each patch, wait for the tool results before proposing the next patch. "
        "Use status='in_progress' until the last patch, then 'completed'. "
        "Avoid prose unless asked; keep changes small and self‑contained; use repo‑relative POSIX paths."
    )


def _instructions_block(user_instructions: str) -> str:
    # Keep this compact and language‑agnostic.
    rules = (
        "Rules:\n"
        "1) One file per patch; return a **complete file** for create/update.\n"
        "2) Preserve behaviour (backwards‑compatible) unless fixing a clear defect.\n"
        "3) Keep diffs minimal; avoid unrelated formatting or churn.\n"
        "4) Use exact repo‑relative POSIX paths; avoid creating new directories unless requested.\n"
        "5) When the command fails, propose the next patch to address the failure.\n"
    )
    return f"{user_instructions.strip()}\n\n{rules}"


def _prune_messages(msgs: List[Dict[str, Any]], max_turn_pairs: int) -> List[Dict[str, Any]]:
    """
    Keep system + initial user, plus the last *max_turn_pairs* (assistant/tool/user cycles).
    Messages ordering must remain chronological.
    """
    # Always keep the first two: system + initial user
    if len(msgs) <= 2:
        return msgs
    head = msgs[:2]
    tail = msgs[2:]
    # A "turn pair" here is coarse (assistant + tool [+ optional user log]).
    # We approximate by limiting to the last N *assistant/tool* pairs in the tail.
    indices = [i for i, m in enumerate(tail) if m["role"] in ("assistant", "tool")]
    if not indices:
        return msgs
    # Chunk by assistant/tool boundaries; keep the last N chunks.
    # Simpler: keep the last (2 * max_turn_pairs + some slack) messages.
    # Slack allows occasional user log injections without losing pairing.
    slack = 2
    approx = 2 * max_turn_pairs + slack
    pruned_tail = tail[-approx:]
    return head + pruned_tail


# ─────────────────────────────────────────────────────────────────────────────
# Patch application
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class ApplyResult:
    ok: bool
    exit_code: int
    stdout: str
    stderr: str


def _apply_patch(repo: Path, patch: Dict[str, Any]) -> ApplyResult:
    """
    Call apply_patch.py via stdin. Return process result.
    """
    try:
        proc = subprocess.run(
            [sys.executable, str(Path(__file__).resolve().parent.parent / "apply_patch.py"), "-", str(repo)],
            input=json.dumps(patch, ensure_ascii=False),
            capture_output=True,
            text=True,
        )
        return ApplyResult(
            ok=(proc.returncode == 0),
            exit_code=proc.returncode,
            stdout=proc.stdout or "",
            stderr=proc.stderr or "",
        )
    except Exception as exc:  # pragma: no cover
        return ApplyResult(ok=False, exit_code=1, stdout="", stderr=str(exc))


# ─────────────────────────────────────────────────────────────────────────────
# OpenAI client
# ─────────────────────────────────────────────────────────────────────────────
def _ensure_client(client: Any | None, api_timeout: int):
    """
    Return a usable client. If *client* is provided (e.g., tests), use it.
    Otherwise instantiate the official SDK client.
    """
    if client is not None:
        return client
    try:
        from openai import OpenAI  # type: ignore
    except Exception as exc:  # pragma: no cover
        log.error("OpenAI client is not installed. pip install openai")
        raise

    if not OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY is not set in the environment.")

    # 'timeout' can be set per-call; here we keep the client simple.
    return OpenAI(base_url=OPENAI_BASE_URL, api_key=OPENAI_API_KEY)


# ─────────────────────────────────────────────────────────────────────────────
# Main API loop
# ─────────────────────────────────────────────────────────────────────────────
def run(
    *,
    instructions_path: Path,
    repo: Path,
    cmd: Optional[str],
    auto: bool,        # kept for CLI parity; has no special meaning in API mode
    timeout: int,
    model: str,
    api_timeout: int,
    client: Any | None = None,
) -> None:
    """
    Execute the API‑driven review loop until status='completed' and (if provided)
    --cmd passes. Raises SystemExit on unrecoverable errors.
    """
    del auto  # not used in API mode; we always proceed automatically

    # Load instructions (fail fast)
    try:
        user_instructions = instructions_path.read_text(encoding="utf-8")
    except Exception as exc:
        raise SystemExit(f"Failed to read instructions file: {exc}") from exc

    client = _ensure_client(client, api_timeout)

    tools = [_submit_patch_tool()]
    tool_name = tools[0]["function"]["name"]

    messages: List[Dict[str, Any]] = [
        {"role": "system", "content": _system_prompt()},
        {"role": "user", "content": _instructions_block(user_instructions)},
    ]

    turn = 0
    while True:
        turn += 1
        # Keep history short for cost control
        messages = _prune_messages(messages, DEFAULT_CTX_TURNS)

        # Issue request
        try:
            resp = client.chat.completions.create(  # type: ignore[attr-defined]
                model=model,
                messages=messages,
                temperature=0,
                tools=tools,
                tool_choice={"type": "function", "function": {"name": tool_name}},
                # Some SDKs accept per-call timeouts; if not, it's harmless for fakes/tests.
                timeout=api_timeout,  # type: ignore[call-arg]
            )
        except Exception as exc:
            log.exception("OpenAI API request failed: %s", exc)
            raise SystemExit(1) from exc

        # Extract the first tool call (we force tool_choice, so it should exist)
        try:
            choice = resp.choices[0]
            msg = choice.message
            tool_calls = getattr(msg, "tool_calls", None) or []
        except Exception as exc:
            log.error("Malformed API response: %s", exc)
            raise SystemExit(1) from exc

        if not tool_calls:
            # Nudge: ask assistant to call the function properly.
            log.warning("Assistant response lacked tool_calls; sending nudge.")
            messages.append(
                {
                    "role": "user",
                    "content": (
                        "Please call the function `submit_patch` only. "
                        "Do not reply with natural language."
                    ),
                }
            )
            continue

        tc = tool_calls[0]
        call_id = getattr(tc, "id", None) or "call_0"
        fn = getattr(tc, "function", None)
        fn_name = getattr(fn, "name", None)
        raw_args = getattr(fn, "arguments", "") or ""

        if fn_name != tool_name:
            log.warning("Received unexpected function name: %s", fn_name)
            # Keep the assistant message so the tool result can be linked by call_id.
            messages.append({"role": "assistant", "content": msg.content or "", "tool_calls": tool_calls})
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call_id,
                    "name": tool_name,
                    "content": json.dumps({"ok": False, "error": f"Unexpected function: {fn_name}"}),
                }
            )
            continue

        # Record the assistant tool-call message before sending the tool result
        messages.append({"role": "assistant", "content": msg.content or "", "tool_calls": tool_calls})

        # Parse & validate the patch
        tool_result: Dict[str, Any]
        try:
            patch = json.loads(raw_args)
            # Reuse our schema validator (accepts dict or JSON string)
            validate_patch(json.dumps(patch, ensure_ascii=False))
        except Exception as exc:
            log.warning("Patch validation failed at turn %d: %s", turn, exc)
            tool_result = {
                "ok": False,
                "stage": "validate_patch",
                "error": f"{exc}",
            }
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call_id,
                    "name": tool_name,
                    "content": json.dumps(tool_result, ensure_ascii=False),
                }
            )
            continue

        # Helpful visibility before applying the patch
        op = patch.get("op")
        file_path = patch.get("file")
        log.info("Turn %d: applying patch op=%s file=%s status=%s", turn, op, file_path, patch.get("status"))

        # Apply patch
        apply_res = _apply_patch(repo, patch)
        if not apply_res.ok:
            log.warning("Patch apply failed (rc=%s) at turn %d", apply_res.exit_code, turn)
            tool_result = {
                "ok": False,
                "stage": "apply_patch",
                "exit_code": apply_res.exit_code,
                "stdout": _tail(apply_res.stdout),
                "stderr": _tail(apply_res.stderr),
                "commit": _current_commit(repo),
                "time": _now_iso_utc(),
            }
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call_id,
                    "name": tool_name,
                    "content": json.dumps(tool_result, ensure_ascii=False),
                }
            )
            continue

        # Optionally run command
        cmd_ok, cmd_out, cmd_code = (True, "", 0)
        if cmd:
            log.info("Running command after patch: %s", shlex.join(shlex.split(cmd)))
            cmd_ok, cmd_out, cmd_code = _run_cmd(cmd, repo, timeout)

        # Build tool output (success + optional command results)
        tool_result = {
            "ok": True,
            "stage": "apply_patch",
            "commit": _current_commit(repo),
            "time": _now_iso_utc(),
        }
        if cmd:
            tool_result["command"] = {
                "cmd": cmd,
                "exit_code": cmd_code,
                "ok": cmd_ok,
                "log_tail": _tail(cmd_out),
            }

        messages.append(
            {
                "role": "tool",
                "tool_call_id": call_id,
                "name": tool_name,
                "content": json.dumps(tool_result, ensure_ascii=False),
            }
        )

        # Stop condition: status=completed AND (no cmd OR cmd passed).
        if (patch.get("status") == "completed") and (not cmd or cmd_ok):
            log.info(
                "All done — status=completed%s.",
                "" if not cmd else (" and command passed (rc=0)"),
            )
            return

        # Otherwise, loop continues: assistant will read the tool result
        # and (we force tool_choice) propose another `submit_patch` call.
        # No extra user message is required; this keeps context small.
