#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
===============================================================================
GPT‑Review ▸ Iteration Orchestrator
===============================================================================

Overview
--------
Implements the three‑iteration, plan‑first review workflow **with blueprint
documents generated up-front** and strict full‑file review semantics:

  0) **Preflight & Blueprints**:
        • If the CLI 'repo' looks like a Git URL, clone into a temp workdir.
        • Generate (if missing) and persist four blueprint docs:
            - WHITEPAPER & Engineering Blueprint
            - BUILD GUIDE
            - SDS (Software Design Specifications)
            - PROJECT CODE FILES & INSTRUCTIONS
          These are saved under the canonical directory
          `.gpt-review/blueprints/*.md` and written via apply_patch.py
          (which stages & commits each file).

  1) **Plan‑first** (before touching code):
        • Ask the model for an initial *review plan* (description + run/test
          commands + hints) using the repository snapshot **and** blueprint summary.
        • Persist under `.gpt-review/initial_plan.json` and `INITIAL_REVIEW_PLAN.md`.

  2) **Iteration 1** (branch "iteration1"):
        • For each *code‑like text* file, request a **COMPLETE file** replacement
          (or KEEP/DELETE). We **send full file contents** (no excerpts).
        • **Commit after every file** is handled by `apply_patch.py`.
        • After all files, ask the model for **new source files**; create them
          one‑by‑one (full content). Each write is committed by `apply_patch.py`.

  3) **Iteration 2** (branch "iteration2"):
        • Repeat file‑wise review over *code‑like* files, including newly created.
        • Ask again for additional **new source files** and create them one by one.

  4) **Iteration 3** (branch "iteration3"):
        • Consistency pass over **all files** (code + deferred).
        • Generate final **plan artifacts**:
            - machine‑readable: `.gpt-review/review_plan.json`
            - human guide     : `REVIEW_GUIDE.md`
          (both are written and committed by `apply_patch.py`).
        • Review/generate **deferred** files now (docs/setup/examples).

  5) **Error‑fix loop**:
        • Execute the plan’s commands (run/test). On failure, send logs to the
          model and apply returned **COMPLETE file** fixes. Each write is committed
          by `apply_patch.py`. Repeat until commands pass or max rounds reached.

  6) **Push** the final branch and **create a Pull Request** (when possible).

Strictness
----------
• The model must always return **complete files** (never diffs).
• Docs/install/setup/examples are **deferred** until iteration 3 (except the
  blueprint documents generated up‑front for context).
• All actions use repo‑root‑relative POSIX paths.
• We write via `apply_patch.py`, which performs **path‑scoped staging and commits**
  (one file per change) with safety checks.

CLI
---
    python -m gpt_review.orchestrator instructions.txt <repo-or-url> \
        --model gpt-5-pro --remote origin --api-timeout 120 \
        --max-error-rounds 6

Environment:
  OPENAI_API_KEY (required), OPENAI_BASE_URL (optional)
  ALWAYS_SEND_FULL_FILE=1 (default) → always send full file contents to the API
  GPT_REVIEW_CREATE_PR=1           → attempt to open a GitHub PR at the end
  GITHUB_TOKEN / GH_TOKEN          → token for 'gh' CLI or API auth
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
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
)
from gpt_review.blueprints_util import (  # central blueprint helpers
    ensure_blueprint_dir,
    blueprint_paths,
    summarize_blueprints,
    blueprints_exist,
    normalize_markdown,
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

# When file content exceeds this, earlier versions excerpted head+tail. We now
# prefer **full files** by default (can be overridden).
ALWAYS_SEND_FULL_FILE = os.getenv("ALWAYS_SEND_FULL_FILE", "1").strip().lower() not in {"0", "false", "no", ""}

# =============================================================================
# OpenAI client shim (local; avoids cross‑module tight coupling)
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
    return OpenAI(base_url=OPENAI_BASE_URL, api_key=OPENAI_API_KEY)


# =============================================================================
# Tool schemas (OpenAI "functions")
# =============================================================================

def tool_propose_full_file() -> Dict[str, Any]:
    """File‑wise tool: requires COMPLETE file bodies for create/update."""
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
                    "action": {"type": "string", "enum": ["create", "update", "keep", "delete"]},
                    "content": {"type": "string", "description": "Full file content when creating/updating."},
                    "notes": {"type": "string", "description": "Short rationale (optional)."},
                },
                "required": ["path", "action"],
                "additionalProperties": False,
            },
        },
    }


def tool_propose_new_files() -> Dict[str, Any]:
    """Discovery tool for **source** files only (docs/setup/examples excluded)."""
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
    """Planning tool (used at start and end)."""
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
    """Error‑fix tool: return **complete file** replacements for impacted files."""
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


def tool_generate_blueprints() -> Dict[str, Any]:
    """
    Blueprint tool: request the four required documents in one response.

    The orchestrator saves them under .gpt-review/blueprints/*.md.
    """
    return {
        "type": "function",
        "function": {
            "name": "generate_blueprints",
            "description": (
                "Generate the four foundational documents (Markdown): "
                "whitepaper, build_guide, sds, and project_instructions."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "whitepaper": {"type": "string", "description": "Whitepaper & Engineering Blueprint (Markdown)"},
                    "build_guide": {"type": "string", "description": "Build Guide (Markdown)"},
                    "sds": {"type": "string", "description": "Software Design Specifications (Markdown)"},
                    "project_instructions": {"type": "string", "description": "Project Code Files & Instructions (Markdown)"},
                },
                "required": ["whitepaper", "build_guide", "sds", "project_instructions"],
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
    It performs path‑scoped staging **and commits** on success.
    """
    try:
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


def _run_cmd(cmd: str, cwd: Path, timeout: int) -> Tuple[bool, str, int]:
    """Run a shell command and return (ok, combined_output, exit_code)."""
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


# -----------------------------------------------------------------------------
# Repo acquisition helpers (clone-if-URL)
# -----------------------------------------------------------------------------
_GIT_URL_RE = re.compile(r"^(?:https?://|git@|ssh://).*|.*\.git$")

def _looks_like_git_url(arg: str) -> bool:
    try:
        return bool(_GIT_URL_RE.match(arg.strip()))
    except Exception:
        return False


def _clone_repo_to_temp(url: str) -> Path:
    tmpdir = Path(tempfile.mkdtemp(prefix="gpt-review-"))
    log.info("Cloning %s into %s …", url, tmpdir)
    subprocess.run(["git", "clone", "--depth", "1", url, str(tmpdir)], check=True)
    return tmpdir


# =============================================================================
# Prompt builders
# =============================================================================

def _system_prompt(iteration: int, deferred_hint: bool) -> str:
    """Compact system prompt; enforces *full‑file* outputs and deferral rules."""
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
    file_text: str,
    iteration: int,
    blueprints_summary: str,
) -> str:
    header = textwrap.dedent(
        f"""
        Review iteration {iteration} – file: `{rel_path}`.

        Project instructions:
        {instructions.strip()}

        Blueprint documents (abridged):
        {blueprints_summary}

        Repository overview (sampled paths):
        ```
        {repo_summary}
        ```

        Language census: {", ".join(census)}

        The file below is the **current ground truth**. Return a COMPLETE file
        if changes are required; otherwise return KEEP. Avoid unrelated edits.
        """
    ).strip()
    body = f"\n\n--- FILE START `{rel_path}` ---\n{file_text}\n--- FILE END ---\n"
    return header + body


def _new_files_prompt(*, instructions: str, repo_summary: str, iteration: int, blueprints_summary: str) -> str:
    return textwrap.dedent(
        f"""
        Iteration {iteration} – discovery of missing **source** files.

        Blueprint documents (abridged):
        {blueprints_summary}

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


def _plan_prompt(*, instructions: str, repo_summary: str, phase: str, blueprints_summary: str) -> str:
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

        Blueprint documents (abridged):
        {blueprints_summary}

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


def _error_fix_prompt(*, combined_errors: str, last_commands: List[str], blueprints_summary: str) -> str:
    cmds = "\n".join(f"$ {c}" for c in last_commands)
    return textwrap.dedent(
        f"""
        The following commands were executed and produced errors:

        {cmds}

        Blueprint documents (abridged):
        {blueprints_summary}

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
    """Keep the last *keep* messages to control token growth. Always keep the first 2."""
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
    """Force a single function/tool call. Returns (tool_args_dict, call_id)."""
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
    args = json.loads(fn.arguments or "{}")
    return args, getattr(tc, "id", "call_0")


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


def _excerpt_for_prompt(p: Path) -> str:
    """
    Return the whole file (LF-normalized) by default. If ALWAYS_SEND_FULL_FILE=0,
    fall back to head+tail excerpting for very large files.
    """
    if ALWAYS_SEND_FULL_FILE:
        try:
            return read_text_normalized(p)
        except Exception:
            data = p.read_bytes()
            return data.decode("utf-8", errors="replace").replace("\r\n", "\n").replace("\r", "\n")

    # Fallback (rare): excerpt extremely large files to avoid runaway tokens.
    data = p.read_bytes()
    if len(data) <= MAX_PROMPT_BYTES:
        return data.decode("utf-8", errors="replace").replace("\r\n", "\n").replace("\r", "\n")
    head = data[: int(MAX_PROMPT_BYTES // 2)]
    tail = data[-int(MAX_PROMPT_BYTES // 2):]
    h = head.decode("utf-8", errors="replace")
    t = tail.decode("utf-8", errors="replace")
    return f"<<EXCERPT: file too large ({len(data)} bytes); sending head+tail>>\n{h}\n…\n{t}"


def _apply_full_file(repo: Path, rel_path: str, action: str, content: Optional[str]) -> ApplyResult:
    """
    Convert a full‑file proposal into a schema‑compatible patch and apply it.

    action ∈ {"create","update","keep","delete"}

    Side effect:
      • Writes and commits are performed by `apply_patch.py`. For 'keep' we no‑op.
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
            rel_path, action, res.exit_code, res.stdout, res.stderr,
        )
    else:
        log.info("Applied: %-6s %s", action, rel_path)
    return res


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
    blueprints_summary: str,
) -> None:
    """
    Review each file in *files*; apply changes via `apply_patch.py`.
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
            file_text=_excerpt_for_prompt(p),
            iteration=iteration,
            blueprints_summary=blueprints_summary,
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
                        "function": {"name": "propose_full_file", "arguments": json.dumps(args, ensure_ascii=False)},
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
    blueprints_summary: str,
) -> None:
    repo_summary = summarize_repo(repo)
    system_msg = {"role": "system", "content": _system_prompt(iteration, deferred_hint=True)}
    messages: List[Dict[str, Any]] = [
        system_msg,
        {
            "role": "user",
            "content": _new_files_prompt(
                instructions=instructions,
                repo_summary=repo_summary,
                iteration=iteration,
                blueprints_summary=blueprints_summary,
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
    blueprints_summary: str,
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
        {"role": "user", "content": _plan_prompt(instructions=instructions, repo_summary=repo_summary, phase=phase, blueprints_summary=blueprints_summary)},
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
    _ = _apply_full_file(repo, ".gpt-review/.keep", "create", "")

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


# -----------------------------------------------------------------------------
# Blueprints generation (uses blueprints_util)
# -----------------------------------------------------------------------------

def _generate_blueprints(
    *,
    client,
    model: str,
    api_timeout: int,
    repo: Path,
    instructions: str,
) -> None:
    """
    Generate the four blueprint docs if missing, and log paths.
    Each created file is written & committed by apply_patch.py.
    """
    ensure_blueprint_dir(repo)
    if blueprints_exist(repo):
        log.info("Blueprint documents already present; skipping generation.")
        return

    repo_summary = summarize_repo(repo)
    system_msg = {"role": "system", "content": _system_prompt(iteration=1, deferred_hint=True)}
    messages: List[Dict[str, Any]] = [
        system_msg,
        {
            "role": "user",
            "content": textwrap.dedent(
                f"""
                Generate the following four Markdown documents **in one function call**:

                1) Whitepaper & Engineering Blueprint
                2) Build Guide
                3) Project Software Design Specifications (SDS)
                4) Project Code Files & Instructions

                These documents must be **self‑contained** and tailored to the repository.

                Inputs:
                • Project instructions (user): {instructions.strip()}
                • Repository overview:
                ```
                {repo_summary}
                ```

                Return ONLY the tool call `generate_blueprints` with fields:
                - whitepaper
                - build_guide
                - sds
                - project_instructions
                """
            ).strip(),
        },
    ]

    args, _ = _call_tool_only(
        client,
        model=model,
        api_timeout=api_timeout,
        messages=messages,
        tool_schema=tool_generate_blueprints(),
    )

    docs = {
        "whitepaper": normalize_markdown(args.get("whitepaper") or ""),
        "build_guide": normalize_markdown(args.get("build_guide") or ""),
        "sds": normalize_markdown(args.get("sds") or ""),
        "project_instructions": normalize_markdown(args.get("project_instructions") or ""),
    }

    paths = blueprint_paths(repo)  # absolute Paths from blueprints_util
    repo_root = repo.expanduser().resolve()
    created: List[str] = []

    # Create **only missing** documents; leave existing ones untouched.
    for key, path in paths.items():
        try:
            rel_posix = path.relative_to(repo_root).as_posix()
        except Exception:
            rel_posix = path.as_posix()

        if path.exists():
            log.info("Blueprint already exists; keeping as is: %s", rel_posix)
            continue

        content = docs.get(key, "")
        patch = {"op": "create", "file": rel_posix, "body": content, "status": "in_progress"}
        res = _apply_patch(repo, patch)
        if not res.ok:
            raise RuntimeError(f"Failed to create blueprint {key}: {res.stderr or res.stdout}")
        created.append(rel_posix)

    if created:
        log.info("Blueprint documents created: %s", ", ".join(created))
    else:
        log.info("No blueprint documents were created (unexpected — none missing?).")


def _deferred_bucket(repo: Path) -> List[Path]:
    """Return documentation/setup/example files (deferred until iteration 3)."""
    _code, deferred = classify_paths(repo)
    return deferred


# -----------------------------------------------------------------------------
# PR creation
# -----------------------------------------------------------------------------

def _default_remote_branch(repo: Path, remote: str = "origin") -> str:
    """
    Try to resolve origin/HEAD → base branch; fallback to 'main' then 'master'.
    """
    try:
        out = subprocess.run(
            ["git", "-C", str(repo), "symbolic-ref", "-q", f"refs/remotes/{remote}/HEAD"],
            capture_output=True, text=True, check=False,
        ).stdout.strip()
        if out:
            # e.g., refs/remotes/origin/main → 'main'
            return out.rsplit("/", 1)[-1]
    except Exception:
        pass
    for cand in ("main", "master"):
        ok = subprocess.run(["git", "-C", str(repo), "show-ref", "--verify", "--quiet", f"refs/heads/{cand}"]).returncode == 0
        if ok:
            return cand
    return "main"


def _which(cmd: str) -> Optional[str]:
    from shutil import which
    return which(cmd)


def _maybe_create_pull_request(repo: Path, *, branch: str, remote: Optional[str]) -> None:
    """
    Attempt to open a PR for the current branch using 'gh' CLI if available.
    """
    if not os.getenv("GPT_REVIEW_CREATE_PR"):
        log.info("PR creation disabled (set GPT_REVIEW_CREATE_PR=1 to enable).")
        return
    if not remote:
        log.info("No remote configured; skipping PR creation.")
        return

    base = _default_remote_branch(repo, remote)
    title = f"GPT‑Review: {branch}"
    body = "Automated multi‑iteration review by GPT‑Review. See REVIEW_GUIDE.md and .gpt-review/review_plan.json."

    gh = _which("gh")
    if gh:
        try:
            subprocess.run(
                ["gh", "pr", "create", "--repo", ".", "--base", base, "--head", branch, "--title", title, "--body", body],
                cwd=str(repo),
                check=True,
            )
            log.info("Pull request created via gh CLI (base=%s, head=%s).", base, branch)
            return
        except subprocess.CalledProcessError as exc:
            log.warning("gh pr create failed: %s", exc)

    # Fallback: print guidance
    log.info("PR not created automatically. You can create one with:\n"
             "  gh pr create --base %s --head %s --title %r --body %r", base, branch, title, body)


# =============================================================================
# High‑level orchestration
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
    High‑level orchestration entrypoint (blueprints + plan‑first + 3 iterations + error fix + PR).
    """
    client = _ensure_openai_client(api_timeout)
    instr = instructions_path.read_text(encoding="utf-8").strip()

    # Blueprints first (generate if missing)
    _generate_blueprints(client=client, model=model, api_timeout=api_timeout, repo=repo, instructions=instr)
    blueprints_summary = summarize_blueprints(repo)

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
            blueprints_summary=blueprints_summary,
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
        blueprints_summary=blueprints_summary,
    )
    _discover_new_files(
        client=client,
        model=model,
        api_timeout=api_timeout,
        repo=repo,
        instructions=instr,
        iteration=1,
        blueprints_summary=blueprints_summary,
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
        blueprints_summary=blueprints_summary,
    )
    _discover_new_files(
        client=client,
        model=model,
        api_timeout=api_timeout,
        repo=repo,
        instructions=instr,
        iteration=2,
        blueprints_summary=blueprints_summary,
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
        blueprints_summary=blueprints_summary,
    )

    # Final plan artifacts (after consistency)
    run_cmds, test_cmds = _generate_plan_artifacts(
        phase="final",
        client=client,
        model=model,
        api_timeout=api_timeout,
        repo=repo,
        instructions=instr,
        blueprints_summary=blueprints_summary,
    )

    # Now explicitly (re)visit deferred files (docs/setup/examples)
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
            blueprints_summary=blueprints_summary,
        )

    # Error‑fix loop
    commands = [*run_cmds, *test_cmds]
    if commands:
        log.info("Starting error‑fix loop with commands:\n%s", "\n".join(f"- {c}" for c in commands))
        for round_idx in range(1, max_error_rounds + 1):
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
                break

            log.warning("Errors detected (round %d). Sending logs to model.", round_idx)
            system_msg = {"role": "system", "content": _system_prompt(iteration=3, deferred_hint=False)}
            messages: List[Dict[str, Any]] = [
                system_msg,
                {
                    "role": "user",
                    "content": _error_fix_prompt(
                        combined_errors=combined,
                        last_commands=commands,
                        blueprints_summary=blueprints_summary,
                    ),
                },
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
    else:
        log.info("No run/test commands provided; skipping error‑fix loop.")

    _push_branch(repo, b3, remote)
    _maybe_create_pull_request(repo, branch=b3, remote=remote)
    log.info("Orchestration completed. Final branch: %s", b3)


# =============================================================================
# CLI
# =============================================================================

def _cli() -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="gpt-review-iterate")
    p.add_argument("instructions", help="Path to plain‑text project instructions.")
    p.add_argument("repo", help="Path to the Git repository OR a Git URL.")
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

    # Support Git URL input: clone into a temp directory
    repo_arg = args.repo
    repo_path = Path(repo_arg).expanduser()
    if not (repo_path.exists() and (repo_path / ".git").exists()):
        if _looks_like_git_url(repo_arg):
            try:
                repo_path = _clone_repo_to_temp(repo_arg)
            except Exception as exc:
                log.exception("Failed to clone repo %r: %s", repo_arg, exc)
                sys.exit(1)
        else:
            sys.exit(f"❌ Not a git repository or URL: {repo_arg}")

    repo = repo_path.resolve()

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
