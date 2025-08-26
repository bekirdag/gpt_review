#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
===============================================================================
GPT‑Review ▸ Multi‑Iteration Orchestrator
===============================================================================

Responsibilities
----------------
• Create a fresh branch per run (e.g., iteration1, iteration2, …).
• Read the instructions + repository structure; build a manifest.
• Run three iterations:
  1) Review/replace *code+config* files one‑by‑one (full files only).
     Then ask for a list of *new files* and create them *one‑by‑one*.
  2) Repeat the process (existing + new files); ask for additional new files.
  3) Review *all files for consistency*; only now process docs/install/setup/examples.

• After iteration 3:
  - Ask the API to generate a *Software Review Instructions* file that states
    how to run the software and what success looks like.
  - Enter an error‑fix loop: run the provided command, collect logs, ask for
    affected files & full replacements; iterate until success.

• Commit after every file; push branch at the end if requested.

Design choices
--------------
• Full‑file semantics: the assistant must always return an *entire* file as
  the replacement (never a diff). We enforce this via the tool schema (same
  shape as schema.json) and strict prompting.
• Deferral: docs/installation/setup/example files are intentionally skipped
  in iterations 1 & 2 and processed in iteration 3 to avoid churn.
• Explicit paths: every action uses repo‑root‑relative paths to prevent
  accidental nesting/misplacement.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shlex
import subprocess
import sys
import textwrap
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, List, Optional, Sequence, Tuple

from gpt_review import get_logger
from patch_validator import validate_patch

# NOTE: These modules will be added next (one file per message):
#   - from gpt_review.prompts import build_overview_prompt, build_file_prompt, build_new_files_prompt, build_consistency_prompt, build_error_fix_list_prompt, build_error_fix_file_prompt, build_final_instructions_prompt
#   - from gpt_review.file_scanner import RepoScan, scan_repository, classify_for_iteration
#   - from gpt_review.api_client import OpenAIClient, submit_patch_call, strict_json_array

# For applying patches (complete file writes with path‑scoped staging)
#   - from apply_patch import apply_patch

# -----------------------------------------------------------------------------
# Logger
# -----------------------------------------------------------------------------
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

# Paths/dirs we always ignore when scanning the repo
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

    # scanning / categorisation settings
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

    # Create branch at current HEAD (or initial commit after an orphan)
    log.info("Creating/checkout branch: %s", name)
    _git(repo, "checkout", "-B", name, check=True)
    return name


def _commit(repo: Path, message: str, paths: Sequence[str]) -> None:
    """
    Stage only *paths* and commit with *message*. Keeps staging path‑scoped.
    """
    if not paths:
        return
    _git(repo, "add", "--", *paths, check=True)
    _git(repo, "commit", "-m", message, check=True)


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
    High‑level multi‑iteration review loop.

    Usage:
        cfg = OrchestratorConfig(instructions_path=Path(...), repo=Path(...))
        ReviewWorkflow(cfg).run()
    """

    def __init__(self, cfg: OrchestratorConfig):
        self.cfg = cfg
        self.repo = cfg.repo
        self.instructions = self._read_instructions(cfg.instructions_path)

        # Placeholders; wired up after companion modules are added.
        self._client = None  # will be OpenAIClient
        self._scan = None    # will be RepoScan

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
        Late import to keep module load light; avoids hard dependency if the
        orchestrator isn't used. This connects the OpenAI client wrapper.
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
        Build a manifest of files and their categories.
        """
        try:
            from gpt_review.file_scanner import scan_repository
        except Exception as exc:
            log.exception("Failed to import file scanner: %s", exc)
            raise SystemExit(1) from exc

        self._scan = scan_repository(self.repo, ignores=self.cfg.ignores)
        log.info("Scanned repo: %s files (code=%s, docs/setup/examples=%s)",
                 len(self._scan.all_files),
                 len(self._scan.code_and_config),
                 len(self._scan.docs_and_extras))

    def _branch(self) -> None:
        self.branch_name = _ensure_branch(self.repo, self.cfg.branch_prefix)
        log.info("Working on branch '%s' (HEAD=%s)", self.branch_name, _current_commit(self.repo))

    # ────────────────────────────────────────────────────────────────────── #
    # Prompt helpers (defers to prompts.py)
    # ────────────────────────────────────────────────────────────────────── #
    def _overview_prompt(self) -> str:
        from gpt_review.prompts import build_overview_prompt

        return build_overview_prompt(
            instructions=self.instructions,
            manifest=self._scan.manifest_text(),
        )

    def _file_prompt(self, rel_path: str, content: str, iteration: int) -> str:
        from gpt_review.prompts import build_file_prompt

        return build_file_prompt(
            instructions=self.instructions,
            manifest=self._scan.manifest_text(),
            iteration=iteration,
            rel_path=rel_path,
            content=content,
        )

    def _consistency_prompt(self, rel_path: str, content: str) -> str:
        from gpt_review.prompts import build_consistency_prompt

        return build_consistency_prompt(
            instructions=self.instructions,
            manifest=self._scan.manifest_text(),
            rel_path=rel_path,
            content=content,
        )

    def _new_files_list_prompt(self, iteration: int) -> str:
        from gpt_review.prompts import build_new_files_prompt

        return build_new_files_prompt(
            instructions=self.instructions,
            manifest=self._scan.manifest_text(),
            iteration=iteration,
        )

    def _error_fix_list_prompt(self, log_tail: str) -> str:
        from gpt_review.prompts import build_error_fix_list_prompt

        return build_error_fix_list_prompt(
            instructions=self.instructions,
            manifest=self._scan.manifest_text(),
            error_log_tail=log_tail,
        )

    def _error_fix_file_prompt(self, file_path: str, reason: str) -> str:
        from gpt_review.prompts import build_error_fix_file_prompt

        return build_error_fix_file_prompt(
            instructions=self.instructions,
            manifest=self._scan.manifest_text(),
            rel_path=file_path,
            reason=reason,
        )

    def _final_instructions_prompt(self) -> str:
        from gpt_review.prompts import build_final_instructions_prompt

        return build_final_instructions_prompt(
            instructions=self.instructions,
            manifest=self._scan.manifest_text(),
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
    def _apply_and_commit(self, patch: dict, commit_msg: str) -> None:
        """
        Apply a validated patch dict and commit it (path‑scoped).
        """
        from apply_patch import apply_patch  # local import to avoid cycles

        payload = json.dumps(patch, ensure_ascii=False)
        try:
            apply_patch(payload, str(self.repo))
        except Exception as exc:
            # Convert to actionable error (the run will stop immediately)
            log.exception("Patch apply failed for %s: %s", patch.get("file"), exc)
            raise SystemExit(1) from exc

        _commit(self.repo, commit_msg, [patch["file"]])

    def _iter_paths(self, iteration: int) -> List[str]:
        """
        Return the list of files to process for *iteration*.

        Iteration 1 & 2: code+config only.
        Iteration 3:     all files (code+config + docs/setup/examples).
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
        if consistency:
            prompt = self._consistency_prompt(rel_path, content)
        else:
            prompt = self._file_prompt(rel_path, content, iteration)

        kind = "update"
        patch = self._ask_fullfile_patch(prompt, rel_path=rel_path, kind=kind)

        # Enforce full‑file semantics: op must be update/create; body present.
        if patch.get("op") not in {"update", "create"} or ("body" not in patch and "body_b64" not in patch):
            raise SystemExit(f"Assistant did not return a full‑file patch for {rel_path}.")

        # Always in_progress within iterations; final status is handled later.
        if patch.get("status") not in {"in_progress", "completed"}:
            patch["status"] = "in_progress"

        self._apply_and_commit(patch, f"{self.branch_name}: {patch['op']} {rel_path}")

    def _ask_and_create_new_files(self, iteration: int) -> None:
        """
        Ask for a list of new files (strict JSON), then create them one‑by‑one.
        The JSON items must include:
            { "path": "relative/path", "reason": "why" }
        """
        items = self._ask_json_array(self._new_files_list_prompt(iteration))
        if not items:
            log.info("No new files suggested after iteration %d.", iteration)
            return

        for it in items:
            path = (it.get("path") or "").strip()
            reason = (it.get("reason") or "").strip()
            if not path:
                log.warning("Skipping new file suggestion without 'path': %r", it)
                continue

            # Ask for the full file content now (structured tool call).
            prompt = self._error_fix_file_prompt(file_path=path, reason=reason or "New file requested")
            patch = self._ask_fullfile_patch(prompt, rel_path=path, expected_kind="create")

            # Force op=create to avoid accidental updates
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
            overview = self._overview_prompt()
            self._client.note(overview)  # stores as a user message (see api_client)

        # Process target files one‑by‑one (full replacements)
        targets = self._iter_paths(iteration=n)
        log.info("Iteration %d: processing %d files …", n, len(targets))
        for rel in targets:
            try:
                consistency = (n == 3)  # iteration 3: consistency across codebase
                self._process_single_file(rel_path=rel, iteration=n, consistency=consistency)
            except SystemExit:
                raise
            except Exception as exc:
                log.exception("Failed to process %s: %s", rel, exc)
                raise SystemExit(1) from exc

        # Ask for new files list and create them one‑by‑one
        self._ask_and_create_new_files(iteration=n)

    # ────────────────────────────────────────────────────────────────────── #
    # Post‑iterations: final instructions & error‑fix loop
    # ────────────────────────────────────────────────────────────────────── #
    def _generate_final_instructions(self) -> None:
        """
        Ask the model to synthesize an authoritative *Software Review Instructions*
        file based on what it has learned. We store it at:
            REVIEW_INSTRUCTIONS.md
        """
        prompt = self._final_instructions_prompt()
        items = self._ask_json_array(prompt)
        # We expect exactly one file in the array:
        #   [{ "path": "REVIEW_INSTRUCTIONS.md", "body": "<markdown>" }]
        if not items:
            log.warning("Model did not propose a REVIEW_INSTRUCTIONS file; skipping.")
            return

        itm = items[0]
        path = itm.get("path") or "REVIEW_INSTRUCTIONS.md"
        body = itm.get("body") or ""
        if not body:
            log.warning("REVIEW_INSTRUCTIONS empty; skipping.")
            return

        patch = {
            "op": "create" if not (self.repo / path).exists() else "update",
            "file": path,
            "body": body,
            "status": "in_progress",
        }
        validate_patch(json.dumps(patch, ensure_ascii=False))
        self._apply_and_commit(patch, f"{self.branch_name}: write {path}")

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
                path = (it.get("path") or "").strip()
                reason = (it.get("reason") or "error fix").strip()
                if not path:
                    log.warning("Skipping error fix without 'path': %r", it)
                    continue

                prompt = self._error_fix_file_prompt(file_path=path, reason=reason)
                patch = self._ask_fullfile_patch(prompt, rel_path=path, expected_kind="update")

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
