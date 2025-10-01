#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
===============================================================================
GPT‑Review ▸ API Driver (no browser)
===============================================================================

Purpose
-------
Drive the same "edit → run → fix" loop using the GPT-Codex API without
automating a browser. The assistant returns patches via a **function call**
named `submit_patch`. We validate and apply that patch locally, optionally run
a shell command (tests/linter), and send the outcome back as the tool's result.

What this driver guarantees
---------------------------
• **Full‑file outputs**: for create/update, the assistant MUST return a complete
  file body (never a diff). We validate with the canonical JSON‑Schema.

• **Blueprint preflight** (optional, enabled by default via env):
  At the start of a run, ensure the four project blueprints exist under the
  canonical directory managed by `gpt_review.blueprints_util`:
      .gpt-review/blueprints/WHITEPAPER.md
      .gpt-review/blueprints/BUILD_GUIDE.md
      .gpt-review/blueprints/SDS.md
      .gpt-review/blueprints/PROJECT_INSTRUCTIONS.md
  Missing docs are created **one file per tool call** (complete contents).
  A compact **blueprints summary** is injected into the initial prompts so the
  assistant aligns all subsequent edits with the project’s requirements.

• **Strict tool schema + path hygiene**:
  We accept exactly one file per patch and enforce **repository‑relative POSIX paths**
  (no absolute paths, no backslashes, no '..', nothing under .git/, no Windows drive
  letters). Unsafe patches are rejected with a structured tool result so the
  assistant can correct them.

Design notes
------------
• **Token‑aware**:
  - Minimal system prompt (explicitly requires FULL FILE bodies).
  - Rolling history limited by GPT_REVIEW_CTX_TURNS.
  - Only the tail of failing logs is returned (GPT_REVIEW_LOG_TAIL_CHARS).
  - Blueprints summary is size‑capped (see env below).

• **Compatibility**:
  - The `submit_patch` function schema mirrors gpt_review/schema.json so we can
    reuse `patch_validator.validate_patch` before applying.

• **Dependency‑light**:
  - Accepts an injected `client` (used by offline tests).
  - Otherwise, instantiates the GPT-Codex client using GPT_CODEX_API_KEY.

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

Environment
-----------
GPT_CODEX_API_KEY                      – required at runtime (falls back to OPENAI_API_KEY)
GPT_CODEX_BASE_URL                     – optional gateway base URL (aliases supported)

GPT_REVIEW_CTX_TURNS                   – rolling turn pairs (default: 6)
GPT_REVIEW_LOG_TAIL_CHARS              – tail of logs to send (default: 20000)

# Blueprint preflight & summarization
GPT_REVIEW_INCLUDE_BLUEPRINTS          – "1" to enable (default: 1)
GPT_REVIEW_BLUEPRINT_SUMMARY_MAX_BYTES – bytes cap for summary (default: 12000)
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
from gpt_review.codex_client import (
    create_client as create_codex_client,
    resolve_api_key as resolve_codex_api_key,
)
from patch_validator import validate_patch, is_safe_repo_rel_posix

# Canonical blueprint helpers (single source of truth for names/paths/summary)
from gpt_review.blueprints_util import (
    BLUEPRINT_KEYS,
    BLUEPRINT_LABELS,
    blueprint_paths,
    ensure_blueprint_dir,
    missing_blueprints,
    summarize_blueprints,
)

log = get_logger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Environment‑backed tunables
# ─────────────────────────────────────────────────────────────────────────────
DEFAULT_CTX_TURNS = int(os.getenv("GPT_REVIEW_CTX_TURNS", "6"))
LOG_TAIL_CHARS = int(os.getenv("GPT_REVIEW_LOG_TAIL_CHARS", "20000"))

INCLUDE_BLUEPRINTS = os.getenv("GPT_REVIEW_INCLUDE_BLUEPRINTS", "1").strip().lower() in {
    "1", "true", "yes", "on", "y", "t"
}
BLUEPRINT_SUMMARY_MAX_BYTES = int(os.getenv("GPT_REVIEW_BLUEPRINT_SUMMARY_MAX_BYTES", "12000"))

# Human titles for the four blueprint docs (stable + descriptive)
_BLUEPRINT_TITLES: Dict[str, str] = {
    "whitepaper": "Whitepaper & Engineering Blueprint",
    "build_guide": "Build Guide",
    "sds": "Project Software Design Specifications (SDS)",
    "project_instructions": "Project Code Files and Instructions",
}

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


def _snippet(s: str, limit: int = 240) -> str:
    """Compact single‑line snippet for logs."""
    one = (s or "").strip().replace("\n", " ")
    return (one[:limit] + "…") if len(one) > limit else one


# ─────────────────────────────────────────────────────────────────────────────
# Tool schema – mirrors gpt_review/schema.json (kept in sync manually)
# ─────────────────────────────────────────────────────────────────────────────
def _submit_patch_tool() -> Dict[str, Any]:
    """Tool/function schema for `submit_patch`."""
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
def _system_prompt(*, blueprints_summary: Optional[str] = None) -> str:
    """
    Short and directive to keep tokens down; the full contract is enforced via tool.
    Optionally embeds an abridged blueprints summary to ground the session.
    """
    bp = f"\nBlueprint documents (abridged):\n{blueprints_summary}\n" if blueprints_summary else ""
    return (
        "You are GPT‑Review. Respond **only** by calling the function `submit_patch` "
        "with a single, minimal patch for exactly one file. "
        "For create/update operations you MUST return a **COMPLETE FILE** (not a diff) in `body` "
        "or `body_b64` for binary content. "
        "After each patch, wait for the tool results before proposing the next patch. "
        "Use status='in_progress' until the last patch, then 'completed'. "
        "Avoid prose unless asked; keep changes small and self‑contained; use repo‑relative POSIX paths."
        f"{bp}"
    )


def _instructions_block(user_instructions: str, *, blueprints_summary: Optional[str] = None) -> str:
    # Keep this compact and language‑agnostic. Optionally include blueprints summary.
    rules = (
        "Rules:\n"
        "1) One file per patch; return a **complete file** for create/update.\n"
        "2) Preserve behaviour (backwards‑compatible) unless fixing a clear defect.\n"
        "3) Keep diffs minimal; avoid unrelated formatting or churn.\n"
        "4) Use exact repo‑relative POSIX paths; avoid creating new directories unless requested.\n"
        "5) When the command fails, propose the next patch to address the failure.\n"
    )
    bp = f"\nBlueprint documents (abridged):\n{blueprints_summary}\n" if blueprints_summary else ""
    return f"{user_instructions.strip()}\n{bp}\n{rules}"


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
    # Chunk by assistant/tool boundaries; keep the last (2 * max_turn_pairs + slack) messages.
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
# GPT-Codex client
# ─────────────────────────────────────────────────────────────────────────────
def _ensure_client(client: Any | None, api_timeout: int):
    """
    Return a usable client. If *client* is provided (e.g., tests), use it.
    Otherwise instantiate the GPT-Codex SDK client.
    """
    if client is not None:
        return client

    if not resolve_codex_api_key():
        raise RuntimeError(
            "GPT_CODEX_API_KEY is not set in the environment (legacy OPENAI_API_KEY is also checked)."
        )

    # 'timeout' can be set per-call; the adapter injects it when the SDK allows it.
    return create_codex_client(api_timeout)


# ─────────────────────────────────────────────────────────────────────────────
# Blueprint helpers (preflight) — unified with blueprints_util
# ─────────────────────────────────────────────────────────────────────────────
def _blueprints_summary(repo: Path) -> Optional[str]:
    """
    Return a compact, concatenated summary of existing blueprint docs using the
    canonical names/locations from `blueprints_util`. The size is bounded by
    GPT_REVIEW_BLUEPRINT_SUMMARY_MAX_BYTES (roughly even split per doc).
    """
    # We use a per‑doc character cap; bytes vs chars is an approximation here,
    # but good enough for prompt budgeting.
    per_doc = max(512, BLUEPRINT_SUMMARY_MAX_BYTES // max(1, len(BLUEPRINT_KEYS)))
    try:
        summary = summarize_blueprints(repo, max_chars_per_doc=per_doc)
        return summary or None
    except Exception as exc:
        log.warning("Failed to prepare blueprints summary: %s", exc)
        return None


def _ensure_blueprints(
    *,
    client: Any,
    model: str,
    api_timeout: int,
    repo: Path,
    user_instructions: str,
) -> None:
    """
    Ensure the four blueprint documents exist under the canonical directory
    managed by `blueprints_util`. For each missing doc, request a **single**
    `submit_patch` create with full content.
    """
    ensure_blueprint_dir(repo)

    missing = missing_blueprints(repo)
    if not missing:
        log.info("Blueprint preflight: all documents exist.")
        return

    log.info("Blueprint preflight: %d document(s) missing; creating…", len(missing))

    tool = _submit_patch_tool()
    tool_name = tool["function"]["name"]

    # Resolve repo‑relative POSIX paths for the four docs
    abs_paths = blueprint_paths(repo)  # {key: Path (absolute)}
    # Convert to repo‑relative posix strings without guessing (apply_patch expects relative)
    rel_paths: Dict[str, str] = {}
    try:
        # Compute repo root once
        repo_root = repo.expanduser().resolve()
        for k, p in abs_paths.items():
            rel_paths[k] = p.relative_to(repo_root).as_posix()
    except Exception:
        # Extremely defensive fallback (should not happen under a real git repo)
        for k, p in abs_paths.items():
            rel_paths[k] = p.as_posix()

    for key in missing:
        rel_path = rel_paths[key]
        title = _BLUEPRINT_TITLES.get(key, BLUEPRINT_LABELS.get(key, key))

        # Compose a strict, per‑file blueprint creation request
        sys_msg = {
            "role": "system",
            "content": (
                "You are GPT‑Review. Respond ONLY by calling `submit_patch` to CREATE exactly one file. "
                "Return a COMPLETE Markdown file in `body`. Use the EXACT repo‑relative POSIX path I provide. "
                "No prose."
            ),
        }
        user_msg = {
            "role": "user",
            "content": textwrap.dedent(
                f"""
                Create the following blueprint document **now** with clear, structured sections:
                - Path   : {rel_path}
                - Title  : {title}

                Purpose:
                These four documents guide the entire review and build. Write the full content here:
                  1) Whitepaper & Engineering Blueprint – problem, scope, architecture, trade‑offs.
                  2) Build Guide – environment, dependencies, setup, commands.
                  3) Software Design Specification (SDS) – detailed components, interfaces, data models.
                  4) Project Code Files and Instructions – repository layout, entrypoints, run/test commands, expected outputs.

                Inputs (from user instructions):
                {user_instructions.strip()}

                Requirements:
                - Return a **complete Markdown file** via `submit_patch` (op="create") with `file="{rel_path}"`.
                - Use informative headings, lists, and code fences where helpful.
                - Keep secrets & tokens out of the document.
                """
            ).strip(),
        }

        try:
            resp = client.chat.completions.create(
                model=model,
                messages=[sys_msg, user_msg],
                temperature=0,
                tools=[tool],
                tool_choice={"type": "function", "function": {"name": tool_name}},
                timeout=api_timeout,  # type: ignore[call-arg]
            )
        except Exception as exc:
            log.exception("Blueprint create API call failed for %s: %s", rel_path, exc)
            raise SystemExit(1) from exc

        try:
            msg = resp.choices[0].message
            calls = getattr(msg, "tool_calls", None) or []
        except Exception as exc:
            log.error("Malformed API response while creating blueprint %s: %s", rel_path, exc)
            raise SystemExit(1) from exc

        if not calls:
            log.error("Assistant did not call the tool when creating blueprint %s.", rel_path)
            raise SystemExit(1)

        tc = calls[0]
        fn = getattr(tc, "function", None)
        raw_args = getattr(fn, "arguments", "") or "{}"
        call_id = getattr(tc, "id", "call_0")

        # Echo the assistant tool‑call message before our tool result (for traceability)
        assistant_msg = {"role": "assistant", "content": "", "tool_calls": calls}

        # Parse & validate the patch
        try:
            patch = json.loads(raw_args)
            # Enforce schema and safety, then tighten per our expectation (op/file).
            validate_patch(json.dumps(patch, ensure_ascii=False))
        except Exception as exc:
            log.error("Blueprint patch validation failed for %s: %s", rel_path, exc)
            # Send rejection back as tool content to guide a retry (explicit failure).
            tool_result = {"ok": False, "stage": "validate_patch", "error": f"{exc}"}
            client.chat.completions.create(  # best‑effort feedback; ignore result
                model=model,
                messages=[assistant_msg, {
                    "role": "tool",
                    "tool_call_id": call_id,
                    "name": tool_name,
                    "content": json.dumps(tool_result, ensure_ascii=False),
                }],
                tools=[tool],
                tool_choice={"type": "function", "function": {"name": tool_name}},
            )
            raise SystemExit(1)

        # Tighten expectations: must be create on EXACT path and path must be safe
        if patch.get("op") != "create" or patch.get("file") != rel_path or not is_safe_repo_rel_posix(rel_path):
            log.error(
                "Blueprint patch mismatch. Expected create '%s'; got op=%r file=%r",
                rel_path, patch.get("op"), patch.get("file")
            )
            raise SystemExit(1)

        # Apply patch
        res = _apply_patch(repo, patch)
        if not res.ok:
            log.error("Failed to create blueprint %s (rc=%s)\nstdout:\n%s\nstderr:\n%s",
                      rel_path, res.exit_code, res.stdout, res.stderr)
            raise SystemExit(1)

        # Send a success tool result message (helps the model stay in sync if it continues)
        tool_result = {
            "ok": True,
            "stage": "apply_patch",
            "commit": _current_commit(repo),
            "time": _now_iso_utc(),
        }
        try:
            client.chat.completions.create(  # best‑effort feedback; ignore result
                model=model,
                messages=[assistant_msg, {
                    "role": "tool",
                    "tool_call_id": call_id,
                    "name": tool_name,
                    "content": json.dumps(tool_result, ensure_ascii=False),
                }],
                tools=[tool],
                tool_choice={"type": "function", "function": {"name": tool_name}},
            )
        except Exception:
            pass

        log.info("Created blueprint: %s", rel_path)


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

    Flow:
      1) (Optional) Ensure the four blueprint documents exist; create if missing.
      2) Build initial messages (system + user) — include abridged blueprints.
      3) Force tool calls (`submit_patch`) until status='completed'.
      4) After each successful apply, optionally run `cmd` and return tail logs.
    """
    del auto  # not used in API mode; we always proceed automatically

    # Load instructions (fail fast)
    try:
        user_instructions = instructions_path.read_text(encoding="utf-8")
    except Exception as exc:
        raise SystemExit(f"Failed to read instructions file: {exc}") from exc

    # Sanity check: repository root should contain a .git directory
    repo = Path(repo).expanduser().resolve()
    if not (repo / ".git").exists():
        raise SystemExit(f"Not a git repository: {repo}")

    client = _ensure_client(client, api_timeout)

    # Optional preflight: ensure blueprint documents exist (canonical names/paths)
    if INCLUDE_BLUEPRINTS:
        try:
            _ensure_blueprints(
                client=client,
                model=model,
                api_timeout=api_timeout,
                repo=repo,
                user_instructions=user_instructions,
            )
        except SystemExit:
            raise
        except Exception as exc:
            log.exception("Blueprint preflight failed: %s", exc)
            raise SystemExit(1) from exc

    # Prepare abridged blueprint summary (optional)
    bp_summary = _blueprints_summary(repo) if INCLUDE_BLUEPRINTS else None

    tools = [_submit_patch_tool()]
    tool_name = tools[0]["function"]["name"]

    messages: List[Dict[str, Any]] = [
        {"role": "system", "content": _system_prompt(blueprints_summary=bp_summary)},
        {"role": "user", "content": _instructions_block(user_instructions, blueprints_summary=bp_summary)},
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
            log.exception("GPT-Codex API request failed: %s", exc)
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
            # Record assistant content to keep a faithful transcript, then nudge.
            content = msg.content or ""
            messages.append({"role": "assistant", "content": content})
            log.warning(
                "Assistant response lacked tool_calls; snippet: %r. Sending nudge.",
                _snippet(content),
            )
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

        # Additional **path hygiene** enforcement (beyond schema):
        op = patch.get("op")
        file_path = patch.get("file") or ""
        target_path = patch.get("target") or ""
        if not is_safe_repo_rel_posix(file_path):
            log.warning("Unsafe or non‑POSIX file path from assistant: %r", file_path)
            tool_result = {
                "ok": False,
                "stage": "path_check",
                "error": f"Unsafe or non‑POSIX repo‑relative path: {file_path!r}",
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
        if op == "rename" and not is_safe_repo_rel_posix(target_path):
            log.warning("Unsafe or non‑POSIX rename target from assistant: %r", target_path)
            tool_result = {
                "ok": False,
                "stage": "path_check",
                "error": f"Unsafe or non‑POSIX target path for rename: {target_path!r}",
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
