#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
===============================================================================
GPT‑Review ▸ API Driver (no browser)
===============================================================================

Purpose
-------
Drive the same "edit → run → fix" loop using an OpenAI‑compatible API without
automating a browser. The assistant returns patches via a **function call**
named `submit_patch`. We validate and apply that patch locally, optionally run
a shell command (tests/linter), and send the outcome back as the tool's result.

New in this version
-------------------
• **Blueprint preflight** (optional, enabled by default via env):
  At the start of a run, ensure the four project blueprints exist under
  `.gpt-review/blueprints/`:
    1) WHITEPAPER_AND_ENGINEERING_BLUEPRINT.md
    2) BUILD_GUIDE.md
    3) SOFTWARE_DESIGN_SPECIFICATION_SDS.md
    4) PROJECT_CODE_FILES_AND_INSTRUCTIONS.md
  If missing, they are created **one file per tool call** (complete contents).
  A compact **blueprints summary** is then injected into the initial prompts
  so the assistant aligns all subsequent edits with the project’s requirements.

• **Full‑file guarantees**:
  Strict tool schema, server‑side JSON validation (`patch_validator`) and
  path hygiene are preserved. The driver only accepts **complete files** for
  create/update and rejects prose‑only responses.

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

Environment
-----------
OPENAI_API_KEY                         – required at runtime (unless client injected)
OPENAI_BASE_URL                        – optional gateway base URL

GPT_REVIEW_CTX_TURNS                   – rolling turn pairs (default: 6)
GPT_REVIEW_LOG_TAIL_CHARS              – tail of logs to send (default: 20000)

# Blueprint preflight & summarization
GPT_REVIEW_INCLUDE_BLUEPRINTS          – "1" to enable (default: 1)
GPT_REVIEW_BLUEPRINT_DIR               – dir for blueprints (default: .gpt-review/blueprints)
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
from pathlib import Path, PurePosixPath
from typing import Any, Dict, List, Optional, Tuple

from gpt_review import get_logger
from patch_validator import validate_patch

log = get_logger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Environment‑backed tunables
# ─────────────────────────────────────────────────────────────────────────────
DEFAULT_CTX_TURNS = int(os.getenv("GPT_REVIEW_CTX_TURNS", "6"))
LOG_TAIL_CHARS = int(os.getenv("GPT_REVIEW_LOG_TAIL_CHARS", "20000"))

OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL")  # optional
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")    # required at runtime if client not injected

INCLUDE_BLUEPRINTS = os.getenv("GPT_REVIEW_INCLUDE_BLUEPRINTS", "1").strip().lower() in {
    "1", "true", "yes", "on", "y", "t"
}
BLUEPRINT_DIR = os.getenv("GPT_REVIEW_BLUEPRINT_DIR", ".gpt-review/blueprints")
BLUEPRINT_SUMMARY_MAX_BYTES = int(os.getenv("GPT_REVIEW_BLUEPRINT_SUMMARY_MAX_BYTES", "12000"))

# Canonical blueprint file specs (repo‑relative POSIX paths)
_BLUEPRINT_SPECS: Tuple[Tuple[str, str], ...] = (
    ("WHITEPAPER_AND_ENGINEERING_BLUEPRINT.md", "Whitepaper & Engineering Blueprint"),
    ("BUILD_GUIDE.md", "Build Guide"),
    ("SOFTWARE_DESIGN_SPECIFICATION_SDS.md", "Project Software Design Specification (SDS)"),
    ("PROJECT_CODE_FILES_AND_INSTRUCTIONS.md", "Project Code Files and Instructions"),
)


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


def _is_safe_repo_rel_posix(path: str) -> bool:
    """
    Defensive path guard:
      - POSIX separators only; not absolute; no backslashes; no '..'
      - not under '.git/' and not '.git' itself; no empty segments
    """
    if not isinstance(path, str) or not path.strip():
        return False
    if "\\" in path or path.startswith("/"):
        return False
    if path == ".git" or path.startswith(".git/") or "/.git/" in path:
        return False
    if ".." in path.split("/"):
        return False
    return str(PurePosixPath(path)) == path


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
# Blueprint helpers (preflight)
# ─────────────────────────────────────────────────────────────────────────────
def _blueprint_dir(repo: Path) -> Path:
    return (repo / BLUEPRINT_DIR).expanduser().resolve()


def _blueprint_rel_paths() -> List[str]:
    base = PurePosixPath(BLUEPRINT_DIR)
    return [str(base / name) for name, _ in _BLUEPRINT_SPECS]


def _read_file_head(path: Path, max_bytes: int) -> str:
    try:
        data = path.read_bytes()
    except Exception as exc:
        return f"<<error reading {path.name}: {exc}>>"
    data = data[:max_bytes]
    txt = data.decode("utf-8", errors="replace")
    # Normalize EOL for safety
    return txt.replace("\r\n", "\n").replace("\r", "\n")


def _blueprints_summary(repo: Path) -> Optional[str]:
    """
    Return a compact, concatenated summary of existing blueprint docs.
    """
    bdir = _blueprint_dir(repo)
    parts: List[str] = []
    for fname, title in _BLUEPRINT_SPECS:
        p = bdir / fname
        if p.exists() and p.is_file():
            head = _read_file_head(p, BLUEPRINT_SUMMARY_MAX_BYTES // len(_BLUEPRINT_SPECS))
            parts.append(f"# {title}\n{head}".strip())
    if not parts:
        return None
    summary = "\n\n---\n\n".join(parts)
    log.debug("Blueprints summary prepared (%d chars).", len(summary))
    return summary


def _ensure_blueprints(
    *,
    client: Any,
    model: str,
    api_timeout: int,
    repo: Path,
    user_instructions: str,
) -> None:
    """
    Ensure the four blueprint documents exist under BLUEPRINT_DIR.
    For each missing doc, request a **single** `submit_patch` create with full content.
    """
    bdir = _blueprint_dir(repo)
    bdir.mkdir(parents=True, exist_ok=True)

    to_create: List[Tuple[str, str]] = []
    for fname, title in _BLUEPRINT_SPECS:
        p = bdir / fname
        if not p.exists():
            to_create.append((str(PurePosixPath(BLUEPRINT_DIR) / fname), title))

    if not to_create:
        log.info("Blueprint preflight: all documents exist.")
        return

    log.info("Blueprint preflight: %d document(s) missing; creating…", len(to_create))

    tool = _submit_patch_tool()
    tool_name = tool["function"]["name"]

    for rel_path, title in to_create:
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

        # Tighten expectations: must be create on EXACT path
        if patch.get("op") != "create" or patch.get("file") != rel_path or not _is_safe_repo_rel_posix(rel_path):
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

    client = _ensure_client(client, api_timeout)

    # Optional preflight: ensure blueprint documents exist
    if INCLUDE_BLUEPRINTS:
        try:
            _ensure_blueprints(
                client=client,
                model=model,
                api_timeout=api_timeout,
                repo=Path(repo).expanduser().resolve(),
                user_instructions=user_instructions,
            )
        except SystemExit:
            raise
        except Exception as exc:
            log.exception("Blueprint preflight failed: %s", exc)
            raise SystemExit(1) from exc

    # Prepare abridged blueprint summary (optional)
    bp_summary = _blueprints_summary(Path(repo).expanduser().resolve()) if INCLUDE_BLUEPRINTS else None

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
