#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
===============================================================================
GPT‑Review ▸ Multi‑Iteration Orchestrator (API mode, compatibility path)
===============================================================================

Responsibilities
----------------
• Create a fresh branch per run (iteration1, iteration2, iteration3).
• Read the instructions + repository structure; build a manifest.
• Run three iterations:
  1) Review/replace *code+tests* one‑by‑one (return **complete files** only).
     Then ask for a list of *new files* (JSON list) and create them one‑by‑one.
  2) Repeat the process; again ask for additional new files.
  3) Review *all non‑binary files for consistency* (docs/setup/examples
     included now), still one file at a time and still **complete files only**.

• After iteration 3:
  - Generate an authoritative **review instructions** file that states how to
    run the software and what success looks like (full file via tool call).
  - Enter an error‑fix loop: run the provided command, collect logs, ask for
    affected files (JSON list) & full replacements; iterate until success.

• Push branch at the end if requested.

Design choices
--------------
• Full‑file semantics: the assistant must always return an *entire* file as the
  replacement (never a diff). We enforce this with a tool schema and schema
  validation before writing through `apply_patch`.
• `apply_patch.py` performs **path‑scoped staging and the actual commit** for each
  change. This module does not re‑commit the same paths (avoids “nothing to commit”
  errors and respects the single‑source-of-truth safety layer).
• Deferral: docs/installation/setup/example files are intentionally skipped
  in iterations 1 & 2 and processed in iteration 3 to avoid churn.
• Explicit paths: every action uses repo‑root‑relative POSIX paths to prevent
  accidental nesting/misplacement.

This module uses:
  - `gpt_review.api_client.OpenAIClient` for tool‑forced calls & strict arrays
  - `gpt_review.file_scanner` facade (wrapping the robust RepoScanner)
  - `gpt_review.prompts` for strict, concise user prompts
  - `apply_patch.apply_patch` to perform **path‑scoped**, validated writes
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import textwrap
from dataclasses import dataclass
from pathlib import Path
from typing import Any, List, Optional, Sequence, Tuple

from gpt_review import get_logger
from patch_validator import validate_patch

log = get_logger(__name__)

# -----------------------------------------------------------------------------
# Defaults & environment toggles
# -----------------------------------------------------------------------------
DEFAULT_MODEL = os.getenv("GPT_REVIEW_MODEL", "gpt-5-pro")
DEFAULT_API_TIMEOUT = int(os.getenv("GPT_REVIEW_API_TIMEOUT", "120"))
DEFAULT_ITERATIONS = 3
DEFAULT_BRANCH_PREFIX = os.getenv("GPT_REVIEW_BRANCH_PREFIX", "iteration")
DEFAULT_REMOTE = os.getenv("GPT_REVIEW_REMOTE", "origin")
LOG_TAIL_CHARS = int(os.getenv("GPT_REVIEW_LOG_TAIL_CHARS", "20000"))

# Paths/dirs we always ignore when scanning the repo (kept for CLI parity only)
DEFAULT_IGNORES = (
    ".git",
    ".hg",
    ".svn",
    ".idea",
    ".vscode",
    "__pycache__",
    "node_modules",
    "venv",
    ".venv",
    "env",
    ".mypy_cache",
    ".ruff_cache",
    "dist",
    "build",
    ".tox",
    ".pytest_cache",
)


# =============================================================================
# Data models
# =============================================================================
@dataclass(frozen=True)
class OrchestratorConfig:
    instructions_path: Path
    repo: Path
    model: str = DEFAULT_MODEL
    api_timeout: int = DEFAULT_API_TIMEOUT
    iterations: int = DEFAULT_ITERATIONS
    branch_prefix: str = DEFAULT_BRANCH_PREFIX
    remote: str = DEFAULT_REMOTE
    push_at_end: bool = True
    run_cmd: Optional[str] = None  # e.g. "pytest -q"

    # scanning / categorisation settings (accepted for backwards compatibility)
    ignores: Sequence[str] = DEFAULT_IGNORES


@dataclass
class IterationStats:
    created: int = 0
    updated: int = 0
    deleted: int = 0
    chmods: int = 0
    new_files_listed: int = 0
    new_files_created: int = 0
    commits: int = 0


# =============================================================================
# Git helpers
# =============================================================================
def _git(repo: Path, *args: str, capture: bool = False, check: bool = True) -> str:
    """
    Run a git command in *repo*. Raises on error when check=True.
    """
    res = subprocess.run(
        ["git", "-C", str(repo), *args],
        text=True,
        capture_output=capture,
    )
    if check and res.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {res.stderr.strip()}")
    return (res.stdout or "") if capture else ""


def _current_commit(repo: Path) -> str:
    """
    Return current HEAD (short) or "<no-commits-yet>".
    """
    try:
        out = _git(repo, "rev-parse", "--short", "HEAD", capture=True, check=False).strip()
        return out or "<no-commits-yet>"
    except Exception:
        return "<no-commits-yet>"


def _ensure_branch(repo: Path, prefix: str) -> str:
    """
    Create or select the next 'iterationN' branch name in the local repo.
    """
    existing = _git(repo, "branch", "--list", capture=True).splitlines()
    pat = re.compile(rf"^\*?\s*{re.escape(prefix)}(\d+)$")
    nums = [int(m.group(1)) for ln in existing if (m := pat.search(ln))]

    next_num = max(nums) + 1 if nums else 1
    name = f"{prefix}{next_num}"

    log.info("Creating/checkout branch: %s", name)
    _git(repo, "checkout", "-B", name, check=True)
    return name


def _push(repo: Path, remote: str, branch: str) -> None:
    """
    Push the branch to the remote with upstream.
    """
    log.info("Pushing branch '%s' to remote '%s' …", branch, remote)
    _git(repo, "push", "-u", remote, branch, check=True)


# =============================================================================
# Command execution
# =============================================================================
def _run_cmd(cmd: str, repo: Path, timeout: int) -> Tuple[bool, str, int]:
    """
    Execute *cmd* in *repo* and return (ok, combined_output, exit_code).
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


def _tail(text: str, n: int = LOG_TAIL_CHARS) -> str:
    return text if len(text) <= n else text[-n:]


# =============================================================================
# Orchestrator
# =============================================================================
class ReviewWorkflow:
    """
    High‑level multi‑iteration review loop (API‑driven).

    Usage:
        cfg = OrchestratorConfig(instructions_path=Path(...), repo=Path(...))
        ReviewWorkflow(cfg).run()
    """

    def __init__(self, cfg: OrchestratorConfig):
        self.cfg = cfg
        self.repo = cfg.repo
        self.instructions = self._read_instructions(cfg.instructions_path)

        # Late‑bound components
        self._client = None  # type: ignore[assignment]  # OpenAIClient
        self._scan = None    # type: ignore[assignment]  # RepoScan manifest

        # Bookkeeping
        self.branch_name: Optional[str] = None

    # ────────────────────────────────────────────────────────────────────── #
    # Setup helpers
    # ────────────────────────────────────────────────────────────────────── #
    def _read_instructions(self, p: Path) -> str:
        try:
            txt = p.read_text(encoding="utf-8").strip()
            if not txt:
                raise ValueError("Instructions file is empty.")
            return txt
        except Exception as exc:
            raise SystemExit(f"Failed to read instructions file: {exc}") from exc

    def _ensure_repo(self) -> None:
        if not (self.repo / ".git").exists():
            raise SystemExit(f"Not a git repository: {self.repo}")

    def _init_clients(self) -> None:
        """
        Connect the OpenAI client wrapper (tool‑forced).  Keep imports local
        so other entry points don't pay this cost unless needed.
        """
        try:
            from gpt_review.api_client import OpenAIClient
        except Exception as exc:
            log.exception("Failed to import API client: %s", exc)
            raise SystemExit(1) from exc

        self._client = OpenAIClient(
            model=self.cfg.model,
            timeout_s=self.cfg.api_timeout,
        )

    def _scan_repo(self) -> None:
        """
        Build a manifest of files and their categories (non‑binary split).
        """
        try:
            from gpt_review.file_scanner import scan_repository
        except Exception as exc:
            log.exception("Failed to import file scanner: %s", exc)
            raise SystemExit(1) from exc

        self._scan = scan_repository(self.repo, ignores=self.cfg.ignores)
        log.info("Scanned repo: %s files (code+tests=%s, deferred=%s)",
                 len(self._scan.all_files),
                 len(self._scan.code_and_config),
                 len(self._scan.docs_and_extras))

    def _branch(self) -> None:
        self.branch_name = _ensure_branch(self.repo, self.cfg.branch_prefix)
        log.info("Working on branch '%s' (HEAD=%s)", self.branch_name, _current_commit(self.repo))

    # ────────────────────────────────────────────────────────────────────── #
    # Prompt helpers (wired to gpt_review.prompts)
    # ────────────────────────────────────────────────────────────────────── #
    def _overview_note_text(self) -> str:
        """
        One‑time primer before iteration 1: align on goals, repo view, and
        the **full‑file + deferral** contract. Stored in the conversation as
        a user note; the actual edit prompts rely on tool calls.
        """
        manifest = self._scan.manifest_text()
        rules = textwrap.dedent(
            """
            Rules for this review:
            1) We will fix the software one file at a time. For each file you MUST return a
               **complete replacement file** via the `submit_patch` tool (never a diff).
            2) Keep changes minimal and behavior‑preserving unless a bug is evident.
            3) Iterations 1–2: focus on code & tests only. Defer docs/install/setup/examples.
            4) Iteration 3: include all non‑binary files, ensure cross‑file consistency,
               and only then handle docs/install/setup/examples.
            5) Use exact repo‑relative POSIX paths; do not create nested directories
               unless explicitly asked in a dedicated step.
            """
        ).strip()
        return f"Objectives:\n{self.instructions}\n\nRepository manifest:\n{manifest}\n\n{rules}"

    def _file_prompt(self, rel_path: str, content: str, iteration: int) -> str:
        from gpt_review.prompts import build_file_review_prompt

        return build_file_review_prompt(
            iteration=iteration,
            rel_path=rel_path,
            file_text=content,
            file_notes=None,
        )

    def _consistency_prompt(self, rel_path: str, content: str) -> str:
        from gpt_review.prompts import build_file_review_prompt

        # Reuse the same strict full‑file prompt, but iteration=3 and with notes.
        return build_file_review_prompt(
            iteration=3,
            rel_path=rel_path,
            file_text=content,
            file_notes=(
                "Consistency pass context: align naming, imports, error handling, logging, "
                "and configuration with the rest of the codebase. Prefer minimal, coherent changes."
            ),
        )

    def _new_files_list_prompt(self, iteration: int, processed: Sequence[str]) -> str:
        from gpt_review.prompts import build_new_files_discovery_prompt

        return build_new_files_discovery_prompt(
            iteration=iteration,
            processed_paths=list(processed),
            repo_overview=self._scan.manifest_text(),
        )

    def _error_fix_list_prompt(self, log_tail: str) -> str:
        from gpt_review.prompts import build_error_diagnosis_prompt

        run_cmd = self.cfg.run_cmd or "<no-run-command-provided>"
        return build_error_diagnosis_prompt(
            run_command=run_cmd,
            error_log_tail=log_tail,
        )

    def _error_fix_file_prompt(self, file_path: str, reason: str) -> str:
        from gpt_review.prompts import build_error_fix_prompt_for_file

        # Current file text (empty string for new files) to anchor the full‑file replacement.
        p = (self.repo / file_path)
        current_text = p.read_text(encoding="utf-8", errors="replace") if p.exists() else ""
        return build_error_fix_prompt_for_file(
            rel_path=file_path,
            current_text=current_text,
            error_excerpt=None,
            diagnosis_reason=reason,
        )

    def _final_instructions_prompt(self) -> str:
        from gpt_review.prompts import build_review_spec_prompt

        # Generate an authoritative run/spec guide after iteration 3.
        return build_review_spec_prompt(
            goals_from_user=self.instructions,
            observed_behavior="(to date) See commit history from automated review.",
            run_instructions=self.cfg.run_cmd or "(not specified by user)",
            success_criteria="Software runs without errors as per this file; tests (if any) pass.",
            file_name="REVIEW_INSTRUCTIONS.md",
        )

    # ────────────────────────────────────────────────────────────────────── #
    # OpenAI interactions
    # ────────────────────────────────────────────────────────────────────── #
    def _require_api(self) -> None:
        if self._client is None:
            self._init_clients()

    def _ask_json_array(self, prompt: str) -> List[dict]:
        """
        Ask the model for a strict JSON array (no prose). Defensive parsing.
        """
        self._require_api()
        from gpt_review.api_client import strict_json_array

        arr = strict_json_array(self._client, prompt)
        log.info("Received JSON list with %d items.", len(arr))
        return arr

    def _ask_fullfile_patch(self, prompt: str, rel_path: str, kind: str) -> dict:
        """
        Request a full‑file patch through the structured tool call.
        """
        self._require_api()
        from gpt_review.api_client import submit_patch_call

        patch = submit_patch_call(self._client, prompt, rel_path=rel_path, expected_kind=kind)
        # Validate with our JSON‑Schema (enforces shape/enum/patterns).
        validate_patch(json.dumps(patch, ensure_ascii=False))
        return patch

    # ────────────────────────────────────────────────────────────────────── #
    # Core operations
    # ────────────────────────────────────────────────────────────────────── #
    def _apply_and_commit(self, patch: dict, _commit_msg: str) -> None:
        """
        Apply a validated patch dict.

        Note: `apply_patch.py` performs path‑scoped staging **and** the commit.
        We deliberately avoid a second commit here to prevent “nothing to commit”
        failures and to keep all write safeguards in one place.
        """
        from apply_patch import apply_patch  # local import to avoid cycles

        payload = json.dumps(patch, ensure_ascii=False)
        try:
            apply_patch(payload, str(self.repo))
        except Exception as exc:
            # Convert to actionable error (the run will stop immediately)
            log.exception("Patch apply failed for %s: %s", patch.get("file"), exc)
            raise SystemExit(1) from exc

        log.info("Applied patch & committed via apply_patch: %s", patch.get("file"))

    def _iter_paths(self, iteration: int) -> List[str]:
        """
        Return the list of files to process for *iteration*.

        Iteration 1 & 2: code+tests only.
        Iteration 3:     all non‑binary files (code+tests + docs/setup/examples).
        """
        from gpt_review.file_scanner import classify_for_iteration

        return classify_for_iteration(self._scan, iteration=iteration)

    def _process_single_file(self, rel_path: str, iteration: int, consistency: bool = False) -> None:
        """
        Send *rel_path* to the model and apply the full‑file replacement it returns.
        """
        full_path = self.repo / rel_path
        if not full_path.exists():
            log.warning("File vanished before processing: %s", rel_path)
            return

        content = full_path.read_text(encoding="utf-8", errors="replace")
        prompt = self._consistency_prompt(rel_path, content) if consistency else self._file_prompt(rel_path, content, iteration)

        patch = self._ask_fullfile_patch(prompt, rel_path=rel_path, kind="update")

        # Enforce full‑file semantics: op must be update/create; body present.
        if patch.get("op") not in {"update", "create"} or ("body" not in patch and "body_b64" not in patch):
            raise SystemExit(f"Assistant did not return a full‑file patch for {rel_path}.")

        # Always in_progress within iterations; final status is handled later.
        if patch.get("status") not in {"in_progress", "completed"}:
            patch["status"] = "in_progress"

        self._apply_and_commit(patch, f"{self.branch_name}: {patch['op']} {rel_path}")

    def _ask_and_create_new_files(self, iteration: int, processed_paths: Sequence[str]) -> None:
        """
        Ask for a list of new files (strict JSON), then create them one‑by‑one.

        Expected JSON items (flexible keys supported):
            { "path": "relative/path", "purpose|reason|notes": "why", "type": "...", "priority": 1 }
        """
        items = self._ask_json_array(self._new_files_list_prompt(iteration, processed_paths))
        if not items:
            log.info("No new files suggested after iteration %d.", iteration)
            return

        for it in items:
            path = (it.get("path") or "").strip()
            reason = (it.get("reason") or it.get("purpose") or it.get("notes") or "New file requested").strip()
            if not path:
                log.warning("Skipping new file suggestion without 'path': %r", it)
                continue

            # Ask for the full file content now (structured tool call).
            prompt = self._error_fix_file_prompt(file_path=path, reason=reason)
            patch = self._ask_fullfile_patch(prompt, rel_path=path, kind="create")

            # Force op=create to avoid accidental updates to existing files with similar names
            patch["op"] = "create"
            patch["file"] = path
            patch["status"] = patch.get("status") or "in_progress"
            if "body" not in patch and "body_b64" not in patch:
                raise SystemExit(f"Assistant did not provide body/body_b64 for new file {path}")

            self._apply_and_commit(patch, f"{self.branch_name}: create {path}")

    # ────────────────────────────────────────────────────────────────────── #
    # Iterations
    # ────────────────────────────────────────────────────────────────────── #
    def _iteration(self, n: int) -> None:
        """
        Run a single iteration (1, 2, or 3).
        """
        log.info("=== Iteration %d ===", n)

        # On the first iteration, send a one‑time overview to align the model.
        if n == 1:
            self._require_api()
            self._client.note(self._overview_note_text())

        # Process target files one‑by‑one (full replacements)
        targets = self._iter_paths(iteration=n)
        log.info("Iteration %d: processing %d files …", n, len(targets))

        processed: List[str] = []
        for rel in targets:
            try:
                consistency = (n == 3)  # iteration 3: consistency across codebase
                self._process_single_file(rel_path=rel, iteration=n, consistency=consistency)
                processed.append(rel)
            except SystemExit:
                raise
            except Exception as exc:
                log.exception("Failed to process %s: %s", rel, exc)
                raise SystemExit(1) from exc

        # Ask for new files list and create them one‑by‑one
        self._ask_and_create_new_files(iteration=n, processed_paths=processed)

    # ────────────────────────────────────────────────────────────────────── #
    # Post‑iterations: final instructions & error‑fix loop
    # ────────────────────────────────────────────────────────────────────── #
    def _generate_final_instructions(self) -> None:
        """
        Ask the model to synthesize an authoritative *Review Instructions* file
        after iteration 3. We store it at: REVIEW_INSTRUCTIONS.md
        """
        prompt = self._final_instructions_prompt()
        # Create/update via a **tool call** (full file)
        patch = self._ask_fullfile_patch(prompt, rel_path="REVIEW_INSTRUCTIONS.md", kind="create")
        if patch.get("op") not in {"create", "update"}:
            patch["op"] = "create" if not (self.repo / "REVIEW_INSTRUCTIONS.md").exists() else "update"
        patch["file"] = "REVIEW_INSTRUCTIONS.md"
        patch["status"] = patch.get("status") or "in_progress"

        if "body" not in patch and "body_b64" not in patch:
            raise SystemExit("Assistant did not provide body/body_b64 for REVIEW_INSTRUCTIONS.md")

        self._apply_and_commit(patch, f"{self.branch_name}: write REVIEW_INSTRUCTIONS.md")

    def _error_fix_loop(self) -> None:
        """
        Run the provided command until success. On failure, ask for a list of
        affected files and then request each full file replacement.
        """
        if not self.cfg.run_cmd:
            log.info("No --run command provided; skipping error‑fix loop.")
            return

        while True:
            ok, out, code = _run_cmd(self.cfg.run_cmd, self.repo, timeout=self.cfg.api_timeout)
            log_tail = _tail(out, LOG_TAIL_CHARS)
            if ok:
                log.info("Run succeeded (rc=0).")
                break

            log.warning("Run failed (rc=%s). Sending logs to the model.", code)
            items = self._ask_json_array(self._error_fix_list_prompt(log_tail))
            if not items:
                raise SystemExit("Model returned no affected files for the error log; aborting.")

            for it in items:
                path = (it.get("file") or it.get("path") or "").strip()
                reason = (it.get("reason") or "error fix").strip()
                if not path:
                    log.warning("Skipping error fix without a valid path: %r", it)
                    continue

                prompt = self._error_fix_file_prompt(file_path=path, reason=reason)
                patch = self._ask_fullfile_patch(prompt, rel_path=path, kind="update")

                if "body" not in patch and "body_b64" not in patch:
                    raise SystemExit(f"Assistant did not provide full body for error fix: {path}")

                # Keep status in_progress inside the loop
                if patch.get("status") not in {"in_progress", "completed"}:
                    patch["status"] = "in_progress"

                self._apply_and_commit(patch, f"{self.branch_name}: error‑fix {path}")

    # ────────────────────────────────────────────────────────────────────── #
    # Public entry
    # ────────────────────────────────────────────────────────────────────── #
    def run(self) -> None:
        """
        Execute the complete multi‑iteration workflow and push the branch.
        """
        self._ensure_repo()
        self._scan_repo()
        self._branch()
        self._init_clients()

        # Three iterations
        for i in range(1, self.cfg.iterations + 1):
            self._iteration(i)

        # After iteration 3: synthesize final instructions (authoritative run guide)
        self._generate_final_instructions()

        # Error‑fix loop (until the software runs as expected)
        self._error_fix_loop()

        # Mark completion and push
        log.info("All iterations completed on branch '%s' (HEAD=%s).",
                 self.branch_name, _current_commit(self.repo))

        if self.cfg.push_at_end and self.branch_name:
            try:
                _push(self.repo, self.cfg.remote, self.branch_name)
            except Exception as exc:
                log.exception("Failed to push branch: %s", exc)
                raise SystemExit(1) from exc
        else:
            log.info("Skipping push (push_at_end=%s).", self.cfg.push_at_end)
