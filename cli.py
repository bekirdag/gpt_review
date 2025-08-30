#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
===============================================================================
GPT‑Review ▸ Command Line Interface
===============================================================================

Subcommands
-----------
• iterate     – run the multi‑iteration orchestrator (full plan‑first workflow)
• api         – run the tool‑driven API loop (no browser)
• scan        – print a compact repository manifest
• validate    – validate a single patch JSON against the bundled schema
• schema      – print the active JSON schema
• version     – print package version

Global flags
------------
• --version   – print package version (equivalent to the `version` subcommand)

Examples
--------
  # 1) Full orchestrator (three iterations, plan‑first, error‑fix loop)
  gpt-review iterate ./instructions.txt /path/to/repo --run "pytest -q"

  # 2) API driver with a run command after each patch
  gpt-review api ./instructions.txt /path/to/repo --cmd "pytest -q"

  # 3) Validate a patch payload
  gpt-review validate --payload '{"op":"create","file":"a.py","body":"x","status":"in_progress"}'

  # 4) Print schema / scan repo
  gpt-review schema
  gpt-review scan /path/to/repo

  # 5) Top-level version flag (used by software_review.sh)
  gpt-review --version
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from importlib import resources
from pathlib import Path
from typing import Optional

from gpt_review import get_logger, get_version

# Orchestrator (compatibility path that mirrors the new orchestrator)
from gpt_review.workflow import OrchestratorConfig, ReviewWorkflow

# API driver (strict tool calls, optional run command)
from gpt_review.api_driver import run as api_run

# Patch validator (JSON‑Schema + extra guards)
from patch_validator import validate_patch

log = get_logger(__name__)

# Environment‑backed defaults (kept in sync with modules)
DEFAULT_MODEL = os.getenv("GPT_REVIEW_MODEL", "gpt-5-pro")
DEFAULT_API_TIMEOUT = int(os.getenv("GPT_REVIEW_API_TIMEOUT", "120"))
DEFAULT_ITERATIONS = int(os.getenv("GPT_REVIEW_ITERATIONS", "3"))
DEFAULT_BRANCH_PREFIX = os.getenv("GPT_REVIEW_BRANCH_PREFIX", "iteration")
DEFAULT_REMOTE = os.getenv("GPT_REVIEW_REMOTE", "origin")


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

_GIT_URL_RE = re.compile(r"^(?:https?://|git@|ssh://).*|.*\.git$")


def _looks_like_git_url(arg: str) -> bool:
    try:
        return bool(_GIT_URL_RE.match(arg.strip()))
    except Exception:
        return False


def _clone_repo_to_temp(url: str) -> Path:
    tmpdir = Path(tempfile.mkdtemp(prefix="gpt-review-cli-"))
    log.info("Cloning repo %s → %s", url, tmpdir)
    subprocess.run(["git", "clone", "--depth", "1", url, str(tmpdir)], check=True)
    return tmpdir


def _resolve_repo(path_or_url: str) -> Path:
    """
    Accept either a local git repository path or a Git URL.
    Returns the local repository path (cloned into a temp dir when URL).
    """
    cand = Path(path_or_url).expanduser()
    if cand.exists() and (cand / ".git").exists():
        return cand.resolve()

    if _looks_like_git_url(path_or_url):
        try:
            return _clone_repo_to_temp(path_or_url)
        except Exception as exc:
            raise SystemExit(f"Failed to clone repository: {exc}") from exc

    raise SystemExit(f"Not a git repository or URL: {path_or_url}")


def _read_instructions(p: str) -> Path:
    path = Path(p).expanduser()
    if not path.exists():
        raise SystemExit(f"Instructions file not found: {path}")
    return path.resolve()


# ─────────────────────────────────────────────────────────────────────────────
# Subcommand handlers
# ─────────────────────────────────────────────────────────────────────────────

def cmd_iterate(args: argparse.Namespace) -> int:
    """
    Run the multi‑iteration orchestrator on the repository.
    """
    repo = _resolve_repo(args.repo)
    instructions = _read_instructions(args.instructions)

    cfg = OrchestratorConfig(
        instructions_path=instructions,
        repo=repo,
        model=args.model or DEFAULT_MODEL,
        api_timeout=args.api_timeout,
        iterations=args.iterations,
        branch_prefix=args.branch_prefix or DEFAULT_BRANCH_PREFIX,
        remote=args.remote or DEFAULT_REMOTE,
        push_at_end=not args.no_push,
        run_cmd=args.run,
        # 'ignores' accepted for back‑compat by OrchestratorConfig (defaulted there)
    )
    ReviewWorkflow(cfg).run()
    return 0


def cmd_api(args: argparse.Namespace) -> int:
    """
    Run the tool‑driven API loop (no browser) until status='completed'
    and (if provided) the command passes.
    """
    repo = _resolve_repo(args.repo)
    instructions = _read_instructions(args.instructions)
    api_run(
        instructions_path=instructions,
        repo=repo,
        cmd=args.cmd,
        auto=True,
        timeout=args.timeout,
        model=args.model or DEFAULT_MODEL,
        api_timeout=args.api_timeout,
        client=None,
    )
    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    """
    Validate a single patch JSON payload.
    """
    payload: Optional[str] = None
    if args.file:
        try:
            payload = Path(args.file).read_text(encoding="utf-8")
        except Exception as exc:
            log.error("Failed to read %s: %s", args.file, exc)
            return 1
    elif args.payload == "-":
        payload = sys.stdin.read()
    else:
        payload = args.payload

    if not payload:
        log.error("Missing payload. Provide --payload <json> or --payload - (stdin) or --file <path>.")
        return 1

    try:
        validate_patch(payload)
        print("✓ Patch is valid.")
        return 0
    except Exception as exc:
        # Let patch_validator craft precise messages; keep a simple summary here.
        log.error("Patch invalid: %s", exc)
        return 1


def cmd_schema(_args: argparse.Namespace) -> int:
    """
    Print the active JSON schema bundled with the package.
    """
    try:
        with resources.files("gpt_review").joinpath("schema.json").open(encoding="utf-8") as fh:
            data = json.load(fh)
        print(json.dumps(data, indent=2, ensure_ascii=False))
        return 0
    except Exception as exc:
        log.error("Failed to load bundled schema: %s", exc)
        return 1


def cmd_scan(args: argparse.Namespace) -> int:
    """
    Print a compact, deterministic manifest of repository files.
    """
    from gpt_review.file_scanner import scan_repository  # lazy import

    repo = _resolve_repo(args.repo)
    scan = scan_repository(repo, ignores=())  # ignores accepted but unused by facade
    print(scan.manifest_text(max_lines=args.max_lines))
    return 0


def cmd_version(_args: argparse.Namespace) -> int:
    print(get_version())
    return 0


# ─────────────────────────────────────────────────────────────────────────────
# Argument parser
# ─────────────────────────────────────────────────────────────────────────────

def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="gpt-review",
        description="GPT‑Review – multi‑iteration code review CLI",
    )

    # Global flags (keep subparsers optional so --version can succeed)
    p.add_argument(
        "--version",
        action="store_true",
        help="Print package version and exit.",
    )

    sub = p.add_subparsers(dest="cmd", metavar="command")

    # iterate
    pi = sub.add_parser("iterate", help="Run the multi‑iteration orchestrator (plan‑first + 3 iterations)")
    pi.add_argument("instructions", help="Path to a plain‑text instructions file.")
    pi.add_argument("repo", help="Path to a git repository OR a Git URL to clone.")
    pi.add_argument("--model", default=DEFAULT_MODEL, help=f"Model id (default: {DEFAULT_MODEL})")
    pi.add_argument("--api-timeout", type=int, default=DEFAULT_API_TIMEOUT, help="HTTP timeout (seconds).")
    pi.add_argument("--iterations", type=int, default=DEFAULT_ITERATIONS, choices=(1, 2, 3), help="Number of iterations (1..3).")
    pi.add_argument("--branch-prefix", default=DEFAULT_BRANCH_PREFIX, help=f"Branch prefix (default: {DEFAULT_BRANCH_PREFIX})")
    pi.add_argument("--remote", default=DEFAULT_REMOTE, help=f"Git remote to push (default: {DEFAULT_REMOTE})")
    pi.add_argument("--no-push", action="store_true", help="Do not push the final branch upon completion.")
    pi.add_argument("--run", help="Command to run during error‑fix loop (e.g., 'pytest -q').")
    pi.set_defaults(func=cmd_iterate)

    # api
    pa = sub.add_parser("api", help="Run the tool‑driven API loop (no browser)")
    pa.add_argument("instructions", help="Path to a plain‑text instructions file.")
    pa.add_argument("repo", help="Path to a git repository OR a Git URL to clone.")
    pa.add_argument("--cmd", help="Command to run after each successful patch (e.g., 'pytest -q').")
    pa.add_argument("--timeout", type=int, default=300, help="Timeout for --cmd (seconds).")
    pa.add_argument("--model", default=DEFAULT_MODEL, help=f"Model id (default: {DEFAULT_MODEL})")
    pa.add_argument("--api-timeout", type=int, default=DEFAULT_API_TIMEOUT, help="HTTP timeout (seconds).")
    pa.set_defaults(func=cmd_api)

    # validate
    pv = sub.add_parser("validate", help="Validate a single patch JSON against the bundled schema")
    src = pv.add_mutually_exclusive_group(required=True)
    src.add_argument("--payload", help="JSON string payload, or '-' to read from stdin.")
    src.add_argument("--file", help="Read JSON payload from a file path.")
    pv.set_defaults(func=cmd_validate)

    # schema
    ps = sub.add_parser("schema", help="Print the active JSON schema")
    ps.set_defaults(func=cmd_schema)

    # scan
    psn = sub.add_parser("scan", help="Print a compact repository manifest")
    psn.add_argument("repo", help="Path to a git repository OR a Git URL to clone.")
    psn.add_argument("--max-lines", type=int, default=400, help="Max lines in the printed manifest (default: 400).")
    psn.set_defaults(func=cmd_scan)

    # version (subcommand, kept for parity)
    pvrs = sub.add_parser("version", help="Print package version")
    pvrs.set_defaults(func=cmd_version)

    return p


def main(argv: Optional[list[str]] = None) -> int:
    try:
        parser = _parser()
        args = parser.parse_args(argv)

        # Handle global --version early
        if getattr(args, "version", False):
            print(get_version())
            return 0

        # Enforce that a subcommand was provided when not using --version
        if not hasattr(args, "func"):
            parser.print_help()
            return 2

        return int(args.func(args))  # type: ignore[misc]
    except KeyboardInterrupt:
        log.info("Interrupted by user (Ctrl‑C).")
        return 130
    except SystemExit as exc:
        # Propagate explicit SystemExit codes cleanly
        return int(exc.code) if isinstance(exc.code, int) else 1
    except Exception as exc:
        log.exception("Fatal error in CLI: %s", exc)
        return 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
