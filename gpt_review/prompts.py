#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
===============================================================================
GPT‑Review ▸ Prompt Builders
===============================================================================

Purpose
-------
Provide strict, reusable prompt text for every stage of the orchestrated review:
  • Iteration system primer (per iteration)
  • Per‑file review/edit prompts (must return **full file** via submit_patch)
  • New‑files discovery (list spec as raw JSON array)
  • Iteration‑3 consistency pass (cross‑file alignment)
  • Documentation / setup / install files phase (deferred to iteration 3)
  • Error diagnosis and per‑file fix prompts (stages 8–9)

Design choices
--------------
• Output contract is crystal‑clear: **one tool call** to `submit_patch`
  with a **complete file body**. No partial diffs, no prose.
• For discovery steps (e.g., "list the new files"), we return a plain JSON
  array (not a tool call). The orchestrator will parse that and then drive
  per‑file creation via `submit_patch`.
• We keep prompts compact but unambiguous; the orchestrator adds repo/file
  content and enforces tool_choice=submit_patch at the API layer.

Logging
-------
Prompts are trace‑logged (DEBUG) with lengths for observability.

Dependencies
------------
No external deps. The orchestrator provides:
  - repo metadata (language census, file list),
  - file contents (normalized LF),
  - error logs for fix phases.
"""
from __future__ import annotations

import textwrap
from dataclasses import dataclass
from typing import Iterable, List, Optional, Sequence

from gpt_review import get_logger

log = get_logger(__name__)


# =============================================================================
# Tool schema accessor
# =============================================================================
def get_submit_patch_tool() -> dict:
    """
    Return the OpenAI tool/function schema for `submit_patch`.

    We reuse the same contract as the API driver. If importing the internal
    function fails (e.g., during refactors), we fall back to a local copy to
    avoid tight coupling.
    """
    try:
        # Prefer the canonical definition shipped with the API driver.
        from gpt_review.api_driver import _submit_patch_tool as _tool  # type: ignore
        tool = _tool()
        log.debug("Loaded submit_patch tool schema from api_driver.")
        return tool
    except Exception:
        log.debug("Falling back to local submit_patch tool schema.")
        return {
            "type": "function",
            "function": {
                "name": "submit_patch",
                "description": (
                    "Create, update, delete, rename or chmod exactly one file in the repository. "
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


# =============================================================================
# Shared text fragments
# =============================================================================
PATCH_OUTPUT_RULES = (
    "Output contract:\n"
    "• Respond ONLY by calling the tool `submit_patch`.\n"
    "• Provide a **complete file** in `body` (or `body_b64` for binary) — not a diff.\n"
    "• Use the **exact relative path** I provide in `file`.\n"
    "• Use `status=\"in_progress\"` until the very last patch of the whole review, "
    "  then `status=\"completed\"`.\n"
)

DEFER_RULES_EARLY = (
    "Deferrals (iterations 1–2): do NOT propose edits for **documentation (.md/.rst), "
    "installation scripts, packaging/setup files, or examples**. These are handled in iteration 3."
)

DOCS_PHASE_RULES = (
    "Docs/Install/Setup phase (iteration 3): you may now propose edits/creations for README, "
    "CHANGELOG, CONTRIBUTING, install/update scripts, CI workflows, packaging files, and examples."
)

CONSISTENCY_RULES = (
    "Consistency pass: ensure naming, imports, error handling, logging, and configuration are "
    "coherent across the entire codebase. Reduce duplication, align conventions, and eliminate "
    "drift introduced by earlier patches."
)

NEW_FILES_SPEC = textwrap.dedent(
    """
    Return a **raw JSON array** (no prose) describing new files to create **after**
    we finish the current iteration's per‑file edits. Each item must be:

    {
      "path": "relative/path.ext",
      "purpose": "short description of why this file should exist",
      "type": "code|test|doc|install|setup|example|data|binary",
      "priority": 1    // integer, 1 = highest priority
    }

    Do not include files that already exist. Use forward slashes, and ensure paths
    reflect the correct directory structure for the project.
    """
).strip()


# =============================================================================
# Iteration system prompts
# =============================================================================
@dataclass(frozen=True)
class IterationContext:
    """High‑level metadata used to prime the assistant at the start of each iteration."""

    iteration: int
    languages: Sequence[str]       # e.g. ["python:42", "javascript:3"]
    repo_summary: str              # short, human summary (tree/notes)
    goals: str                     # user instruction summary (concise)


def build_system_prompt(ctx: IterationContext) -> str:
    """
    Build a strict system prompt that frames the entire iteration.

    The orchestrator sends this as the `system` message for the turn sequence
    it drives during the iteration.
    """
    lang_line = ", ".join(ctx.languages) or "<unknown>"
    if ctx.iteration in (1, 2):
        extra = DEFER_RULES_EARLY
    else:
        extra = DOCS_PHASE_RULES + "\n" + CONSISTENCY_RULES

    prompt = textwrap.dedent(
        f"""
        You are GPT‑Review. You will audit and fix files **one at a time**.
        You will return **full files** only via the `submit_patch` tool; no diffs/prose.

        Review plan for iteration {ctx.iteration}:
          1) Understand the project, folder structure, and goals.
          2) For each file I send, produce the corrected full file body.
          3) After finishing all files this iteration, prepare a list of **new files** to create.
          4) We will repeat across three iterations; iteration 3 includes docs/install/setup/examples.

        Project snapshot:
          • Languages: {lang_line}
          • Summary  : {ctx.repo_summary}

        Goals:
        {ctx.goals}

        {extra}

        Strictness:
        - Keep changes minimal and targeted per file.
        - Maintain behavior; modernize APIs where appropriate.
        - Respect exact relative paths I provide.
        - {PATCH_OUTPUT_RULES}
        """
    ).strip()

    log.debug("System prompt (iteration %s) built (%d chars).", ctx.iteration, len(prompt))
    return prompt


# =============================================================================
# Per‑file prompts
# =============================================================================
def build_file_review_prompt(
    *,
    iteration: int,
    rel_path: str,
    file_text: str,
    file_notes: Optional[str] = None,
) -> str:
    """
    Construct the per‑file **user** message that carries the current file's
    content and instructions to emit a **full replacement file** via
    `submit_patch`.

    Parameters
    ----------
    iteration : int
        Current iteration index (1..3).
    rel_path : str
        POSIX relative path of the file under review.
    file_text : str
        The current full file content (LF normalized).
    file_notes : Optional[str]
        Optional hints (e.g., known issues, failing tests related to this file).
    """
    notes = f"\nNotes:\n{file_notes.strip()}\n" if file_notes else ""
    defer = ("\n" + DEFER_RULES_EARLY) if iteration in (1, 2) else ""
    user = textwrap.dedent(
        f"""
        Review and fix **this file**. Return a complete file via `submit_patch`.

        Path:
        {rel_path}

        Current content:
        ```text
        {file_text}
        ```

        {notes}
        Requirements:
        - Keep behavior unless clearly buggy; modernize APIs if needed.
        - Ensure imports, logging, and style align with the project.
        - Add/adjust docstrings and type hints when beneficial.
        - If the file should be deleted, call `submit_patch` with op="delete".
        - If the file should be renamed, call `submit_patch` with op="rename" and set `target`.

        {PATCH_OUTPUT_RULES}{defer}
        """
    ).strip()
    log.debug("File prompt built for %s (%d chars).", rel_path, len(user))
    return user


# =============================================================================
# New files discovery (end of iteration 1 and 2; also after 3 before docs)
# =============================================================================
def build_new_files_discovery_prompt(
    *,
    iteration: int,
    processed_paths: Sequence[str],
    repo_overview: str,
) -> str:
    """
    Ask the assistant to propose **new files** to be created next.

    The response is expected to be a **raw JSON array** (no prose), following
    the NEW_FILES_SPEC contract. The orchestrator will then create each item
    with a separate `submit_patch` call.
    """
    processed = "\n".join(processed_paths) or "<none>"
    phase = "early (no docs/install/setup/examples yet)" if iteration in (1, 2) else "full (docs allowed)"
    msg = textwrap.dedent(
        f"""
        We have finished per‑file edits for iteration {iteration} ({phase}).

        Repository overview:
        {repo_overview}

        Files reviewed in this iteration:
        {processed}

        Now propose **new files to create** that are necessary to fulfill the goals.
        {NEW_FILES_SPEC}
        """
    ).strip()
    log.debug("New‑files discovery prompt built (%d chars).", len(msg))
    return msg


# =============================================================================
# Iteration‑3 consistency pass
# =============================================================================
def build_consistency_pass_prompt(
    *,
    repo_overview: str,
    invariant_notes: Optional[str] = None,
) -> str:
    """
    In iteration 3, request cross‑project alignment. The assistant should still
    respond with **one file at a time** (enforced by the orchestrator), but this
    primer sets the expectations.
    """
    inv = f"\nProject invariants:\n{invariant_notes.strip()}\n" if invariant_notes else ""
    msg = textwrap.dedent(
        f"""
        Iteration 3 consistency pass.

        Overview:
        {repo_overview}
        {inv}

        {CONSISTENCY_RULES}

        For each file I send next, return a **complete file** via `submit_patch` —
        no diffs, no prose. If a filename must change for consistency, use op="rename".
        """
    ).strip()
    log.debug("Consistency pass prompt built (%d chars).", len(msg))
    return msg


# =============================================================================
# Docs / Install / Setup phase (iteration 3)
# =============================================================================
def build_docs_phase_prompt(
    *,
    repo_overview: str,
    guidance: Optional[str] = None,
) -> str:
    """
    Enable the deferred classes (docs, install scripts, packaging/setup, examples).
    The orchestrator will drive concrete file edits/creations subsequently.
    """
    guide = f"\nAdditional guidance:\n{guidance.strip()}\n" if guidance else ""
    msg = textwrap.dedent(
        f"""
        Iteration 3 documentation & setup phase begins.

        Overview:
        {repo_overview}
        {guide}

        {DOCS_PHASE_RULES}

        For each doc/setup/install/example file I send, return a **complete file**
        via `submit_patch`. Use op="create" for new files and ensure exact paths.
        """
    ).strip()
    log.debug("Docs/setup phase prompt built (%d chars).", len(msg))
    return msg


# =============================================================================
# Error handling phase (items #8 and #9)
# =============================================================================
def build_error_diagnosis_prompt(
    *,
    run_command: str,
    error_log_tail: str,
) -> str:
    """
    Ask the assistant to identify impacted files based on a failing run.

    Expected response: a **raw JSON array** of objects:
      { "file": "relative/path", "reason": "why this file is implicated", "order": 1 }

    The orchestrator will then ask for fixes **one file at a time** via submit_patch.
    """
    msg = textwrap.dedent(
        f"""
        The following command failed:

        $ {run_command}

        Error log (tail):
        ```text
        {error_log_tail}
        ```

        Identify which files must change to fix these errors and return a **raw JSON array**
        (no prose) with items of shape:
        {{"file": "relative/path", "reason": "short explanation", "order": 1}}

        Only include files you are confident need changes. Do not include generated artefacts.
        """
    ).strip()
    log.debug("Error diagnosis prompt built (%d chars).", len(msg))
    return msg


def build_error_fix_prompt_for_file(
    *,
    rel_path: str,
    current_text: str,
    error_excerpt: Optional[str] = None,
    diagnosis_reason: Optional[str] = None,
) -> str:
    """
    Request a **complete file** fix for a single target file implicated by
    the failing run.

    The assistant must call `submit_patch` and provide full file contents in `body`.
    """
    diag = f"\nDiagnosis: {diagnosis_reason.strip()}\n" if diagnosis_reason else ""
    err = (
        f"\nError excerpts:\n```text\n{error_excerpt.strip()}\n```\n"
        if error_excerpt else ""
    )
    user = textwrap.dedent(
        f"""
        Apply a fix to this file and return the **entire file** via `submit_patch`.

        File:
        {rel_path}

        Current content:
        ```text
        {current_text}
        ```

        {diag}{err}
        Requirements:
        - Provide a complete replacement file (not a diff).
        - Maintain compatibility with the project structure.
        - Include necessary imports and update tests if this is a test file.
        - If the file must be removed, use op="delete"; if renamed, use op="rename".

        {PATCH_OUTPUT_RULES}
        """
    ).strip()
    log.debug("Error‑fix file prompt built for %s (%d chars).", rel_path, len(user))
    return user


# =============================================================================
# Review spec (generated after iteration 3)
# =============================================================================
def build_review_spec_prompt(
    *,
    goals_from_user: str,
    observed_behavior: str,
    run_instructions: str,
    success_criteria: str,
    file_name: str = "SOFTWARE_REVIEW_SPEC.md",
) -> str:
    """
    Ask the assistant to generate a **self‑contained review guide** that becomes
    the canonical reference for subsequent error‑fix loops and future runs.

    The orchestrator will request a `submit_patch` create/update for `file_name`.
    """
    msg = textwrap.dedent(
        f"""
        Create or update `{file_name}` documenting the software review objectives and
        expected outcomes. Return a **complete Markdown file** via `submit_patch`.

        Include:
        - Project overview
        - What the software does (in your words)
        - How to run it (commands)
        - What success looks like (observable outputs / passing tests)
        - Supported tech stack(s) and versions
        - Known constraints and non‑goals
        - Checklist for future reviews

        Inputs:
        • Goals (from user): {goals_from_user}
        • Observed behavior: {observed_behavior}
        • Run instructions   : {run_instructions}
        • Success criteria   : {success_criteria}

        {PATCH_OUTPUT_RULES}
        """
    ).strip()
    log.debug("Review spec prompt built for %s (%d chars).", file_name, len(msg))
    return msg
