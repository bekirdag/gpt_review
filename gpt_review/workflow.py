#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
===============================================================================
GPT‑Review ▸ Multi‑Iteration Orchestrator (API mode, compatibility path)
===============================================================================

This orchestrator drives the three‑iteration “edit → run → fix” workflow using
the structured tool call contract (submit_patch). It coordinates:

  • Repo scan & per‑iteration file selection
  • Per‑file prompts that MUST return **complete files** (never diffs)
  • New‑file discovery & creation (strict JSON → one file per patch)
  • Optional run/test command execution and error‑driven fix loops
  • Blueprint preflight and prompt grounding via blueprint summaries

Path hygiene
------------
All file operations enforce **repo‑relative POSIX paths** and reuse the
centralized validator from `patch_validator`. This rejects absolute paths,
backslashes, parent‑traversal, anything under `.git/`, and Windows drive
letters. When importing that helper is not possible (older installs), we
fall back to an identical local check.

This module never mutates Git state except through the apply_patch pipeline.
"""
from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import textwrap
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, List, Optional, Sequence, Tuple

from gpt_review import get_logger
from patch_validator import validate_patch

# Prefer the centralized path guard from patch_validator (if available)
try:  # pragma: no cover - import availability varies across versions
    from patch_validator import is_safe_repo_rel_posix  # type: ignore
except Exception:  # pragma: no cover
    def is_safe_repo_rel_posix(path: str) -> bool:
        """
        Local fallback (kept in sync with patch_validator):
          - POSIX separators only; not absolute; no backslashes; no '..'
          - not under '.git/' and not '.git' itself; no empty segments
          - reject Windows drive letters (e.g., 'C:...')
          - normalization must be stable (reject 'a//b', 'a/./b', trailing '/')
        """
        import re
        if not isinstance(path, str) or not path.strip():
            return False
        raw = path.strip()
        if "\\" in raw or raw.startswith("/"):
            return False
        if re.match(r"^[A-Za-z]:", raw):
            return False
        if raw == ".git" or raw.startswith(".git/") or "/.git/" in raw or raw.endswith("/.git"):
            return False
        if ".." in raw.split("/"):
            return False
        p = PurePosixPath(raw)
        if str(p) != raw:
            return False
        return all(seg for seg in p.parts)

# Canonical blueprint helpers
from gpt_review.blueprints_util import (
    ensure_blueprint_dir,
    blueprint_paths,
    missing_blueprints,
    summarize_blueprints,
)

# Fallback repo scanning (if file_scanner API is unavailable)
from gpt_review.fs_utils import classify_paths, summarize_repo

log = get_logger(__name__)

# -----------------------------------------------------------------------------
# Defaults & environment toggles
# -----------------------------------------------------------------------------
DEFAULT_MODEL = os.getenv("GPT_REVIEW_MODEL", "gpt-5-codex")
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

# Optional PR creation
CREATE_PR = os.getenv("GPT_REVIEW_CREATE_PR", "").strip().lower() in {"1", "true", "yes", "on"}


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


def _branch_exists(repo: Path, name: str) -> bool:
    """
    Return True if the local branch exists.
    """
    return subprocess.run(
        ["git", "-C", str(repo), "show-ref", "--verify", "--quiet", f"refs/heads/{name}"]
    ).returncode == 0


def _has_commits(repo: Path) -> bool:
    """
    True if repository has at least one commit.
    """
    return subprocess.run(["git", "-C", str(repo), "rev-parse", "--verify", "-q", "HEAD"]).returncode == 0


def _checkout_branch(repo: Path, name: str) -> None:
    """
    Create or switch to *name* in *repo*. Safe if branch already exists.
    Falls back to **orphan** branch on fresh repositories and to `git checkout`
    when `git switch` is unavailable.
    """
    if _branch_exists(repo, name):
        # Prefer `switch`; fall back to `checkout`.
        if subprocess.run(["git", "-C", str(repo), "switch", name]).returncode != 0:
            subprocess.run(["git", "-C", str(repo), "checkout", name], check=True)
        log.info("Checked out existing branch '%s'.", name)
        return

    if _has_commits(repo):
        if subprocess.run(["git", "-C", str(repo), "switch", "-c", name]).returncode != 0:
            subprocess.run(["git", "-C", str(repo), "checkout", "-b", name], check=True)
        log.info("Created and switched to new branch '%s'.", name)
    else:
        # No commits yet → orphan branch.
        if subprocess.run(["git", "-C", str(repo), "switch", "--orphan", name]).returncode != 0:
            subprocess.run(["git", "-C", str(repo), "checkout", "--orphan", name], check=True)
        log.info("Created **orphan** branch '%s' (fresh repository).", name)


def _iteration_branch_name(prefix: str, i: int) -> str:
    return f"{prefix}{i}"


def _push(repo: Path, remote: str, branch: str) -> None:
    """
    Push the branch to the remote with upstream.
    """
    log.info("Pushing branch '%s' to remote '%s' …", branch, remote)
    _git(repo, "push", "-u", remote, branch, check=True)


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
    Controlled by GPT_REVIEW_CREATE_PR=1.
    """
    if not CREATE_PR:
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
    log.info("PR not created automatically. You can create one with:\n"
             "  gh pr create --base %s --head %s --title %r --body %r", base, branch, title, body)


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
    High‑level multi‑iteration review loop (API‑driven, compatibility path).

    Usage:
        cfg = OrchestratorConfig(instructions_path=Path(...), repo=Path(...))
        ReviewWorkflow(cfg).run()
    """

    def __init__(self, cfg: OrchestratorConfig):
        self.cfg = cfg
        self.repo = cfg.repo
        self.instructions = self._read_instructions(cfg.instructions_path)

        # Late‑bound components
        self._client = None  # type: ignore[assignment]  # CodexClient
        self._scan = None    # optional: file_scanner facade

        # Bookkeeping
        self.branch_name: Optional[str] = None
        self._bp_summary: Optional[str] = None  # abridged blueprints for prompts

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
        Connect the GPT-Codex client wrapper (tool‑forced). Keep imports local
        so other entry points don't pay this cost unless needed.
        """
        try:
            from gpt_review.api_client import CodexClient
        except Exception as exc:
            log.exception("Failed to import API client: %s", exc)
            raise SystemExit(1) from exc

        self._client = CodexClient(
            model=self.cfg.model,
            timeout_s=self.cfg.api_timeout,
        )

    def _scan_repo(self) -> None:
        """
        Build a manifest of files and their categories (non‑binary split).
        Prefer file_scanner facade; fallback to fs_utils for manifest text.
        """
        try:
            from gpt_review.file_scanner import scan_repository  # type: ignore
            self._scan = scan_repository(self.repo, ignores=self.cfg.ignores)
            log.info("Scanned repo: %s files (code+tests=%s, deferred=%s)",
                     len(self._scan.all_files),
                     len(self._scan.code_and_config),
                     len(self._scan.docs_and_extras))
        except Exception as exc:
            # Fallback: derive only the manifest text via fs_utils
            self._scan = None
            log.warning("file_scanner unavailable; falling back to fs_utils: %s", exc)

    def _manifest_text(self) -> str:
        if self._scan and hasattr(self._scan, "manifest_text"):
            try:
                return self._scan.manifest_text()
            except Exception:
                pass
        # Fallback compact summary
        return summarize_repo(self.repo)

    # ────────────────────────────────────────────────────────────────────── #
    # Blueprints preflight + summary
    # ────────────────────────────────────────────────────────────────────── #
    def _posix_rel(self, p: Path) -> str:
        try:
            return p.resolve().relative_to(self.repo.resolve()).as_posix()
        except Exception:
            return p.as_posix()

    def _ensure_blueprints(self) -> None:
        """
        Ensure the four blueprint documents exist, creating each missing file
        via a **submit_patch create** (full Markdown body) and committing it.
        """
        ensure_blueprint_dir(self.repo)
        miss = missing_blueprints(self.repo)
        if not miss:
            log.info("Blueprint preflight: all documents exist.")
            return

        # Resolve repo-relative POSIX paths
        abs_paths = blueprint_paths(self.repo)
        for key in miss:
            rel_path = self._posix_rel(abs_paths[key])

            # Compose a strict, per‑file blueprint creation request
            sys_msg = (
                "You are GPT‑Review. Respond ONLY by calling `submit_patch` to CREATE exactly one file. "
                "Return a COMPLETE Markdown file in `body`. Use the EXACT repo‑relative POSIX path I provide. "
                "No prose."
            )
            details = textwrap.dedent(
                f"""
                Create the following blueprint document **now** with clear, structured sections:
                - Path   : {rel_path}
                - Title  : {key.replace('_', ' ').title()}

                Purpose:
                These four documents guide the entire review and build. Write the full content here:
                  1) Whitepaper & Engineering Blueprint – problem, scope, architecture, trade‑offs.
                  2) Build Guide – environment, dependencies, setup, commands.
                  3) Software Design Specification (SDS) – detailed components, interfaces, data models.
                  4) Project Code Files and Instructions – repository layout, entrypoints, run/test commands, expected outputs.

                Inputs (from user instructions):
                {self.instructions}
                """
            ).strip()

            prompt = f"{sys_msg}\n\n{details}"
            patch = self._ask_fullfile_patch(prompt, rel_path=rel_path, kind="create")

            # Tighten expectations and apply
            patch["op"] = "create"
            patch["file"] = rel_path
            if "body" not in patch and "body_b64" not in patch:
                raise SystemExit(f"Assistant did not provide body/body_b64 for blueprint {rel_path}")
            self._apply_and_commit(patch, f"blueprint: create {rel_path}")

        log.info("Blueprint documents created.")

    def _blueprints_summary(self) -> str:
        # Reasonable per‑doc cap; small enough for prompts
        try:
            return summarize_blueprints(self.repo, max_chars_per_doc=1500)
        except Exception as exc:
            log.warning("Failed to prepare blueprints summary: %s", exc)
            return ""

    # ────────────────────────────────────────────────────────────────────── #
    # Plan artifacts (initial/final)
    # ────────────────────────────────────────────────────────────────────── #
    def _write_full_file(self, rel_path: str, content: str) -> None:
        """
        Create or update *rel_path* with *content* as a full-file operation.
        Chooses op based on current existence to keep reruns idempotent.
        """
        op = "update" if (self.repo / rel_path).exists() else "create"
        self._apply_full_file(rel_path, op, content)

    def _generate_plan_artifacts(self, *, phase: str) -> Tuple[List[str], List[str]]:
        """
        Create plan artifacts for a given *phase*.

        Returns (run_commands, test_commands). When 'initial', artifacts are:
            .gpt-review/initial_plan.json, INITIAL_REVIEW_PLAN.md
        When 'final', artifacts are:
            .gpt-review/review_plan.json, REVIEW_GUIDE.md
        """
        assert self._client is not None

        # Build a compact prompt (phase-sensitive)
        preface = (
            "Before we start editing files, produce an initial execution plan with "
            "**actionable commands** to run the software and (optionally) tests on a clean machine."
            if phase == "initial"
            else "We have completed the third iteration of code review. Produce a concise execution plan with "
                 "**actionable commands** to run the software and its tests."
        )
        bp = self._bp_summary or ""
        prompt = textwrap.dedent(
            f"""
            {preface}

            Blueprint documents (abridged):
            {bp}

            Return ONLY the function call `propose_review_plan` with:
              - run_commands: list[str]  (required)
              - test_commands: list[str] (optional)
              - description: str
              - hints: list[str] (optional)

            Instructions:
            {self.instructions}

            Repository overview:
            ```
            {self._manifest_text()}
            ```
            """
        ).strip()

        args = self._client.call_propose_review_plan(prompt)  # type: ignore[attr-defined]
        description = args.get("description") or ""
        run_cmds = [c for c in (args.get("run_commands") or []) if isinstance(c, str) and c.strip()]
        test_cmds = [c for c in (args.get("test_commands") or []) if isinstance(c, str) and c.strip()]
        hints = [h for h in (args.get("hints") or []) if isinstance(h, str) and h.strip()]

        # Ensure the dot‑dir exists by creating a harmless .keep via apply_patch (idempotent)
        self._write_full_file(".gpt-review/.keep", "")

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
            "generated_by": "gpt_review.workflow",
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

        # Write artifacts (full files) idempotently
        self._write_full_file(plan_path, json.dumps(plan_json, indent=2, ensure_ascii=False) + "\n")
        self._write_full_file(guide_path, guide_md)
        return run_cmds, test_cmds

    # ────────────────────────────────────────────────────────────────────── #
    # Prompt helpers (wired to gpt_review.prompts)
    # ────────────────────────────────────────────────────────────────────── #
    def _overview_note_text(self) -> str:
        manifest = self._manifest_text()
        bp = f"\nBlueprint documents (abridged):\n{self._bp_summary}\n" if self._bp_summary else ""
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
        return f"Objectives:\n{self.instructions}\n{bp}\nRepository manifest:\n{manifest}\n\n{rules}"

    def _file_prompt(self, rel_path: str, content: str, iteration: int) -> str:
        from gpt_review.prompts import build_file_review_prompt

        return build_file_review_prompt(
            iteration=iteration,
            rel_path=rel_path,
            file_text=content,
            file_notes=None,
            blueprints_summary=self._bp_summary,
        )

    def _consistency_prompt(self, rel_path: str, content: str) -> str:
        from gpt_review.prompts import build_file_review_prompt

        return build_file_review_prompt(
            iteration=3,
            rel_path=rel_path,
            file_text=content,
            file_notes=(
                "Consistency pass context: align naming, imports, error handling, logging, "
                "and configuration with the rest of the codebase. Prefer minimal, coherent changes."
            ),
            blueprints_summary=self._bp_summary,
        )

    def _new_files_list_prompt(self, iteration: int, processed: Sequence[str]) -> str:
        from gpt_review.prompts import build_new_files_discovery_prompt

        return build_new_files_discovery_prompt(
            iteration=iteration,
            processed_paths=list(processed),
            repo_overview=self._manifest_text(),
            blueprints_summary=self._bp_summary,
        )

    def _error_fix_list_prompt(self, log_tail: str) -> str:
        from gpt_review.prompts import build_error_diagnosis_prompt

        run_cmd = self.cfg.run_cmd or "<no-run-command-provided>"
        return build_error_diagnosis_prompt(
            run_command=run_cmd,
            error_log_tail=log_tail,
            blueprints_summary=self._bp_summary,
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
            blueprints_summary=self._bp_summary,
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
            blueprints_summary=self._bp_summary,
        )

    # ────────────────────────────────────────────────────────────────────── #
    # GPT-Codex interactions
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
        Apply a validated patch dict via apply_patch (path‑scoped staging + commit).

        Additional hygiene:
          • Enforce POSIX repo‑relative paths (and rename targets).
        """
        # Path hygiene – aligns with API driver & patch_validator
        src = (patch.get("file") or "").strip()
        if src and not is_safe_repo_rel_posix(src):
            raise SystemExit(f"Unsafe or non‑POSIX repo‑relative path: {src!r}")
        if patch.get("op") == "rename":
            tgt = (patch.get("target") or "").strip()
            if not is_safe_repo_rel_posix(tgt):
                raise SystemExit(f"Unsafe or non‑POSIX target path for rename: {tgt!r}")

        from apply_patch import apply_patch  # local import to avoid cycles

        payload = json.dumps(patch, ensure_ascii=False)
        try:
            apply_patch(payload, str(self.repo))
        except Exception as exc:
            log.exception("Patch apply failed for %s: %s", patch.get("file"), exc)
            raise SystemExit(1) from exc

        log.info("Applied patch & committed via apply_patch: %s", patch.get("file"))

    def _apply_full_file(self, rel_path: str, action: str, content: str) -> None:
        """
        Convenience wrapper to create a *full-file* patch and apply it directly.
        """
        if not is_safe_repo_rel_posix(rel_path):
            raise SystemExit(f"Unsafe or non‑POSIX repo‑relative path: {rel_path!r}")
        patch = {"op": action, "file": rel_path, "body": content, "status": "in_progress"}
        validate_patch(json.dumps(patch, ensure_ascii=False))
        self._apply_and_commit(patch, f"{action} {rel_path}")

    def _iter_paths(self, iteration: int) -> List[str]:
        """
        Return the list of files to process for *iteration*.

        Iteration 1 & 2: code+tests only.
        Iteration 3:     all non‑binary files (code+tests + docs/setup/examples).
        """
        # Preferred path: dedicated file_scanner helper
        try:
            from gpt_review.file_scanner import classify_for_iteration  # type: ignore
            if self._scan is None:
                raise RuntimeError("scan_repository not initialized")
            return classify_for_iteration(self._scan, iteration=iteration)
        except Exception:
            # Fallback: fs_utils classification
            code_like, deferred = classify_paths(self.repo)
            if iteration in (1, 2):
                return [p.relative_to(self.repo).as_posix() for p in code_like]
            return [p.relative_to(self.repo).as_posix() for p in [*code_like, *deferred]]

    def _process_single_file(self, rel_path: str, iteration: int, consistency: bool = False) -> None:
        """
        Send *rel_path* to the model and apply the returned patch.

        Enforces **complete file bodies** for create/update; allows delete/rename/chmod
        as returned by the model (schema validation handles required fields).
        """
        full_path = self.repo / rel_path
        if not full_path.exists():
            log.warning("File vanished before processing: %s", rel_path)
            return

        content = full_path.read_text(encoding="utf-8", errors="replace")
        prompt = self._consistency_prompt(rel_path, content) if consistency else self._file_prompt(rel_path, content, iteration)

        patch = self._ask_fullfile_patch(prompt, rel_path=rel_path, kind="update")

        # Ensure path/status present and consistent
        patch["file"] = rel_path
        if patch.get("status") not in {"in_progress", "completed"}:
            patch["status"] = "in_progress"

        # For create/update we must have a full body; other ops (delete/rename/chmod) are fine.
        op = (patch.get("op") or "").strip()
        if op in {"create", "update"} and ("body" not in patch and "body_b64" not in patch):
            raise SystemExit(f"Assistant did not return a full file body for {rel_path} (op={op}).")

        self._apply_and_commit(patch, f"{self.branch_name}: {op or 'update'} {rel_path}")

    def _ask_and_create_new_files(self, iteration: int, processed_paths: Sequence[str]) -> None:
        """
        Ask for a list of new files (strict JSON), then create them one‑by‑one.

        Expected JSON items:
            { "path": "relative/path", "reason|purpose|notes": "why", "type": "...", "priority": 1 }
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
            if not is_safe_repo_rel_posix(path):
                log.warning("Skipping new file with unsafe path: %r", path)
                continue

            # Ask for the full file content now (structured tool call).
            prompt = self._error_fix_file_prompt(file_path=path, reason=reason)
            patch = self._ask_fullfile_patch(prompt, rel_path=path, kind="create")

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
    def _checkout_iteration_branch(self, i: int) -> None:
        """
        Ensure we're on the deterministic branch 'iteration{i}' for this pass.
        """
        name = _iteration_branch_name(self.cfg.branch_prefix, i)
        _checkout_branch(self.repo, name)
        self.branch_name = name
        log.info("Working on branch '%s' (HEAD=%s)", self.branch_name, _current_commit(self.repo))

    def _iteration(self, n: int) -> None:
        """
        Run a single iteration (1, 2, or 3).
        """
        log.info("=== Iteration %d ===", n)
        self._checkout_iteration_branch(n)

        # On the first iteration, send a one‑time overview and initial plan.
        if n == 1:
            self._require_api()
            self._client.note(self._overview_note_text())
            try:
                self._generate_plan_artifacts(phase="initial")
            except Exception as exc:
                log.warning("Initial plan artifacts step failed: %s (continuing).", exc)

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
    # Post‑iterations: final instructions, final plan & error‑fix loop
    # ────────────────────────────────────────────────────────────────────── #
    def _generate_final_instructions(self) -> None:
        """
        Ask the model to synthesize an authoritative *Review Instructions* file
        after iteration 3. We store it at: REVIEW_INSTRUCTIONS.md
        """
        prompt = self._final_instructions_prompt()
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
                if not is_safe_repo_rel_posix(path):
                    log.warning("Skipping error fix with unsafe path: %r", path)
                    continue

                prompt = self._error_fix_file_prompt(file_path=path, reason=reason)
                patch = self._ask_fullfile_patch(prompt, rel_path=path, kind="update")

                if "body" not in patch and "body_b64" not in patch:
                    raise SystemExit(f"Assistant did not provide full body for error fix: {path}")

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
        self._init_clients()

        # Blueprints preflight + summary (up‑front)
        self._ensure_blueprints()
        self._bp_summary = self._blueprints_summary()

        # Three iterations (deterministic branches iteration1/2/3)
        for i in range(1, self.cfg.iterations + 1):
            self._iteration(i)

        # After iteration 3: synthesize final plan + instructions (authoritative)
        try:
            self._generate_plan_artifacts(phase="final")
        except Exception as exc:
            log.warning("Final plan artifacts step failed: %s (continuing).", exc)

        self._generate_final_instructions()

        # Error‑fix loop (until the software runs as expected)
        self._error_fix_loop()

        # Push & optional PR
        log.info("All iterations completed on branch '%s' (HEAD=%s).",
                 self.branch_name, _current_commit(self.repo))
        if self.cfg.push_at_end and self.branch_name:
            try:
                _push(self.repo, self.cfg.remote, self.branch_name)
            except Exception as exc:
                log.exception("Failed to push branch: %s", exc)
                raise SystemExit(1) from exc
            _maybe_create_pull_request(self.repo, branch=self.branch_name, remote=self.cfg.remote)
        else:
            log.info("Skipping push (push_at_end=%s).", self.cfg.push_at_end)
