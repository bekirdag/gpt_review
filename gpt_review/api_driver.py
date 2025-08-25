#!/usr/bin/env python3
"""
===============================================================================
GPT‑Review ▸ API Driver (no browser)
===============================================================================

Purpose
-------
Run the review loop via an OpenAI‑compatible HTTPS API instead of a browser.
The loop mirrors the browser driver:

  1) Send initial prompt (instructions + session rules).
  2) Receive a **single JSON patch** per assistant reply.
  3) Apply it to the Git repo (via apply_patch.py) and commit.
  4) Optionally run a shell command (tests, linter, build, …).
  5) If it fails, send back the (truncated) failing logs.
  6) Repeat until the command passes **and** status == "completed".

Design notes
------------
* No third‑party deps: uses urllib to POST to /v1/chat/completions.
* Token‑aware:
    - Keeps only the most recent N conversational turns (env: GPT_REVIEW_CTX_TURNS).
    - Truncates long logs to the last K characters (env: GPT_REVIEW_LOG_TAIL_CHARS).
* Robust patch extraction (accepts fenced JSON; balanced‑brace scanning).
* Reuses existing apply tool and validator to preserve behaviour & tests.

Environment
-----------
OPENAI_API_KEY            – required (Bearer token)
OPENAI_BASE_URL           – e.g. https://api.openai.com/v1  (falls back to this)
OPENAI_API_BASE           – legacy alias; used if OPENAI_BASE_URL not set
GPT_REVIEW_MODEL          – default: "gpt-5-pro"
GPT_REVIEW_API_TIMEOUT    – request timeout in seconds (default: 120)
GPT_REVIEW_CTX_TURNS      – how many *rounds* to keep in context (default: 6)
GPT_REVIEW_LOG_TAIL_CHARS – tail size for logs sent back (default: 20000)

CLI delegation
--------------
review.py will parse --mode api and call:

    api_driver.run(
        instructions_path, repo_path,
        cmd=args.cmd, auto=args.auto, timeout=args.timeout,
        model=args.model, api_timeout=args.api_timeout
    )

===============================================================================
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import textwrap
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from patch_validator import validate_patch
from logger import get_logger

# ─────────────────────────────────────────────────────────────────────────────
# Configuration (env with sane defaults)
# ─────────────────────────────────────────────────────────────────────────────
DEFAULT_MODEL = os.getenv("GPT_REVIEW_MODEL", "gpt-5-pro")
DEFAULT_API_TIMEOUT = int(os.getenv("GPT_REVIEW_API_TIMEOUT", "120"))
DEFAULT_CTX_TURNS = int(os.getenv("GPT_REVIEW_CTX_TURNS", "6"))
DEFAULT_LOG_TAIL = int(os.getenv("GPT_REVIEW_LOG_TAIL_CHARS", "20000"))

API_BASE = (
    os.getenv("OPENAI_BASE_URL")
    or os.getenv("OPENAI_API_BASE")
    or "https://api.openai.com/v1"
).rstrip("/")

NUDGE_RETRIES = int(os.getenv("GPT_REVIEW_NUDGE_RETRIES", "2"))
PROBE_RETRIES = int(os.getenv("GPT_REVIEW_PROBE_RETRIES", "1"))

# Re‑use the same textual helpers as the browser driver (kept inline here to
# avoid importing review.py, which would import Selenium on import time).
EXTRA_RULES = (
    "Your fixes must be **chunk by chunk**. "
    "Provide a fix for **one script only** in each answer. "
    "Ask me to **continue** when you are done with one script."
)
RAW_JSON_REMINDER = (
    "Format reminder: return exactly **one** JSON object — raw JSON only, "
    "no prose, no markdown, no code fences. "
    "Keys: op, file, (body|body_b64), target, mode, status. "
    'Use status="in_progress" until the last patch, then "completed".'
)
PROBE_FILE_MAGIC = "__gpt_review_probe__"
PROBE_BODY_EXPECTED = "ok"

log = get_logger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Chat protocol (OpenAI‑compatible)
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class ChatConfig:
    model: str = DEFAULT_MODEL
    api_base: str = API_BASE
    api_key: Optional[str] = os.getenv("OPENAI_API_KEY")
    timeout: int = DEFAULT_API_TIMEOUT
    ctx_turns: int = DEFAULT_CTX_TURNS  # keep last N QA rounds (+ system)
    log_tail: int = DEFAULT_LOG_TAIL


def _endpoint(base: str) -> str:
    # Use /v1/chat/completions for broad compatibility
    return f"{base.rstrip('/')}/chat/completions"


def _post_json(url: str, payload: dict, headers: dict, timeout: int) -> dict:
    body = json.dumps(payload).encode("utf-8")
    req = Request(url=url, data=body, headers=headers, method="POST")
    try:
        with urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            return json.loads(raw)
    except HTTPError as exc:
        err_body = exc.read().decode("utf-8", errors="replace")
        log.error("HTTP %s from API: %s", exc.code, err_body[:5000])
        raise
    except URLError as exc:
        log.error("Network error talking to API: %s", exc)
        raise


def _chat_once(cfg: ChatConfig, messages: List[Dict[str, str]]) -> str:
    """
    Send a single non‑streaming chat request and return the assistant content.
    """
    if not cfg.api_key:
        raise RuntimeError(
            "OPENAI_API_KEY is not set. Export it or place it in your dotenv."
        )

    url = _endpoint(cfg.api_base)
    headers = {
        "Authorization": f"Bearer {cfg.api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": cfg.model,
        "messages": messages,
        "temperature": 0,
    }
    data = _post_json(url, payload, headers, timeout=cfg.timeout)

    try:
        content = (data["choices"][0]["message"]["content"] or "").strip()
    except Exception:
        log.debug("Raw API response: %s", json.dumps(data, indent=2)[:2000])
        raise RuntimeError("Unexpected API response format (missing message.content)")
    return content


# ─────────────────────────────────────────────────────────────────────────────
# Patch extraction – tolerant to fences and braces in strings
# ─────────────────────────────────────────────────────────────────────────────
_FENCE_RE = re.compile(r"```(?:jsonc?|text)?\s*(.*?)\s*```", re.S)


def _strip_fence(text: str) -> str:
    m = _FENCE_RE.search(text)
    return m.group(1) if m else text


def _balanced_json(text: str) -> Optional[str]:
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    in_str = esc = False
    for i, ch in enumerate(text[start:], start):
        if esc:
            esc = False
            continue
        if ch == "\\":
            esc = True
            continue
        if ch == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None


def _extract_patch(raw: str) -> Optional[dict]:
    blob = _balanced_json(_strip_fence(raw))
    if not blob:
        return None
    try:
        return validate_patch(blob)
    except Exception as exc:
        log.error("Patch validation failed: %s", exc)
        return None


def _is_probe_patch(patch: dict) -> bool:
    try:
        return (
            patch.get("op") == "create"
            and patch.get("file") == PROBE_FILE_MAGIC
            and patch.get("body") == PROBE_BODY_EXPECTED
            and patch.get("status") in {"in_progress", "completed"}
        )
    except Exception:
        return False


# ─────────────────────────────────────────────────────────────────────────────
# Utilities shared with the browser driver (duplicated to avoid Selenium import)
# ─────────────────────────────────────────────────────────────────────────────
def _run_cmd(cmd: str, repo: Path, timeout: int) -> Tuple[bool, str, int]:
    try:
        res = subprocess.run(
            cmd, cwd=repo, shell=True, capture_output=True, text=True, timeout=timeout
        )
        out = (res.stdout or "") + (res.stderr or "")
        return res.returncode == 0, out, res.returncode
    except subprocess.TimeoutExpired as exc:
        out = (exc.stdout or "") + (exc.stderr or "")
        banner = f"TIMEOUT: command exceeded {timeout}s\n"
        return False, banner + out, 124


def _now_iso_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _current_commit(repo: Path) -> str:
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


def _initial_prompt(instr_text: str) -> str:
    schema_blurb = (
        '{ "op": "create|update|delete|rename|chmod", '
        '"file": "...", "status": "in_progress|completed", ... }'
    )
    return (
        f"{instr_text}\n\n"
        "Work **one file at a time**.\n\n"
        f"{EXTRA_RULES}\n\n"
        f"{RAW_JSON_REMINDER}\n"
        "Return *exactly one* JSON object (no extra text). Example shape only:\n"
        f"{schema_blurb}\n\n"
        "Use status = in_progress while patches remain; "
        "status = completed when done."
    )


def _nudge_resend_raw_json() -> str:
    return (
        "I could not detect a valid patch. "
        "Please **resend** the patch as a single **raw JSON** object only — "
        "no prose, no markdown, no code fences. "
        "Remember the keys: op, file, (body|body_b64), target, mode, status. "
        'Use status="in_progress" until the final patch, then "completed".'
    )


def _probe_prompt() -> str:
    return (
        "Capability probe: reply with this **exact JSON object only** "
        "(raw JSON, no markdown, no prose):\n"
        '{"op":"create","file":"__gpt_review_probe__","body":"ok","status":"in_progress"}'
    )


def _chunk_tail(text: str, tail: int) -> str:
    """Return the last *tail* characters of *text*, prefixed with ⟪…⟫ when truncated."""
    if len(text) <= tail:
        return text
    return f"⟪…⟫{text[-tail:]}"


# ─────────────────────────────────────────────────────────────────────────────
# Conversation state & helpers
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class Conv:
    """Minimal rolling context to keep tokens down."""

    system: str
    history: List[Dict[str, str]]
    cfg: ChatConfig

    def __init__(self, system: str, cfg: ChatConfig):
        self.system = system
        self.history = []
        self.cfg = cfg

    def _trimmed(self) -> List[Dict[str, str]]:
        """
        Build a message list: system + last N *pairs* (user/assistant).
        """
        # Keep last 2 * ctx_turns messages (user+assistant pairs), plus system.
        trimmed = self.history[-(2 * self.cfg.ctx_turns) :]
        return [{"role": "system", "content": self.system}, *trimmed]

    def user(self, content: str) -> str:
        """
        Append a user message, call the API, append the assistant reply,
        and return the assistant content.
        """
        self.history.append({"role": "user", "content": content})
        reply = _chat_once(self.cfg, self._trimmed())
        self.history.append({"role": "assistant", "content": reply})
        return reply


# ─────────────────────────────────────────────────────────────────────────────
# API Driver main loop
# ─────────────────────────────────────────────────────────────────────────────
def run(
    instructions_path: Path,
    repo: Path,
    *,
    cmd: Optional[str],
    auto: bool,
    timeout: int,
    model: Optional[str] = None,
    api_timeout: Optional[int] = None,
) -> None:
    """
    Execute the review loop in API mode.

    Parameters
    ----------
    instructions_path : Path
        Path to the plain‑text instructions file.
    repo : Path
        Path to a local Git repository (must already exist).
    cmd : Optional[str]
        Shell command to run after each patch (tests, linters, …).
    auto : bool
        If True, automatically send 'continue' after each successful patch unless status=completed.
    timeout : int
        Timeout for *cmd* execution (seconds).
    model : Optional[str]
        Model to request from the API (defaults to env/constant).
    api_timeout : Optional[int]
        HTTP request timeout (seconds).
    """
    cfg = ChatConfig(
        model=model or DEFAULT_MODEL,
        api_base=API_BASE,
        api_key=os.getenv("OPENAI_API_KEY"),
        timeout=api_timeout or DEFAULT_API_TIMEOUT,
        ctx_turns=DEFAULT_CTX_TURNS,
        log_tail=DEFAULT_LOG_TAIL,
    )

    if not cfg.api_key:
        raise SystemExit(
            "❌ OPENAI_API_KEY is not set. Export it or add it to your .env and re‑run."
        )

    instr_text = Path(instructions_path).read_text(encoding="utf-8").strip()
    system = (
        "You are GPT‑Review, a meticulous coding assistant. "
        "Always return exactly one JSON patch per reply (raw JSON only)."
    )
    conv = Conv(system=_initial_prompt(instr_text), cfg=cfg)

    session_id = uuid.uuid4().hex[:12]
    log.info("API session id: %s | model=%s | base=%s", session_id, cfg.model, cfg.api_base)

    # Kick off the conversation with the initial prompt
    try:
        reply = conv.user("Begin.")
    except Exception as exc:
        log.exception("Initial API request failed: %s", exc)
        raise SystemExit(1)

    nudge_budget = NUDGE_RETRIES
    probe_budget = PROBE_RETRIES

    while True:
        patch = _extract_patch(reply)

        if not patch:
            # Nudge (bounded retries)
            if nudge_budget > 0:
                attempt = (NUDGE_RETRIES - nudge_budget) + 1
                log.warning(
                    "No valid JSON patch found; requesting raw‑JSON resend (attempt %d/%d).",
                    attempt,
                    NUDGE_RETRIES,
                )
                try:
                    reply = conv.user(_nudge_resend_raw_json())
                except Exception as exc:
                    log.exception("API request failed during nudge: %s", exc)
                    raise SystemExit(1)
                nudge_budget -= 1
                continue

            # Probe (bounded)
            if probe_budget > 0:
                probe_attempt = (PROBE_RETRIES - probe_budget) + 1
                log.warning(
                    "Raw‑JSON nudges exhausted. Sending capability probe (attempt %d/%d).",
                    probe_attempt,
                    PROBE_RETRIES,
                )
                try:
                    reply = conv.user(_probe_prompt())
                except Exception as exc:
                    log.exception("API request failed during probe: %s", exc)
                    raise SystemExit(1)

                probe_patch = _extract_patch(reply)
                if probe_patch is None:
                    log.error("Probe response still not a valid JSON object.")
                    probe_budget -= 1
                    if probe_budget > 0:
                        try:
                            reply = conv.user(_nudge_resend_raw_json())
                        except Exception as exc:
                            log.exception("API request failed after probe: %s", exc)
                            raise SystemExit(1)
                    continue

                if _is_probe_patch(probe_patch):
                    log.info("Capability probe succeeded (marker patch received).")
                    # Reset nudge budget and ask for the real patch.
                    nudge_budget = NUDGE_RETRIES
                    try:
                        reply = conv.user(
                            "Thanks. Now please **resend the actual patch** as a single "
                            "**raw JSON** object only (no prose, no fences)."
                        )
                    except Exception as exc:
                        log.exception("API request failed after probe ack: %s", exc)
                        raise SystemExit(1)
                    continue

                log.info("Probe returned a valid non‑marker patch; proceeding.")
                patch = probe_patch
            else:
                log.error("Exceeded capability probe attempts. Stopping.")
                break

        # At this point we have a (validated) patch.
        nudge_budget = NUDGE_RETRIES

        if _is_probe_patch(patch):
            log.info("Ignoring marker probe patch (no apply). Requesting real patch.")
            try:
                reply = conv.user(_nudge_resend_raw_json())
            except Exception as exc:
                log.exception("API request failed while ignoring marker: %s", exc)
                raise SystemExit(1)
            continue

        # Apply patch via apply_patch.py (stdin to avoid argv length limits)
        try:
            proc = subprocess.run(
                [sys.executable, str(Path(__file__).resolve().parent / "apply_patch.py"), "-", str(repo)],
                input=json.dumps(patch),
                capture_output=True,
                text=True,
            )
        except Exception as exc:
            log.exception("Patch apply raised: %s", exc)
            _send_apply_error_api(conv, session_id, repo, patch, "", str(exc), 1, cfg)
            reply = conv.history[-1]["content"] if conv.history else ""
            continue

        if proc.returncode != 0:
            log.warning("Patch apply failed with code %s", proc.returncode)
            _send_apply_error_api(
                conv,
                session_id,
                repo,
                patch,
                proc.stdout or "",
                proc.stderr or "",
                proc.returncode,
                cfg,
            )
            reply = conv.history[-1]["content"] if conv.history else ""
            continue

        # Run optional command (tests)
        if cmd:
            ok, output, code = _run_cmd(cmd, repo, timeout)
            if not ok:
                _send_error_chunks_api(conv, session_id, repo, cmd, code, output, cfg)
                reply = conv.history[-1]["content"] if conv.history else ""
                continue

        # Success path for this patch: check status
        if patch.get("status") == "completed":
            log.info("🎉 All done – tests pass and status=completed.")
            break

        # Next chunk: auto or manual
        if auto:
            try:
                reply = conv.user("continue")
            except Exception as exc:
                log.exception("API request failed while continuing: %s", exc)
                raise SystemExit(1)
        else:
            input("Press <Enter> for next patch …")
            try:
                reply = conv.user("continue")
            except Exception as exc:
                log.exception("API request failed while continuing: %s", exc)
                raise SystemExit(1)

    # End loop
    log.info("API loop finished.")


# ─────────────────────────────────────────────────────────────────────────────
# Error reporting to API (token‑aware)
# ─────────────────────────────────────────────────────────────────────────────
def _send_error_chunks_api(
    conv: Conv,
    session_id: str,
    repo: Path,
    cmd: str,
    exit_code: int,
    output: str,
    cfg: ChatConfig,
) -> None:
    """
    Send a concise, tagged error report back to the assistant.
    We keep a *single* message (not multiple slices) but truncate to tail.
    """
    commit = _current_commit(repo)
    ts = _now_iso_utc()
    body = _chunk_tail(output, cfg.log_tail)

    message = textwrap.dedent(
        f"""\
        [gpt-review#{session_id}] — The command failed. Please propose the next fix.
        meta:
          commit    : {commit}
          command   : {cmd}
          exit_code : {exit_code}
          timestamp : {ts}

        Only return the next **single** JSON patch (raw JSON only, no prose).
        Here is the tail of the failing output:

        ```text
        {body}
        ```
        """
    ).strip()

    try:
        conv.user(message)
    except Exception as exc:
        log.exception("Failed to send error chunk to API: %s", exc)
        raise SystemExit(1)


def _send_apply_error_api(
    conv: Conv,
    session_id: str,
    repo: Path,
    patch: dict,
    stdout: str,
    stderr: str,
    exit_code: int,
    cfg: ChatConfig,
) -> None:
    try:
        patch_json = json.dumps(patch, indent=2, ensure_ascii=False)
    except Exception:
        patch_json = str(patch)

    report = textwrap.dedent(
        f"""\
        Patch apply failed. Please send a corrected patch (raw JSON only).

        Patch JSON (as attempted):
        ```json
        {patch_json}
        ```

        apply_patch.py output (tail):
        ```text
        {_chunk_tail((stdout or '') + (stderr or ''), cfg.log_tail)}
        ```
        """
    ).strip()

    # Reuse the same structured error sender for consistency
    _send_error_chunks_api(
        conv,
        session_id=session_id,
        repo=repo,
        cmd="apply_patch",
        exit_code=exit_code,
        output=report,
        cfg=cfg,
    )
