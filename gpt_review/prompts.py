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

Blueprint awareness
-------------------
When available, the orchestrator can pass an abridged summary of the four
blueprint documents (Whitepaper, Build Guide, SDS, Project Instructions) via
the optional `blueprints_summary` keyword parameter. This content is spliced
into prompts to keep the review aligned with the project’s authoritative
requirements. If not provided, prompts behave as before.

Design choices
--------------
• Output contract is crystal‑clear: **one tool call** to `submit_patch`
  with a **complete file body**. No partial diffs, no prose.
• For discovery steps (e.g., “list the new files”), we ask for a raw **JSON
  array** (not a tool call). The orchestrator will parse that and then drive
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
  - error logs for fix phases,
  - (optionally) blueprints summary text.
"""
from __future__ import annotations

import textwrap
from dataclasses import dataclass
from typing import Optional, Sequence

from gpt_review import get_logger

log = get_logger(__name__)


# =============================================================================
# Tool schema accessor
# =============================================================================
def get_submit_patch_tool() -> dict:
    """
    Return the OpenAI tool/function schema for `submit_patch`.

    We reuse the canonical definition shipped with the OpenAI client wrapper.
    If importing that function fails (e.g., during refactors), we fall back to
    a local copy to avoid tight coupling.
    """
    try:
        # Prefer the canonical definition shipped with the API client.
        from gpt_review.api_client import _submit_patch_tool as _tool  # type: ignore
        tool = _tool()
        log.debug("Loaded submit_patch tool schema from api_client.")
        return tool
    except Exception:
        log.debug("Falling back to local submit_patch tool schema.")
        return {
            "type": "function",
            "function": {
                "name": "submit_patch",
                "description": (
                    "Create, update, delete, rename or chmod exactly one file in the repository. "
                    "For create/update you MUST return a **COMPLETE FILE** in `body` (or `body_b64` for binary) — never a diff. "
                    "Return one patch at a time and set status to 'in_progress' until the last patch, "
                    "then 'completed'. Use **repo‑relative POSIX paths** in `file` (and `target` for rename)."
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
    "• One patch == one file; do not bundle multiple edits.\n"
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


def _bp_block(blueprints_summary: Optional[str]) -> str:
    """
    Small helper: materialize the blueprint documents block if provided.
    """
    if not blueprints_summary:
        return ""
    return f"\nBlueprint documents (abridged):\n{blueprints_summary}\n"


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


def build_system_prompt(
    ctx: IterationContext,
    *,
    blueprints_summary: Optional[str] = None,
) -> str:
    """
    Build a strict system prompt that frames the entire iteration.

    The orchestrator sends this as the `system` message for the turn sequence
    it drives during the iteration.

    Parameters
    ----------
    ctx : IterationContext
        Iteration metadata.
    blueprints_summary : Optional[str]
        Optional abridged text of the four blueprint docs to ground the review.
    """
    lang_line = ", ".join(ctx.languages) or "<unknown>"
    if ctx.iteration in (1, 2):
        extra = DEFER_RULES_EARLY
    else:
        extra = DOCS_PHASE_RULES + "\n" + CONSISTENCY_RULES

    bp = _bp_block(blueprints_summary)

    prompt = textwrap.dedent(
        f"""
        You are GPT‑Review. You will audit and fix files **one at a time**.
        You will return **full files** only via the `submit_patch` tool; no diffs/prose.
        {bp}
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
        {PATCH_OUTPUT_RULES}
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
    blueprints_summary: Optional[str] = None,
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
    blueprints_summary : Optional[str]
        Abridged blueprint docs to ground the per‑file decision.
    """
    notes = f"\nNotes:\n{file_notes.strip()}\n" if file_notes else ""
    defer = ("\n" + DEFER_RULES_EARLY) if iteration in (1, 2) else ""
    bp = _bp_block(blueprints_summary)

    user = textwrap.dedent(
        f"""
        Review and fix **this file**. Return a complete file via `submit_patch`.
        {bp}
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
    blueprints_summary: Optional[str] = None,
) -> str:
    """
    Ask the assistant to propose **new files** to be created next.

    The response is expected to be a **raw JSON array** (no prose), following
    the NEW_FILES_SPEC contract. The orchestrator will then create each item
    with a separate `submit_patch` call.
    """
    processed = "\n".join(processed_paths) or "<none>"
    phase = "early (no docs/install/setup/examples yet)" if iteration in (1, 2) else "full (docs allowed)"
    bp = _bp_block(blueprints_summary)

    msg = textwrap.dedent(
        f"""
        We have finished per‑file edits for iteration {iteration} ({phase}).
        {bp}
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
    blueprints_summary: Optional[str] = None,
) -> str:
    """
    In iteration 3, request cross‑project alignment. The assistant should still
    respond with **one file at a time** (enforced by the orchestrator), but this
    primer sets the expectations.
    """
    inv = f"\nProject invariants:\n{invariant_notes.strip()}\n" if invariant_notes else ""
    bp = _bp_block(blueprints_summary)

    msg = textwrap.dedent(
        f"""
        Iteration 3 consistency pass.
        {bp}
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
    blueprints_summary: Optional[str] = None,
) -> str:
    """
    Enable the deferred classes (docs, install scripts, packaging/setup, examples).
    The orchestrator will drive concrete file edits/creations subsequently.
    """
    guide = f"\nAdditional guidance:\n{guidance.strip()}\n" if guidance else ""
    bp = _bp_block(blueprints_summary)

    msg = textwrap.dedent(
        f"""
        Iteration 3 documentation & setup phase begins.
        {bp}
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
    blueprints_summary: Optional[str] = None,
) -> str:
    """
    Ask the assistant to identify impacted files based on a failing run.

    Expected response: a **raw JSON array** of objects:
      { "path": "relative/path", "reason": "why this file is implicated", "order": 1 }

    The orchestrator will then ask for fixes **one file at a time** via submit_patch.
    """
    bp = _bp_block(blueprints_summary)

    msg = textwrap.dedent(
        f"""
        The following command failed:

        $ {run_command}
        {bp}
        Error log (tail):
        ```text
        {error_log_tail}
        ```

        Identify which files must change to fix these errors and return a **raw JSON array**
        (no prose) with items of shape:
        {{"path": "relative/path", "reason": "short explanation", "order": 1}}

        Only include files you are confident need changes. Do not include generated artifacts.
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
    blueprints_summary: Optional[str] = None,
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
    bp = _bp_block(blueprints_summary)

    user = textwrap.dedent(
        f"""
        Apply a fix to this file and return the **entire file** via `submit_patch`.
        {bp}
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
    blueprints_summary: Optional[str] = None,
) -> str:
    """
    Ask the assistant to generate a **self‑contained review guide** that becomes
    the canonical reference for subsequent error‑fix loops and future runs.

    The orchestrator will request a `submit_patch` create/update for `file_name`.
    """
    bp = _bp_block(blueprints_summary)

    msg = textwrap.dedent(
        f"""
        Create or update `{file_name}` documenting the software review objectives and
        expected outcomes. Return a **complete Markdown file** via `submit_patch`.
        {bp}
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


# =============================================================================
# Compatibility wrappers (used by `gpt_review.workflow`)
# =============================================================================
def build_overview_prompt(
    *,
    instructions: str,
    manifest: str,
    blueprints_summary: Optional[str] = None,
) -> str:
    """
    One‑time “understand first” overview message used at the **start of iteration 1**.
    This is a **plain user message** (no tool call) that frames the whole run.
    """
    bp = _bp_block(blueprints_summary)

    msg = textwrap.dedent(
        f"""
        You are GPT‑Review running a three‑iteration software review.
        {bp}
        Objectives:
        1) Understand the repository structure and the user's instructions.
        2) Review **each file one by one**, returning **complete files** via `submit_patch`.
        3) After finishing all files in an iteration, propose any **new files** as a JSON list.
        4) Only in iteration 3, also review **docs/setup/examples** and ensure cross‑project consistency.
        5) During the error‑fix phase, use the logs to propose **complete file** fixes.

        Instructions from user:
        {instructions}

        Repository manifest (non‑binary, repo‑relative paths):
        {manifest}

        Ground rules:
        - Preserve behavior unless fixing clear defects.
        - Keep changes minimal and self‑contained.
        - All paths must be **repo‑relative POSIX**.
        - {PATCH_OUTPUT_RULES}
        """
    ).strip()
    log.debug("Overview prompt built (%d chars).", len(msg))
    return msg


def build_file_prompt(
    *,
    instructions: str,
    manifest: str,
    iteration: int,
    rel_path: str,
    content: str,
    blueprints_summary: Optional[str] = None,
) -> str:
    """
    Per‑file prompt used by `workflow.ReviewWorkflow` (iterations 1–3).
    Requires a `submit_patch` call that returns a **complete file**.
    """
    defer = ("\n" + DEFER_RULES_EARLY) if iteration in (1, 2) else ("\n" + CONSISTENCY_RULES)
    bp = _bp_block(blueprints_summary)

    msg = textwrap.dedent(
        f"""
        Review iteration {iteration} — file: `{rel_path}`.
        {bp}
        Project instructions:
        {instructions}

        Repository manifest:
        {manifest}

        The file below is the **current ground truth**. Return a COMPLETE file
        if changes are required; otherwise use op="update" with an identical body
        or op="delete" only if the file must be removed.

        Current content:
        ```text
        {content}
        ```

        Requirements:
        - Keep behavior unless clearly buggy; modernize APIs for Python 3.12 if applicable.
        - Ensure imports, logging, typing, and style align with the project.
        - Use the exact repo‑relative path I gave.
        - If the file should be renamed: `submit_patch` with op="rename" and set `target`.
        {defer}

        {PATCH_OUTPUT_RULES}
        """
    ).strip()
    log.debug("Workflow file prompt built for %s (%d chars).", rel_path, len(msg))
    return msg


def build_new_files_prompt(
    *,
    instructions: str,
    manifest: str,
    iteration: int,
    blueprints_summary: Optional[str] = None,
) -> str:
    """
    Ask for a **strict JSON array** of new files after an iteration.
    The `workflow` will then request each file body via a separate `submit_patch` call.
    """
    phase = "early (no docs/setup/examples)" if iteration in (1, 2) else "full (docs allowed)"
    bp = _bp_block(blueprints_summary)

    msg = textwrap.dedent(
        f"""
        We have finished per‑file edits for iteration {iteration} — {phase}.
        {bp}
        Based on the repository state and the instructions below, return a **strict JSON array**
        (no prose, no code fences) where each item is:

        {{ "path": "relative/posix/path.ext", "reason": "why this file is needed now" }}

        Rules:
        - Paths MUST be **repo‑relative POSIX**.
        - For iterations 1–2, **exclude** docs/setup/examples; include only source/tests/config.
        - Do not include files that already exist.

        Instructions:
        {instructions}

        Repository manifest:
        {manifest}
        """
    ).strip()
    log.debug("Workflow new-files prompt built (%d chars).", len(msg))
    return msg


def build_consistency_prompt(
    *,
    instructions: str,
    manifest: str,
    rel_path: str,
    content: str,
    blueprints_summary: Optional[str] = None,
) -> str:
    """
    Iteration‑3 consistency prompt for a single file (full replacement required).
    """
    bp = _bp_block(blueprints_summary)

    msg = textwrap.dedent(
        f"""
        Iteration 3 — **consistency pass** for file `{rel_path}`.
        {bp}
        Instructions:
        {instructions}

        Repository manifest:
        {manifest}

        The goal is cross‑file consistency (naming, imports, error handling, logging,
        configuration). If you change conventions here, ensure they align with the
        rest of the project. Return a **complete file**.

        Current content:
        ```text
        {content}
        ```

        {CONSISTENCY_RULES}

        {PATCH_OUTPUT_RULES}
        """
    ).strip()
    log.debug("Workflow consistency prompt built for %s (%d chars).", rel_path, len(msg))
    return msg


def build_error_fix_list_prompt(
    *,
    instructions: str,
    manifest: str,
    error_log_tail: str,
    blueprints_summary: Optional[str] = None,
) -> str:
    """
    Ask for a **strict JSON array** of files to change given error logs.
    Each item: { "path": "relative/path", "reason": "short explanation" }.
    """
    bp = _bp_block(blueprints_summary)

    msg = textwrap.dedent(
        f"""
        The following run produced errors. Analyze and list only the files that must change.
        {bp}
        Error log (tail):
        ```text
        {error_log_tail}
        ```

        Return a **strict JSON array** (no prose) with items of shape:
        {{ "path": "relative/posix/path", "reason": "short explanation" }}

        Rules:
        - Include only files you are confident must change.
        - Exclude generated artifacts and build outputs.
        - Paths MUST be repo‑relative POSIX.

        Context:
        • Instructions: {instructions}
        • Repository manifest:
        {manifest}
        """
    ).strip()
    log.debug("Workflow error-fix list prompt built (%d chars).", len(msg))
    return msg


def build_error_fix_file_prompt(
    *,
    instructions: str,
    manifest: str,
    rel_path: str,
    reason: str,
    blueprints_summary: Optional[str] = None,
) -> str:
    """
    Ask for a **complete file** (create/update) for a single target path.
    Used both for new file creation and for error‑driven fixes to existing files.
    """
    bp = _bp_block(blueprints_summary)

    msg = textwrap.dedent(
        f"""
        Provide a **complete file** for this path via `submit_patch`.
        {bp}
        Path:
        {rel_path}

        Reason:
        {reason}

        Constraints:
        - Use the exact repo‑relative POSIX path above.
        - Include all required imports and code; do not return a diff.
        - Maintain project conventions (logging, typing, structure).
        - If this replaces an existing file, ensure backward compatibility unless fixing a defect.

        Context:
        • Instructions: {instructions}
        • Repository manifest:
        {manifest}

        {PATCH_OUTPUT_RULES}
        """
    ).strip()
    log.debug("Workflow error-fix file prompt built for %s (%d chars).", rel_path, len(msg))
    return msg


def build_final_instructions_prompt(
    *,
    instructions: str,
    manifest: str,
    blueprints_summary: Optional[str] = None,
) -> str:
    """
    Ask the assistant to synthesize an authoritative **REVIEW_INSTRUCTIONS.md**
    as a **strict JSON array** with a single object:
      [{ "path": "REVIEW_INSTRUCTIONS.md", "body": "<full markdown>" }]
    """
    bp = _bp_block(blueprints_summary)

    msg = textwrap.dedent(
        f"""
        Create a **single Markdown file** named `REVIEW_INSTRUCTIONS.md` that explains:
        - What this software does (your own words, based on the code)
        - How to run it (commands)
        - What success looks like (observable outputs)
        - Supported tech stack(s) and versions
        - Known constraints / non‑goals
        - Checklist for future review runs
        {bp}
        Return a **strict JSON array** (no prose) with exactly one item:
        [{{ "path": "REVIEW_INSTRUCTIONS.md", "body": "<full markdown content>" }}]

        Context:
        • Instructions: {instructions}
        • Repository manifest:
        {manifest}
        """
    ).strip()
    log.debug("Workflow final-instructions prompt built (%d chars).", len(msg))
    return msg


# =============================================================================
# __all__
# =============================================================================
__all__ = [
    # tool schema
    "get_submit_patch_tool",
    # system / iteration
    "IterationContext",
    "build_system_prompt",
    # per-file
    "build_file_review_prompt",
    # discovery
    "build_new_files_discovery_prompt",
    # consistency/docs
    "build_consistency_pass_prompt",
    "build_docs_phase_prompt",
    # error diagnostics / fixes
    "build_error_diagnosis_prompt",
    "build_error_fix_prompt_for_file",
    # review spec
    "build_review_spec_prompt",
    # workflow compatibility wrappers
    "build_overview_prompt",
    "build_file_prompt",
    "build_new_files_prompt",
    "build_consistency_prompt",
    "build_error_fix_list_prompt",
    "build_error_fix_file_prompt",
    "build_final_instructions_prompt",
]
