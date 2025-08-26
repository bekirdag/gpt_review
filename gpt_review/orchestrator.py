#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
===============================================================================
GPT‑Review ▸ Iteration Orchestrator
===============================================================================

Overview
--------
Implements the three‑iteration, plan‑first review workflow:

  0) **Plan‑first** (before touching code):
        • Ask the model for an initial *review plan* (description + run/test
          commands + hints) based on the repository snapshot and instructions.
        • Persist it under `.gpt-review/initial_plan.json` and
          `INITIAL_REVIEW_PLAN.md` to guide the upcoming iterations.

  1) **Iteration 1** (branch "iteration1"):
        • For each *code‑like text* file, request a **COMPLETE file** replacement
          (or KEEP/DELETE) via a forced tool call; apply via `apply_patch.py`.
        • **Commit after every file** to keep a readable history.
        • After all files, ask the model for **new source files** (strict JSON
          list); create them one‑by‑one (full file bodies); **commit each**.

  2) **Iteration 2** (branch "iteration2"):
        • Repeat file‑wise review over *code‑like* files, including newly created.
        • Ask again for additional **new source files** and create them **one by one**,
          committing each created file.

  3) **Iteration 3** (branch "iteration3"):
        • Consistency pass over **all files** (code + deferred bucket).
        • Generate final **plan artifacts**:
            - machine‑readable: `.gpt-review/review_plan.json`
            - human guide     : `REVIEW_GUIDE.md`
          (both are committed).
        • Review/generate **deferred** files now (docs/setup/examples) — commit each.

  4) **Error‑fix loop**:
        • Execute the plan’s commands (run/test). On failure, send logs to the
          model and apply returned **COMPLETE file** fixes; **commit each**.
        • Repeat until commands pass or max rounds reached.

  5) **Push** the final branch.

Strictness
----------
• The model must always return **complete files** (never diffs).
• Docs/install/setup/examples are **deferred** until iteration 3.
• All actions use repo‑root‑relative paths.
• We commit **one file per change** to preserve a readable history.

Logging
-------
INFO for high‑level flow; DEBUG for payloads and tool results.

CLI
---
    python -m gpt_review.orchestrator instructions.txt /path/to/repo \
        --model gpt-5-pro --remote origin --api-timeout 120 \
        --max-error-rounds 6

Environment:
  OPENAI_API_KEY (required), OPENAI_BASE_URL (optional)
"""
from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import textwrap
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from gpt_review import get_logger
from gpt_review.fs_utils import (
    checkout_branch,
    classify_paths,
    current_commit,
    is_binary_file,
    language_census,
    read_text_normalized,
    summarize_repo,
    git,  # reuse git helper for precise, path-scoped commits
)

log = get_logger(__name__)

# =============================================================================
# Config (env‑overridable)
# =============================================================================

OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL")  # optional
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")    # required at runtime

DEFAULT_MODEL = os.getenv("GPT_REVIEW_MODEL", "gpt-5-pro")
DEFAULT_API_TIMEOUT = int(os.getenv("GPT_REVIEW_API_TIMEOUT", "120"))

MAX_PROMPT_BYTES = int(os.getenv("GPT_REVIEW_MAX_PROMPT_BYTES", str(200_000)))
MAX_ERROR_ROUNDS = int(os.getenv("GPT_REVIEW_MAX_ERROR_ROUNDS", "6"))

# When file content exceeds this, we only send head+tail excerpts to the model.
HEAD_TAIL_BYTES = int(os.getenv("GPT_REVIEW_HEAD_TAIL_BYTES", "60000"))

# =============================================================================
# Small OpenAI client shim (local; avoids cross‑module tight coupling)
# =============================================================================


def _ensure_openai_client(api_timeout: int):
    """
    Return an OpenAI client instance. Raises on missing key.

    Type matches the official `openai>=1.0.0` SDK.
    """
    if not OPENAI_API_KEY:
        raise RuntimeError(
            "OPENAI_API_KEY is not set. Export it before running the orchestrator."
        )
    try:
        from openai import OpenAI  # type: ignore
    except Exception as exc:  # pragma: no cover
        raise RuntimeError("The 'openai' package is not installed. pip install openai") from exc

    # Per‑call timeouts are used later; the client itself is lightweight.
    return OpenAI(base_url=OPENAI_BASE_URL, api_key=OPENAI_API_KEY)


# =============================================================================
# Tool schemas (OpenAI "functions")
# =============================================================================


def tool_propose_full_file() -> Dict[str, Any]:
    """
    File‑wise tool: requires COMPLETE file bodies for create/update.
    """
    return {
        "type": "function",
        "function": {
            "name": "propose_full_file",
            "description": (
                "Return a COMPLETE file for the given path (create/update/keep/delete). "
                "ALWAYS return 'content' for action in {'create','update'}. "
                "For 'keep' or 'delete', omit 'content'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative path from repo root."},
                    "action": {
                        "type": "string",
                        "enum": ["create", "update", "keep", "delete"],
                    },
                    "content": {"type": "string", "description": "Full file content when creating/updating."},
                    "notes": {"type": "string", "description": "Short rationale (optional)."},
                },
                "required": ["path", "action"],
                "additionalProperties": False,
            },
        },
    }


def tool_propose_new_files() -> Dict[str, Any]:
    """
    Discovery tool for **source** files only (docs/setup/examples excluded).
    """
    return {
        "type": "function",
        "function": {
            "name": "propose_new_files",
            "description": (
                "Propose new SOURCE files to add now (exclude docs/setup/examples). "
                "Each entry must include the full relative path and the COMPLETE file content."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "new_files": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "required": ["path", "content"],
                            "additionalProperties": False,
                            "properties": {
                                "path": {"type": "string"},
                                "content": {"type": "string"},
                                "notes": {"type": "string"},
                            },
                        },
                    }
                },
                "required": ["new_files"],
                "additionalProperties": False,
            },
        },
    }


def tool_propose_review_plan() -> Dict[str, Any]:
    """
    Planning tool (used at start and end).
    """
    return {
        "type": "function",
        "function": {
            "name": "propose_review_plan",
            "description": (
                "Summarize how to build/run/test this repository. "
                "Provide machine‑actionable commands and a concise description."
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


def tool_propose_error_fixes() -> Dict[str, Any]:
    """
    Error‑fix tool: return **complete file** replacements for impacted files.
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


# =============================================================================
# Utilities
# =============================================================================

@dataclass
class ApplyResult:
    ok: bool
    exit_code: int
    stdout: str
    stderr: str


def _apply_patch(repo: Path, patch: Dict[str, Any]) -> ApplyResult:
    """
    Invoke apply_patch.py with the given patch dict via stdin.

    NOTE: apply_patch.py lives at the project **root**, not inside the package.
    """
    try:
        # Resolve project root (package dir/..)
        apply_tool = Path(__file__).resolve().parent.parent / "apply_patch.py"
        proc = subprocess.run(
            [sys.executable, str(apply_tool), "-", str(repo)],
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


def _commit(repo: Path, message: str, paths: Sequence[str]) -> None:
    """
    Stage only *paths* and commit with *message*. Keeps staging path‑scoped.
    """
    if not paths:
        return
    try:
        git(repo, "add", "--", *paths, check=True)
        git(repo, "commit", "-m", message, check=True)
        log.info("Committed %s", ", ".join(paths))
    except Exception as exc:
        # Make failure explicit; avoid silent divergence between FS and history.
        log.exception("Git commit failed: %s", exc)
        raise


def _apply_full_file(repo: Path, rel_path: str, action: str, content: Optional[str]) -> ApplyResult:
    """
    Convert a full‑file proposal into a schema‑compatible patch and apply it.

    action ∈ {"create","update","keep","delete"}

    Side effect:
      • Commits on success (one file per change), except for keep.
    """
    if action == "keep":
        log.debug("No‑op KEEP for %s", rel_path)
        return ApplyResult(ok=True, exit_code=0, stdout="no-op", stderr="")

    patch: Dict[str, Any]
    if action == "delete":
        patch = {"op": "delete", "file": rel_path, "status": "in_progress"}
    elif action in {"create", "update"}:
        if content is None:
            return ApplyResult(ok=False, exit_code=1, stdout="", stderr="Missing content for create/update")
        patch = {"op": action, "file": rel_path, "body": content, "status": "in_progress"}
    else:
        return ApplyResult(ok=False, exit_code=1, stdout="", stderr=f"Invalid action: {action}")

    res = _apply_patch(repo, patch)
    if not res.ok:
        log.warning(
            "apply_patch failed for %s (%s): rc=%s\nstdout:\n%s\nstderr:\n%s",
            rel_path,
            action,
            res.exit_code,
            res.stdout,
            res.stderr,
        )
        return res

    log.info("Applied: %-6s %s", action, rel_path)
    # Commit on success (one file per change)
    try:
        _commit(repo, f"orchestrator: {action} {rel_path}", [rel_path])
    except Exception:
        # Surface commit failures as non‑fatal here; caller may choose to stop later.
        pass
    return res


def _run_cmd(cmd: str, cwd: Path, timeout: int) -> Tuple[bool, str, int]:
    """
    Run a shell command and return (ok, combined_output, exit_code).
    """
    try:
        proc = subprocess.run(cmd, cwd=str(cwd), shell=True, capture_output=True, text=True, timeout=timeout)
        out = (proc.stdout or "") + (proc.stderr or "")
        return (proc.returncode == 0), out, proc.returncode
    except subprocess.TimeoutExpired as exc:
        out = (exc.stdout or "") + (exc.stderr or "")
        banner = f"TIMEOUT after {timeout}s\n"
        return False, banner + out, 124


def _tail(text: str, n: int = 20000) -> str:
    return text if len(text) <= n else text[-n:]


# =============================================================================
# Prompt builders
# =============================================================================

def _system_prompt(iteration: int, deferred_hint: bool) -> str:
    """
    Compact system prompt; enforces *full‑file* outputs and deferral rules.
    """
    defer_msg = (
        "Do NOT modify documentation/installation/setup/example files in this pass; "
        "we will handle them in iteration 3."
        if deferred_hint
        else "This pass may include documentation and setup files."
    )
    return (
        "You are GPT‑Review operating in **chunk‑by‑chunk** mode. "
        "We will first establish a plan, then fix files **one by one**. "
        "For each request, you MUST respond only by calling the provided function, "
        "returning a **COMPLETE file** (no diffs) or declaring KEEP/DELETE. "
        f"{defer_msg} Keep changes minimal and precise."
    )


def _file_review_prompt(
    *,
    instructions: str,
    repo_summary: str,
    census: List[str],
    rel_path: str,
    file_excerpt: str,
    iteration: int,
) -> str:
    header = textwrap.dedent(
        f"""
        Review iteration {iteration} – file: `{rel_path}`.

        Project instructions:
        {instructions.strip()}

        Repository overview (sampled paths):
        ```
        {repo_summary}
        ```

        Language census: {", ".join(census)}

        The file below is the **current ground truth**. Return a COMPLETE file
        if changes are required; otherwise return KEEP. Avoid unrelated edits.
        """
    ).strip()

    body = f"\n\n--- FILE START `{rel_path}` ---\n{file_excerpt}\n--- FILE END ---\n"
    return header + body


def _new_files_prompt(*, instructions: str, repo_summary: str, iteration: int) -> str:
    return textwrap.dedent(
        f"""
        Iteration {iteration} – discovery of missing **source** files.

        Based on the current repository state (below) and the project instructions,
        propose additional **source** files that should be present *now* to make the
        software coherent. Exclude documentation/installation/setup/examples for
        this iteration.

        Return ONLY a function call with `new_files=[{{path, content}}]` (full content).

        Repository overview:
        ```
        {repo_summary}
        ```
        """
    ).strip()


def _plan_prompt(*, instructions: str, repo_summary: str, phase: str) -> str:
    """
    'phase' is 'initial' (before edits) or 'final' (after consistency pass).
    """
    preface = (
        "Before we start editing files, produce an initial execution plan with "
        "**actionable commands** to run the software and (optionally) tests on a clean machine."
        if phase == "initial"
        else "We have completed the third iteration of code review. Produce a concise execution plan with "
             "**actionable commands** to run the software and its tests."
    )
    return textwrap.dedent(
        f"""
        {preface}

        Return ONLY a function call `propose_review_plan` with:
          - run_commands: list[str]  (required)
          - test_commands: list[str] (optional)
          - description: str
          - hints: list[str] (optional)

        Instructions:
        {instructions.strip()}

        Repository overview:
        ```
        {repo_summary}
        ```
        """
    ).strip()


def _error_fix_prompt(*, combined_errors: str, last_commands: List[str]) -> str:
    cmds = "\n".join(f"$ {c}" for c in last_commands)
    return textwrap.dedent(
        f"""
        The following commands were executed and produced errors:

        {cmds}

        Error logs (tail, possibly truncated):
        ```text
        {combined_errors}
        ```

        Please return ONLY a function call `propose_error_fixes` with a list of
        COMPLETE file replacements (edits=[{{path, action, content?}}]) to fix
        the issues. Avoid unrelated changes. Limit to the minimal set of files.
        """
    ).strip()


# =============================================================================
# Chat utilities
# =============================================================================

def _prune_messages(msgs: List[Dict[str, Any]], keep: int = 12) -> List[Dict[str, Any]]:
    """
    Keep the last *keep* messages to control token growth. Always keep the first 2.
    """
    if len(msgs) <= 2:
        return msgs
    return msgs[:2] + msgs[-keep:]


def _call_tool_only(
    client,
    *,
    model: str,
    api_timeout: int,
    messages: List[Dict[str, Any]],
    tool_schema: Dict[str, Any],
) -> Tuple[Dict[str, Any], str]:
    """
    Force a single function/tool call. Returns (tool_args_dict, call_id).
    """
    tool_name = tool_schema["function"]["name"]
    resp = client.chat.completions.create(
        model=model,
        messages=messages,
        temperature=0,
        tools=[tool_schema],
        tool_choice={"type": "function", "function": {"name": tool_name}},
        timeout=api_timeout,  # type: ignore[arg-type]
    )
    choice = resp.choices[0]
    msg = choice.message
    calls = getattr(msg, "tool_calls", None) or []
    if not calls:
        raise RuntimeError(f"Assistant did not call {tool_name}")
    tc = calls[0]
    fn = tc.function
    if getattr(fn, "name", None) != tool_name:
        raise RuntimeError(f"Unexpected tool name: {fn.name}")
    # The SDK returns JSON as a string in fn.arguments
    args = json.loads(fn.arguments or "{}")
    return args, getattr(tc, "id", "call_0")


def _excerpt_for_prompt(p: Path) -> str:
    """
    Return either the whole file (when small) or head+tail excerpts to keep
    prompts within a safe token budget. Always normalize EOL to LF.
    """
    try:
        data = p.read_bytes()
    except Exception as exc:
        return f"<<error reading file: {exc}>>"

    if len(data) <= MAX_PROMPT_BYTES:
        try:
            return read_text_normalized(p)
        except Exception:
            # Fall back to a lossy decode if necessary
            return data.decode("utf-8", errors="replace").replace("\r\n", "\n").replace("\r", "\n")

    head = data[: HEAD_TAIL_BYTES]
    tail = data[-HEAD_TAIL_BYTES :]
    h = head.decode("utf-8", errors="replace")
    t = tail.decode("utf-8", errors="replace")
    return f"<<EXCERPT: file too large ({len(data)} bytes); sending head+tail>>\n{h}\n…\n{t}"


# =============================================================================
# Core orchestration helpers
# =============================================================================

def _apply_new_files(repo: Path, new_files: List[Dict[str, Any]]) -> None:
    for item in new_files:
        rel = (item.get("path") or "").strip()
        content = item.get("content")
        if not rel or content is None:
            log.warning("Skipping malformed new file entry: %r", item)
            continue
        if Path(rel).parts and rel.split("/")[0] == ".git":
            log.warning("Rejecting unsafe target under .git/: %s", rel)
            continue
        res = _apply_full_file(repo, rel, "create", content)
        if not res.ok:
            raise RuntimeError(f"Failed to create new file: {rel}")


def _review_files_in_bucket(
    *,
    client,
    model: str,
    api_timeout: int,
    repo: Path,
    files: Sequence[Path],
    instructions: str,
    iteration: int,
    deferred_hint: bool,
) -> None:
    """
    Review each file in *files*; apply changes via `apply_patch.py` and commit them.
    """
    repo_summary = summarize_repo(repo)
    census = language_census(files)

    system_msg = {"role": "system", "content": _system_prompt(iteration, deferred_hint)}
    messages: List[Dict[str, Any]] = [system_msg]

    for p in files:
        rel = p.relative_to(repo).as_posix()

        if is_binary_file(p):
            log.info("Skipping binary file: %s", rel)
            continue

        prompt = _file_review_prompt(
            instructions=instructions,
            repo_summary=repo_summary,
            census=census,
            rel_path=rel,
            file_excerpt=_excerpt_for_prompt(p),
            iteration=iteration,
        )

        messages = _prune_messages(messages)
        messages.append({"role": "user", "content": prompt})

        try:
            args, call_id = _call_tool_only(
                client,
                model=model,
                api_timeout=api_timeout,
                messages=messages,
                tool_schema=tool_propose_full_file(),
            )
        except Exception as exc:
            log.exception("Model call failed for %s: %s", rel, exc)
            continue

        # Record the assistant tool-call message before sending the tool result
        messages.append(
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": call_id,
                        "function": {
                            "name": "propose_full_file",
                            "arguments": json.dumps(args, ensure_ascii=False),
                        },
                    }
                ],
            }
        )

        # Validate and apply
        action = (args.get("action") or "").strip()
        path = (args.get("path") or "").strip()
        content = args.get("content")

        if not path or path != rel:
            log.warning("Ignoring tool result with mismatched path. got=%r expected=%r", path, rel)
            continue

        res = _apply_full_file(repo, rel, action, content)
        if not res.ok:
            raise RuntimeError(f"Failed to apply {action} to {rel}: rc={res.exit_code}")


def _discover_new_files(
    *,
    client,
    model: str,
    api_timeout: int,
    repo: Path,
    instructions: str,
    iteration: int,
) -> None:
    repo_summary = summarize_repo(repo)
    system_msg = {"role": "system", "content": _system_prompt(iteration, deferred_hint=True)}
    messages: List[Dict[str, Any]] = [
        system_msg,
        {
            "role": "user",
            "content": _new_files_prompt(
                instructions=instructions, repo_summary=repo_summary, iteration=iteration
            ),
        },
    ]

    try:
        args, _ = _call_tool_only(
            client,
            model=model,
            api_timeout=api_timeout,
            messages=messages,
            tool_schema=tool_propose_new_files(),
        )
    except Exception as exc:
        log.info("No new files proposed (or tool call failed): %s", exc)
        return

    new_files = args.get("new_files") or []
    if not isinstance(new_files, list) or not new_files:
        log.info("Model returned no new files for iteration %d.", iteration)
        return

    log.info("Model proposed %d new file(s) in iteration %d.", len(new_files), iteration)
    _apply_new_files(repo, new_files)


def _generate_plan_artifacts(
    *,
    phase: str,  # "initial" or "final"
    client,
    model: str,
    api_timeout: int,
    repo: Path,
    instructions: str,
) -> Tuple[List[str], List[str]]:
    """
    Create plan artifacts for a given *phase*.

    Returns (run_commands, test_commands). When 'initial', artifacts are:
        .gpt-review/initial_plan.json, INITIAL_REVIEW_PLAN.md
    When 'final', artifacts are:
        .gpt-review/review_plan.json, REVIEW_GUIDE.md
    """
    repo_summary = summarize_repo(repo)
    system_msg = {"role": "system", "content": _system_prompt(iteration=1 if phase == "initial" else 3, deferred_hint=False)}
    messages: List[Dict[str, Any]] = [
        system_msg,
        {"role": "user", "content": _plan_prompt(instructions=instructions, repo_summary=repo_summary, phase=phase)},
    ]

    args, _ = _call_tool_only(
        client,
        model=model,
        api_timeout=api_timeout,
        messages=messages,
        tool_schema=tool_propose_review_plan(),
    )

    description = args.get("description") or ""
    run_cmds = [c for c in (args.get("run_commands") or []) if isinstance(c, str) and c.strip()]
    test_cmds = [c for c in (args.get("test_commands") or []) if isinstance(c, str) and c.strip()]
    hints = [h for h in (args.get("hints") or []) if isinstance(h, str) and h.strip()]

    # Ensure the dot‑dir exists by creating a harmless .keep via apply_patch
    res_keep = _apply_full_file(repo, ".gpt-review/.keep", "create", "")
    if not res_keep.ok:
        raise RuntimeError("Failed to create .gpt-review/.keep")

    if phase == "initial":
        plan_path = ".gpt-review/initial_plan.json"
        guide_path = "INITIAL_REVIEW_PLAN.md"
        heading = "# Initial Review Plan"
    else:
        plan_path = ".gpt-review/review_plan.json"
        guide_path = "REVIEW_GUIDE.md"
        heading = "# Review Guide"

    plan_json = {
        "phase": phase,
        "description": description,
        "run_commands": run_cmds,
        "test_commands": test_cmds,
        "hints": hints,
        "generated_by": "gpt_review.orchestrator",
    }
    guide_md = textwrap.dedent(
        f"""
        {heading}

        {description.strip() or "_(no description returned)_"}

        ## Run commands
        {"".join(f"- `{shlex.join(shlex.split(x))}`\n" for x in run_cmds) or "_none_"}

        ## Test commands
        {"".join(f"- `{shlex.join(shlex.split(x))}`\n" for x in test_cmds) or "_none_"}

        ## Hints
        {"".join(f"- {h}\n" for h in hints) or "_none_"}
        """
    ).strip() + "\n"

    res1 = _apply_full_file(
        repo, plan_path, "create", json.dumps(plan_json, indent=2, ensure_ascii=False) + "\n"
    )
    res2 = _apply_full_file(repo, guide_path, "create", guide_md)

    if not (res1.ok and res2.ok):
        raise RuntimeError(f"Failed to create {phase} plan artifacts")

    return run_cmds, test_cmds


def _deferred_bucket(repo: Path) -> List[Path]:
    """
    Return documentation/setup/example files (deferred until iteration 3).
    """
    _code, deferred = classify_paths(repo)
    return deferred


# =============================================================================
# Error‑fix loop
# =============================================================================

def _error_fix_prompt_commands(commands: List[str]) -> str:
    return "\n".join(f"- {c}" for c in commands) or "_none_"


def _run_error_fix_loop(
    *,
    client,
    model: str,
    api_timeout: int,
    repo: Path,
    instructions: str,
    run_cmds: List[str],
    test_cmds: List[str],
    max_rounds: int,
    timeout: int,
) -> None:
    """
    Execute commands and iteratively fix errors via COMPLETE file replacements.
    """
    commands = [*run_cmds, *test_cmds]
    if not commands:
        log.info("No run/test commands provided; skipping error‑fix loop.")
        return

    log.info("Starting error‑fix loop with commands:\n%s", _error_fix_prompt_commands(commands))

    for round_idx in range(1, max_rounds + 1):
        logs: List[str] = []
        all_ok = True
        for cmd in commands:
            ok, out, code = _run_cmd(cmd, cwd=repo, timeout=timeout)
            logs.append(f"$ {cmd}\n(exit={code})\n{out}\n")
            if not ok:
                all_ok = False

        combined = "\n".join(logs)[-MAX_PROMPT_BYTES:]
        if all_ok:
            log.info("All commands passed on round %d.", round_idx)
            return

        log.warning("Errors detected (round %d). Sending logs to model.", round_idx)

        # Build prompt & call
        system_msg = {"role": "system", "content": _system_prompt(iteration=3, deferred_hint=False)}
        messages: List[Dict[str, Any]] = [
            system_msg,
            {"role": "user", "content": _error_fix_prompt(combined_errors=combined, last_commands=commands)},
        ]
        args, _ = _call_tool_only(
            client,
            model=model,
            api_timeout=api_timeout,
            messages=messages,
            tool_schema=tool_propose_error_fixes(),
        )

        edits = args.get("edits") or []
        if not isinstance(edits, list) or not edits:
            log.warning("Model returned no edits in error‑fix round %d.", round_idx)
            continue

        applied_any = False
        for e in edits:
            path = (e.get("path") or "").strip()
            action = (e.get("action") or "").strip()
            content = e.get("content")
            if not path or action not in {"create", "update", "delete"}:
                log.warning("Skipping malformed edit: %r", e)
                continue
            res = _apply_full_file(repo, path, action, content)
            applied_any = applied_any or res.ok

        if not applied_any:
            log.warning("No edits could be applied in round %d.", round_idx)

    log.error("Exceeded maximum error‑fix rounds (%d).", max_rounds)
    # Intentionally do not raise; leave the branch with best effort changes.


# =============================================================================
# Branching & pipeline
# =============================================================================

def _current_branch(repo: Path) -> str:
    try:
        out = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        return out or "HEAD"
    except Exception:
        return "HEAD"


def _push_branch(repo: Path, branch: str, remote: Optional[str]) -> None:
    if not remote:
        log.info("No remote configured for push; skipping.")
        return
    try:
        subprocess.run(["git", "-C", str(repo), "push", "-u", remote, branch], check=True)
        log.info("Pushed branch '%s' to remote '%s'.", branch, remote)
    except subprocess.CalledProcessError as exc:
        log.warning("Push failed for %s/%s: %s", remote, branch, exc)


def _iteration_branch_name(i: int) -> str:
    return f"iteration{i}"


def run_iterations(
    *,
    instructions_path: Path,
    repo: Path,
    model: str,
    api_timeout: int,
    remote: Optional[str],
    timeout: int,
    max_error_rounds: int,
) -> None:
    """
    High‑level orchestration entrypoint (plan‑first + 3 iterations + error fix).
    """
    client = _ensure_openai_client(api_timeout)
    instr = instructions_path.read_text(encoding="utf-8").strip()

    # Initial classification (for logging only)
    code_like, deferred = classify_paths(repo)
    log.info("Initial classification → code: %d, deferred: %d", len(code_like), len(deferred))

    base_branch = _current_branch(repo)
    log.info("Starting from branch '%s' at %s", base_branch, current_commit(repo))

    # ── Iteration 1 ──────────────────────────────────────────────────────────
    b1 = _iteration_branch_name(1)
    checkout_branch(repo, b1)

    # Plan‑first artifacts (initial) — guides the upcoming edits
    try:
        _generate_plan_artifacts(
            phase="initial",
            client=client,
            model=model,
            api_timeout=api_timeout,
            repo=repo,
            instructions=instr,
        )
    except Exception as exc:
        log.warning("Initial plan artifacts step failed: %s (continuing).", exc)

    code_like, _ = classify_paths(repo)
    _review_files_in_bucket(
        client=client,
        model=model,
        api_timeout=api_timeout,
        repo=repo,
        files=code_like,
        instructions=instr,
        iteration=1,
        deferred_hint=True,
    )
    _discover_new_files(
        client=client, model=model, api_timeout=api_timeout, repo=repo, instructions=instr, iteration=1
    )
    _push_branch(repo, b1, remote)

    # ── Iteration 2 ──────────────────────────────────────────────────────────
    b2 = _iteration_branch_name(2)
    checkout_branch(repo, b2)
    code_like, _ = classify_paths(repo)  # refresh (there may be new files)
    _review_files_in_bucket(
        client=client,
        model=model,
        api_timeout=api_timeout,
        repo=repo,
        files=code_like,
        instructions=instr,
        iteration=2,
        deferred_hint=True,
    )
    _discover_new_files(
        client=client, model=model, api_timeout=api_timeout, repo=repo, instructions=instr, iteration=2
    )
    _push_branch(repo, b2, remote)

    # ── Iteration 3 ──────────────────────────────────────────────────────────
    b3 = _iteration_branch_name(3)
    checkout_branch(repo, b3)

    # Consistency pass over EVERYTHING (code + deferred)
    code_like, deferred = classify_paths(repo)
    all_files = [*code_like, *deferred]
    _review_files_in_bucket(
        client=client,
        model=model,
        api_timeout=api_timeout,
        repo=repo,
        files=all_files,
        instructions=instr,
        iteration=3,
        deferred_hint=False,
    )

    # Final plan artifacts (after consistency)
    run_cmds, test_cmds = _generate_plan_artifacts(
        phase="final",
        client=client,
        model=model,
        api_timeout=api_timeout,
        repo=repo,
        instructions=instr,
    )

    # Now explicitly (re)visit deferred files (docs/setup/examples) — they may be
    # generated/updated after code stabilization, in case the previous pass skipped.
    deferred = _deferred_bucket(repo)
    if deferred:
        log.info("Reviewing deferred files (%d).", len(deferred))
        _review_files_in_bucket(
            client=client,
            model=model,
            api_timeout=api_timeout,
            repo=repo,
            files=deferred,
            instructions=instr,
            iteration=3,
            deferred_hint=False,
        )

    # Error‑fix loop
    _run_error_fix_loop(
        client=client,
        model=model,
        api_timeout=api_timeout,
        repo=repo,
        instructions=instr,
        run_cmds=run_cmds,
        test_cmds=test_cmds,
        max_rounds=max_error_rounds,
        timeout=timeout,
    )

    _push_branch(repo, b3, remote)
    log.info("Orchestration completed. Final branch: %s", b3)


# =============================================================================
# CLI
# =============================================================================

def _cli() -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="gpt-review-iterate")
    p.add_argument("instructions", help="Path to plain‑text project instructions.")
    p.add_argument("repo", help="Path to the Git repository.")
    p.add_argument("--model", default=DEFAULT_MODEL, help="OpenAI model id (default: %(default)s)")
    p.add_argument("--api-timeout", type=int, default=DEFAULT_API_TIMEOUT, help="HTTP timeout (seconds).")
    p.add_argument("--remote", default=os.getenv("GPT_REVIEW_REMOTE", "origin"), help="Git remote name to push.")
    p.add_argument(
        "--timeout",
        type=int,
        default=int(os.getenv("GPT_REVIEW_COMMAND_TIMEOUT", "300")),
        help="Timeout for run/test commands (seconds).",
    )
    p.add_argument(
        "--max-error-rounds",
        type=int,
        default=MAX_ERROR_ROUNDS,
        help="Max rounds in the error‑fix loop.",
    )
    return p.parse_args()


def main() -> None:
    args = _cli()
    repo = Path(args.repo).expanduser().resolve()
    if not (repo / ".git").exists():
        sys.exit(f"❌ Not a git repository: {repo}")

    try:
        run_iterations(
            instructions_path=Path(args.instructions).expanduser().resolve(),
            repo=repo,
            model=args.model,
            api_timeout=args.api_timeout,
            remote=args.remote or None,
            timeout=args.timeout,
            max_error_rounds=args.max_error_rounds,
        )
    except SystemExit:
        raise
    except KeyboardInterrupt:
        log.info("Interrupted by user (Ctrl‑C).")
        sys.exit(130)
    except Exception as exc:
        log.exception("Fatal error in orchestrator: %s", exc)
        sys.exit(1)


if __name__ == "__main__":
    main()
