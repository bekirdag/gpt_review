#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
===============================================================================
GPT‑Review ▸ Git helpers for multi‑iteration workflow
===============================================================================

Responsibilities
----------------
* Verify repository structure (.git present) and cleanliness before starting.
* Resolve a sensible **base branch** (origin/HEAD → main/master → current).
* Create a **new iteration branch** (e.g. iteration1, iteration2, …).
  - If the desired name already exists, append a timestamp suffix safely.
* Query commit / branch state in a resilient way (works on fresh repos too).
* Push the current branch to a remote (if configured), setting upstream on first push.

Design notes
------------
* All interactions go through `_git()` which logs commands and captures output.
* No global side‑effects; everything is scoped to the provided repository path.
* We avoid sweeping changes: staging/committing remains under the control of
  the existing patch applier (`apply_patch.py`), which performs **path‑scoped**
  staging and already has tests covering this behaviour.

Usage (example)
---------------
    from pathlib import Path
    from gpt_review.git_ops import GitOps

    repo = GitOps(Path("/path/to/repo"))
    repo.ensure_repo_ready()         # raises early if not a repo or dirty
    branch = repo.create_iteration_branch(iteration=1)   # → "iteration1"
    # ... run the review workflow which creates commits one by one ...
    repo.push_current_branch()       # optional (if remote exists)

The orchestrator will call these helpers at iteration boundaries.
"""
from __future__ import annotations

import datetime as _dt
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from gpt_review import get_logger

log = get_logger(__name__)


@dataclass(frozen=True)
class GitRunResult:
    """Simple carrier for git command results."""
    ok: bool
    code: int
    out: str
    err: str


class GitOps:
    """
    Convenience wrapper around common `git` shell operations.

    This class is intentionally minimal and dependency‑free.  It centralises
    logging and error messages so higher‑level orchestration code stays clean.
    """

    def __init__(self, repo: Path):
        self.repo = Path(repo).expanduser().resolve()

    # --------------------------------------------------------------------- #
    # Core plumbing
    # --------------------------------------------------------------------- #
    def _git(self, *args: str, check: bool = False) -> GitRunResult:
        """
        Run `git -C <repo> <args...>` and return a structured result.

        Parameters
        ----------
        args : str
            Raw git arguments, e.g. ("status", "--porcelain").
        check : bool
            If True, raise RuntimeError on non‑zero exit codes.

        Returns
        -------
        GitRunResult
        """
        cmd = ["git", "-C", str(self.repo), *args]
        log.debug("git %s", " ".join(args))
        try:
            res = subprocess.run(
                cmd, capture_output=True, text=True, check=False
            )
        except Exception as exc:
            log.exception("Failed to execute git: %s", exc)
            raise RuntimeError(f"Failed to execute git: {exc}") from exc

        ok = res.returncode == 0
        out = (res.stdout or "").strip()
        err = (res.stderr or "").strip()

        if check and not ok:
            msg = f"git {' '.join(args)} failed (rc={res.returncode}): {err or out}"
            log.error(msg)
            raise RuntimeError(msg)

        if not ok:
            log.debug("git returned rc=%s | stdout=%r | stderr=%r", res.returncode, out, err)

        return GitRunResult(ok=ok, code=res.returncode, out=out, err=err)

    # --------------------------------------------------------------------- #
    # Repo assertions & facts
    # --------------------------------------------------------------------- #
    def ensure_repo_ready(self) -> None:
        """
        Ensure the path is a Git repository and the working tree is clean.

        Raises
        ------
        RuntimeError
            If `.git/` is missing or local changes are present.
        """
        if not (self.repo / ".git").exists():
            raise RuntimeError(f"Not a git repository: {self.repo}")

        # `status --porcelain` is empty when clean
        res = self._git("status", "--porcelain")
        if res.out:
            log.error("Working tree has uncommitted changes:\n%s", res.out)
            raise RuntimeError(
                "Working tree is not clean. Please commit/stash your changes before starting."
            )
        log.info("Repository ready: %s (clean working tree)", self.repo)

    def current_branch(self) -> str:
        """
        Return the current branch name or 'HEAD' if detached.
        """
        res = self._git("rev-parse", "--abbrev-ref", "HEAD", check=True)
        name = res.out or "HEAD"
        log.debug("Current branch: %s", name)
        return name

    def current_commit(self) -> str:
        """
        Return the HEAD commit SHA, or '<no-commits-yet>' on fresh repos.
        """
        res = self._git("rev-parse", "--verify", "-q", "HEAD")
        sha = res.out
        if res.ok and sha:
            return sha
        return "<no-commits-yet>"

    def has_remote(self, name: str = "origin") -> bool:
        """
        True if the remote exists (used before attempting to push).
        """
        res = self._git("remote", "get-url", name)
        exists = res.ok and bool(res.out)
        log.debug("Remote %r exists: %s", name, exists)
        return exists

    def _guess_default_base(self) -> str:
        """
        Guess a sensible base branch for new iteration branches.

        Preference:
        1) origin/HEAD target (e.g. origin/main → main)
        2) 'main' if it exists locally
        3) 'master' if it exists locally
        4) current branch (even if detached, git will use the commit)

        Returns
        -------
        str
            Branch or refname suitable for `git checkout -b <new> <base>`.
        """
        # 1) origin/HEAD → refs/remotes/origin/<branch>
        res = self._git("symbolic-ref", "-q", "refs/remotes/origin/HEAD")
        if res.ok and res.out:
            # Example output: refs/remotes/origin/main → base 'main'
            try:
                base = res.out.rsplit("/", 1)[-1]
                if base:
                    log.debug("Base branch via origin/HEAD: %s", base)
                    return base
            except Exception:
                pass

        # 2) local 'main'
        if self._git("show-ref", "--verify", "--quiet", "refs/heads/main").ok:
            log.debug("Base branch via local 'main'")
            return "main"

        # 3) local 'master'
        if self._git("show-ref", "--verify", "--quiet", "refs/heads/master").ok:
            log.debug("Base branch via local 'master'")
            return "master"

        # 4) fallback to current ref
        cur = self.current_branch()
        log.debug("Base branch fallback to current: %s", cur)
        return cur

    # --------------------------------------------------------------------- #
    # Branch management
    # --------------------------------------------------------------------- #
    def _unique_branch_name(self, desired: str) -> str:
        """
        If *desired* exists, return a unique suffixed name:
          desired-YYYYmmdd-HHMMSS

        This is deterministic per second and avoids guessing counters.
        """
        if not self._git("show-ref", "--verify", "--quiet", f"refs/heads/{desired}").ok:
            return desired
        ts = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        unique = f"{desired}-{ts}"
        log.warning("Branch %r already exists; using %r instead.", desired, unique)
        return unique

    def create_iteration_branch(self, *, iteration: int, base: Optional[str] = None) -> str:
        """
        Create and switch to a new branch for the given *iteration*.

        Parameters
        ----------
        iteration : int
            Iteration number (1‑based). The branch name will be 'iteration{N}'.
        base : str | None
            Optional explicit base. If omitted, we guess a sensible base.

        Returns
        -------
        str
            The actual branch name used (may include a timestamp suffix).

        Raises
        ------
        RuntimeError
            If branch creation fails.
        """
        desired = f"iteration{int(iteration)}"
        name = self._unique_branch_name(desired)
        base = base or self._guess_default_base()

        # `checkout -b <name> <base>` works for branch or commit
        self._git("checkout", "-b", name, base, check=True)
        log.info(
            "Created and switched to branch %s (base=%s, head=%s)",
            name, base, self.current_commit(),
        )
        return name

    def checkout(self, name: str) -> None:
        """
        Switch to an existing branch.
        """
        self._git("checkout", name, check=True)
        log.info("Checked out branch %s", name)

    # --------------------------------------------------------------------- #
    # Push helpers
    # --------------------------------------------------------------------- #
    def push_current_branch(self, *, remote: str = "origin", set_upstream: bool = True) -> None:
        """
        Push the current branch to *remote*, setting upstream on first push.

        If the remote does not exist, we log a warning and return silently
        (the orchestrator may run in local‑only mode during development).

        Raises
        ------
        RuntimeError
            On push failure when a remote is present.
        """
        if not self.has_remote(remote):
            log.warning("No git remote named %r; skipping push.", remote)
            return

        branch = self.current_branch()
        if branch == "HEAD":
            log.warning("Detached HEAD; skipping push.")
            return

        args = ["push", remote, f"HEAD:{branch}"]
        if set_upstream:
            # --set-upstream only on first push; harmless if already set.
            args.insert(1, "--set-upstream")

        self._git(*args, check=True)
        log.info("Pushed branch %s to %s", branch, remote)
