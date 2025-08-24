#!/usr/bin/env python3
"""
===============================================================================
GPT‑Review ▸ Main Driver
===============================================================================

Automates an **edit → run → fix** conversation between you and ChatGPT.

Flow
----
1. Present *instructions* to ChatGPT.
2. Receive **one JSON patch** per reply (see README for schema).
3. Apply the patch to a Git repository & commit.
4. Optionally run *any shell command* (tests, linter, build, …).
5. If the command fails, send the full log back to ChatGPT.
6. Repeat until the command passes **and** "status": "completed".

Session rule
------------
*ChatGPT must:*
* deliver **one script per answer** (chunk‑by‑chunk),
* explicitly ask the user to **continue** before proceeding.

These constraints ensure deterministic patching and a clean Git history.

High‑impact robustness
----------------------
• **Driver provisioning order (most reliable first)**:
  1) Respect **CHROMEDRIVER** env (absolute path to chromedriver).
  2) Try **Selenium Manager** (built into Selenium 4.6+).
  3) Fall back to **webdriver‑manager** (works online).
  This avoids fragile imports like `ChromeType` whose locations change between
  webdriver‑manager releases.

• **Composer detection/clearing**:
  - Prefer `<textarea>`; fall back to `div[contenteditable="true"]`.
  - Always clear drafts safely (Select‑All + Backspace) before sending.

• **Apply‑failure reporting (non‑fatal)**:
  - If `apply_patch.py` fails, send a concise report (includes the **patch JSON**
    and the tool’s **stdout/stderr**) and continue the loop.

• **Patch delivery to apply tool via STDIN**:
  - Use `"-"` argument and `input=...` to avoid OS argv length limits.

• **Safe commit lookup**:
  - `_current_commit()` returns `"<no-commits-yet>"` on fresh repos instead
    of crashing, so error reporting works before the first commit.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import textwrap
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Tuple

from selenium import webdriver
from selenium.common.exceptions import WebDriverException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC  # noqa: F401
from selenium.webdriver.support.ui import WebDriverWait  # noqa: F401

# webdriver-manager is optional; import guarded and only used as a fallback.
try:  # pragma: no cover - availability differs by env
    from webdriver_manager.chrome import ChromeDriverManager  # type: ignore
    _WDM_AVAILABLE = True
except Exception:  # pragma: no cover
    ChromeDriverManager = None  # type: ignore
    _WDM_AVAILABLE = False

from logger import get_logger
from patch_validator import validate_patch

# ════════════════════════════════════════════════════════════════════════════
# Tunables & constants (env‑overridable)
# ════════════════════════════════════════════════════════════════════════════
CHAT_URL: str = os.getenv("GPT_REVIEW_CHAT_URL", "https://chatgpt.com/")
CHAT_URL_FALLBACK: str = "https://chat.openai.com/"

WAIT_UI: int = int(os.getenv("GPT_REVIEW_WAIT_UI", "90"))
IDLE_SECS: float = float(os.getenv("GPT_REVIEW_STREAM_IDLE_SECS", "2"))
CHUNK_SIZE: int = int(os.getenv("GPT_REVIEW_CHUNK_SIZE", "15000"))
DEFAULT_TIMEOUT: int = int(os.getenv("GPT_REVIEW_COMMAND_TIMEOUT", "300"))
RETRIES: int = int(os.getenv("GPT_REVIEW_RETRIES", "3"))
NUDGE_RETRIES: int = int(os.getenv("GPT_REVIEW_NUDGE_RETRIES", "2"))
PROBE_RETRIES: int = int(os.getenv("GPT_REVIEW_PROBE_RETRIES", "1"))
STATE_FILE: str = ".gpt-review-state.json"

EXTRA_RULES: str = (
    "Your fixes must be **chunk by chunk**. "
    "Provide a fix for **one script only** in each answer. "
    "Ask me to **continue** when you are done with one script."
)

RAW_JSON_REMINDER: str = (
    "Format reminder: return exactly **one** JSON object — raw JSON only, "
    "no prose, no markdown, no code fences. "
    "Keys: op, file, (body|body_b64), target, mode, status. "
    'Use status=\"in_progress\" until the last patch, then \"completed\".'
)

PROBE_FILE_MAGIC = "__gpt_review_probe__"
PROBE_BODY_EXPECTED = "ok"

ROOT = Path(__file__).resolve().parent
log = get_logger(__name__)

# ════════════════════════════════════════════════════════════════════════════
# State helpers – crash‑safe resume
# ════════════════════════════════════════════════════════════════════════════
def _state_path(repo: Path) -> Path:
    """Return path of the persistent state‑file inside *repo*."""
    return repo / STATE_FILE


def _load_state(repo: Path) -> Optional[dict]:
    """Load state‑file if present, else *None*."""
    try:
        return json.loads(_state_path(repo).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None


def _current_commit(repo: Path) -> str:
    """
    Return HEAD SHA for *repo*.

    Robust: returns the literal string "<no-commits-yet>" when the repository
    has no commits (so callers like error reporters don't crash).
    """
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


def _save_state(repo: Path, url: str) -> None:
    """Write resume metadata to disk."""
    data = {
        "conversation_url": url,
        "last_commit": _current_commit(repo),
        "timestamp": int(time.time()),
    }
    _state_path(repo).write_text(json.dumps(data, indent=2), encoding="utf-8")
    log.debug("State saved: %s", data)


def _clear_state(repo: Path) -> None:
    """Delete state‑file if it exists."""
    try:
        _state_path(repo).unlink()
    except FileNotFoundError:
        pass


def _save_state_quiet(repo: Path, url: str) -> None:
    """
    Best‑effort wrapper around `_save_state`.

    Used at **pre‑prompt** and **pre‑patch** moments where we want resilience
    but must not abort if the filesystem is momentarily unavailable.
    """
    try:
        _save_state(repo, url)
    except Exception as exc:  # pragma: no cover
        log.warning("Non‑fatal: failed to persist state (%s): %s", url, exc)


# ════════════════════════════════════════════════════════════════════════════
# Browser detection & Selenium helpers
# ════════════════════════════════════════════════════════════════════════════
def _detect_browser_binary() -> Optional[str]:
    """
    Try to resolve a Chrome/Chromium binary.

    Priority:
    1) Explicit CHROME_BIN (if executable)
    2) Common PATH names on Linux
    3) macOS app bundles
    """
    env_bin = os.getenv("CHROME_BIN", "")
    if env_bin and os.path.exists(env_bin) and os.access(env_bin, os.X_OK):
        return env_bin

    for name in ("google-chrome-stable", "google-chrome", "chromium", "chromium-browser", "chrome"):
        p = shutil.which(name)
        if p:
            return p

    if sys.platform == "darwin":
        for p in (
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Chromium.app/Contents/MacOS/Chromium",
        ):
            if os.path.exists(p) and os.access(p, os.X_OK):
                return p
    return None


def _log_driver_versions(drv) -> None:
    """Best‑effort capability logging (version numbers)."""
    try:
        caps = getattr(drv, "capabilities", {}) or {}
        browser_version = caps.get("browserVersion", "unknown")
        chrome_block = caps.get("chrome", {}) if isinstance(caps, dict) else {}
        driver_version = (chrome_block.get("chromedriverVersion", "unknown").split(" ") or ["unknown"])[0]
        log.info("Browser launched – version=%s, driver=%s", browser_version, driver_version)
    except Exception:
        log.info("Browser launched.")


def _chrome_driver() -> webdriver.Chrome:
    """
    Launch Chromium/Chrome with a persistent profile (headless‑optional).

    Honors:
    • GPT_REVIEW_PROFILE – persistent user‑data dir (cookies live here)
    • GPT_REVIEW_HEADLESS – any non‑empty value enables headless
    • CHROME_BIN – explicit browser binary location (google‑chrome/chromium)
    • CHROMEDRIVER – explicit chromedriver binary (takes precedence)
    • Provisioning order: CHROMEDRIVER → Selenium Manager → webdriver‑manager
    """
    profile = Path(
        os.getenv("GPT_REVIEW_PROFILE", "~/.cache/gpt-review/chrome")
    ).expanduser()
    profile.parent.mkdir(parents=True, exist_ok=True)

    opts = Options()
    opts.add_argument(f"--user-data-dir={profile}")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--no-sandbox")
    if os.getenv("GPT_REVIEW_HEADLESS"):
        opts.add_argument("--headless=new")

    binary = _detect_browser_binary()
    if binary:
        opts.binary_location = binary
        log.info("Using browser binary: %s", binary)
    else:
        log.info("No explicit browser binary found; relying on Selenium defaults.")

    # 1) Explicit chromedriver from env
    explicit_driver = os.getenv("CHROMEDRIVER")
    if explicit_driver and os.path.exists(explicit_driver) and os.access(explicit_driver, os.X_OK):
        log.info("Using CHROMEDRIVER from env: %s", explicit_driver)
        service = Service(explicit_driver)
        drv = webdriver.Chrome(service=service, options=opts)
        _log_driver_versions(drv)
        return drv

    # 2) Selenium Manager (best default; works with system Chrome/Chromium)
    try:
        drv = webdriver.Chrome(options=opts)
        _log_driver_versions(drv)
        return drv
    except Exception as exc:
        log.warning("Selenium Manager failed to provision driver: %s", exc)

    # 3) Fallback: webdriver‑manager (online only)
    if _WDM_AVAILABLE:
        try:
            service = Service(ChromeDriverManager().install())  # no ChromeType dependency
            drv = webdriver.Chrome(service=service, options=opts)
            _log_driver_versions(drv)
            return drv
        except Exception as exc:  # pragma: no cover
            log.warning("webdriver‑manager failed as fallback: %s", exc)

    # All strategies failed
    raise RuntimeError(
        "Unable to provision a Chrome driver. "
        "Set CHROMEDRIVER to a working chromedriver path, or ensure Selenium "
        "Manager can download drivers (internet access), or install webdriver-manager."
    )


def _retry(action, what: str):
    """Retry *action* up to RETRIES times with exponential back‑off."""
    pause = 2.0
    for attempt in range(1, RETRIES + 1):
        try:
            return action()
        except WebDriverException as exc:  # pragma: no cover
            if attempt == RETRIES:
                raise
            log.warning("%s failed (attempt %d/%d): %s", what, attempt, RETRIES, exc)
            time.sleep(pause)
            pause *= 2.0


# ────────────────────────────────────────────────────────────────────────────
#  Composer detection & interaction
# ────────────────────────────────────────────────────────────────────────────
def _is_interactable(el) -> bool:
    """
    Return True if *el* is visible and enabled, and not aria-hidden.

    ChatGPT sometimes renders hidden/disabled inputs during UI re-mounts.
    """
    try:
        if not el.is_displayed():
            return False
        if hasattr(el, "is_enabled") and not el.is_enabled():
            return False
        aria_hidden = (el.get_attribute("aria-hidden") or "").strip().lower()
        if aria_hidden == "true":
            return False
        if el.get_attribute("disabled"):
            return False
        return True
    except WebDriverException:
        return False


def _find_composer(drv):
    """Return the most recent visible composer element (textarea or contenteditable)."""
    # 1) Known ChatGPT selector
    try:
        els = drv.find_elements(By.CSS_SELECTOR, "textarea[data-testid='composer-textarea']")
    except WebDriverException:
        els = []
    visible = [e for e in els if _is_interactable(e)]
    if visible:
        return visible[-1]

    # 2) Any visible textarea
    try:
        els = drv.find_elements(By.CSS_SELECTOR, "textarea")
    except WebDriverException:
        els = []
    visible = [e for e in els if _is_interactable(e)]
    if visible:
        return visible[-1]

    # 3) Rich editor contenteditable
    try:
        els = drv.find_elements(By.CSS_SELECTOR, "div[contenteditable='true']")
    except WebDriverException:
        els = []
    visible = [e for e in els if _is_interactable(e)]
    if visible:
        return visible[-1]

    return None


def _wait_composer(drv, *, bounded: bool = False, max_wait: int = WAIT_UI) -> None:
    """
    Wait for the composer to be present & interactable.

    bounded=False waits indefinitely (used during initial login).
    bounded=True waits up to *max_wait* seconds then raises TimeoutError.
    """
    start = time.time()
    last_log = 0.0
    while True:
        try:
            el = _find_composer(drv)
            if el:
                return
        except WebDriverException:
            pass

        now = time.time()
        if not bounded and (now - last_log) >= 5.0:
            log.info("Waiting for user to sign in to ChatGPT …")
            last_log = now

        if bounded and (now - start) >= max_wait:
            raise TimeoutError(f"Composer not found within {max_wait}s")

        time.sleep(0.5 if bounded else 5.0)


# Backward‑compatibility aliases (internal use only)
def _find_textarea(drv):  # pragma: no cover
    return _find_composer(drv)


def _wait_textarea(drv, *, bounded: bool = False, max_wait: int = WAIT_UI):  # pragma: no cover
    return _wait_composer(drv, bounded=bounded, max_wait=max_wait)


def _clear_and_send(area, text: str) -> None:
    """Focus, clear any draft robustly, then send *text* + ENTER."""
    try:
        area.click()
    except WebDriverException:
        pass

    try:
        area.clear()
    except Exception:
        pass

    try:
        area.send_keys(Keys.CONTROL, "a")
        area.send_keys(Keys.BACK_SPACE)
        area.send_keys(Keys.COMMAND, "a")
        area.send_keys(Keys.BACK_SPACE)
    except Exception:
        pass

    area.send_keys(text)
    area.send_keys(Keys.ENTER)


def _send_message(drv, text: str) -> None:
    """Send *text* to ChatGPT."""
    def _inner():
        area = _find_composer(drv)
        if not area:
            raise WebDriverException("Composer not found")
        _clear_and_send(area, text)

    _retry(_inner, "send_message")
    log.debug("Sent %d chars", len(text))


def _assistant_block(drv):
    """Return the most recent assistant message element (or *None*)."""
    try:
        blocks = drv.find_elements(
            By.CSS_SELECTOR, "div[data-message-author-role='assistant']"
        )
    except WebDriverException:
        blocks = []
    return blocks[-1] if blocks else None


def _wait_reply(drv) -> str:
    """
    Wait for ChatGPT to finish streaming and return the reply text.
    """
    _wait_composer(drv, bounded=True, max_wait=WAIT_UI)
    start = time.time()
    last_txt, last_change = "", time.time()

    while time.time() - start < WAIT_UI:
        block = _assistant_block(drv)
        if block:
            txt = block.text
            if txt != last_txt:
                last_txt, last_change = txt, time.time()
            if time.time() - last_change > IDLE_SECS:
                return txt
        time.sleep(0.5)

    raise TimeoutError("Assistant reply timeout")


def _navigate_to_chat(drv) -> None:
    """Navigate to ChatGPT, with chat.openai.com fallback."""
    for url in (CHAT_URL, CHAT_URL_FALLBACK):
        try:
            drv.get(url)
            _wait_composer(drv, bounded=False)  # allow user to sign in interactively
            log.info("Chat page loaded: %s", url)
            return
        except Exception as exc:  # pragma: no cover
            log.warning("Navigation to %s failed: %s", url, exc)
    raise RuntimeError("Unable to load ChatGPT UI (both URLs failed)")


# ════════════════════════════════════════════════════════════════════════════
# Patch extraction – code‑fence tolerant, balanced braces
# ════════════════════════════════════════════════════════════════════════════
_FENCE_RE = re.compile(r"```(?:jsonc?|text)?\s*(.*?)\s*```", re.S)


def _strip_fence(text: str) -> str:
    """Return the payload inside the first code fence if present; else *text*."""
    m = _FENCE_RE.search(text)
    return m.group(1) if m else text


def _balanced_json(text: str) -> Optional[str]:
    """Return the first balanced JSON object substring from *text*, or None."""
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
    """Return first valid JSON patch found in *raw* or *None*."""
    blob = _balanced_json(_strip_fence(raw))
    if not blob:
        return None
    try:
        return validate_patch(blob)
    except Exception as exc:
        log.error("Patch validation failed: %s", exc)
        return None


# ════════════════════════════════════════════════════════════════════════════
# Command execution & error‑chunking
# ════════════════════════════════════════════════════════════════════════════
def _run_cmd(cmd: str, repo: Path, timeout: int) -> Tuple[bool, str, int]:
    """
    Execute *cmd* in *repo*; return (success, combined output, exit_code).

    On timeout, returns (False, <output>, 124) and prefixes the output with a
    short TIMEOUT banner for clarity.
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
        out = res.stdout + res.stderr
        return res.returncode == 0, out, res.returncode
    except subprocess.TimeoutExpired as exc:
        out = (exc.stdout or "") + (exc.stderr or "")
        banner = f"TIMEOUT: command exceeded {timeout}s\n"
        return False, banner + out, 124


def _chunk(text: str, size: int = CHUNK_SIZE) -> List[str]:
    """Split *text* into ≤*size* chunks (never empty list)."""
    return [text[i : i + size] for i in range(0, len(text), size)] or [""]


def _now_iso_utc() -> str:
    """Return an ISO‑8601 timestamp in UTC (seconds precision)."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _send_error_chunks(
    drv,
    *,
    session_id: str,
    repo: Path,
    cmd: str,
    exit_code: int,
    output: str,
) -> None:
    """
    Post a failing log back to ChatGPT in **tagged, safe‑sized chunks**.

    Each message is prefixed with `[gpt-review#<session>] (i/N)` so interleaving
    cannot confuse the assistant. The **first** chunk includes a compact metadata
    header (commit SHA, command, exit code, timestamp). Commit lookup is safe on
    fresh repos.
    """
    chunks = _chunk(output)
    N = len(chunks)
    commit = _current_commit(repo)  # safe on empty repos
    ts = _now_iso_utc()

    header = textwrap.dedent(
        f"""\
        [gpt-review#{session_id}] (1/{N}) — The command failed. Please propose the next fix.
        meta:
          commit   : {commit}
          command  : {cmd}
          exit_code: {exit_code}
          timestamp: {ts}

        ```text
        {chunks[0]}
        ```
        """
    )
    _send_message(drv, header)

    for idx, part in enumerate(chunks[1:], start=2):
        _send_message(
            drv,
            f"[gpt-review#{session_id}] ({idx}/{N}) log slice\n```text\n{part}\n```",
        )


def _send_apply_error(
    *,
    drv,
    session_id: str,
    repo: Path,
    patch: dict,
    stdout: str,
    stderr: str,
    exit_code: int,
) -> None:
    """
    Send a concise, tagged report when `apply_patch.py` fails,
    including the attempted patch JSON and tool output.
    """
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

        apply_patch.py output:
        ```text
        {stdout}
        {stderr}
        ```
        """
    ).strip()

    _send_error_chunks(
        drv,
        session_id=session_id,
        repo=repo,
        cmd="apply_patch",
        exit_code=exit_code,
        output=report,
    )


# ════════════════════════════════════════════════════════════════════════════
# Prompt helpers
# ════════════════════════════════════════════════════════════════════════════
def _initial_prompt(instr: str) -> str:
    """
    Build the initial prompt mixing user *instr*, session rules, and a concise
    fence‑free formatting reminder to improve JSON‑only compliance.
    """
    schema_blurb = (
        '{ "op": "create|update|delete|rename|chmod", '
        '"file": "...", "status": "in_progress|completed", ... }'
    )
    return (
        f"{instr}\n\n"
        "Work **one file at a time**.\n\n"
        f"{EXTRA_RULES}\n\n"
        f"{RAW_JSON_REMINDER}\n"
        "Return *exactly one* JSON object (no extra text). Example shape only:\n"
        f"{schema_blurb}\n\n"
        "Use status = in_progress while patches remain; "
        "status = completed when done."
    )


def _nudge_resend_raw_json() -> str:
    """Short recovery message when a valid JSON patch was not detected."""
    return (
        "I could not detect a valid patch. "
        "Please **resend** the patch as a single **raw JSON** object only — "
        "no prose, no markdown, no code fences. "
        "Remember the keys: op, file, (body|body_b64), target, mode, status. "
        'Use status=\"in_progress\" until the final patch, then \"completed\".'
    )


def _probe_prompt() -> str:
    """
    Minimal schema echo – reliably elicits a valid JSON object we can parse.

    We use a **harmless marker patch** so we can detect it and **skip applying**:
    {
      "op": "create",
      "file": "__gpt_review_probe__",
      "body": "ok",
      "status": "in_progress"
    }
    """
    return (
        "Capability probe: reply with this **exact JSON object only** "
        "(raw JSON, no markdown, no prose):\n"
        '{"op":"create","file":"__gpt_review_probe__","body":"ok","status":"in_progress"}'
    )


def _is_probe_patch(patch: dict) -> bool:
    """True if the JSON patch matches our marker probe."""
    try:
        return (
            patch.get("op") == "create"
            and patch.get("file") == PROBE_FILE_MAGIC
            and patch.get("body") == PROBE_BODY_EXPECTED
            and patch.get("status") in {"in_progress", "completed"}
        )
    except Exception:
        return False


# ════════════════════════════════════════════════════════════════════════════
# CLI parsing
# ════════════════════════════════════════════════════════════════════════════
def _cli_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="gpt-review")
    p.add_argument("instructions", help="Plain‑text instructions file")
    p.add_argument("repo", help="Path to Git repository")
    p.add_argument("--cmd", help="Shell command to run after each patch")
    p.add_argument("--auto", action="store_true", help="Auto‑send 'continue'")
    p.add_argument(
        "--timeout",
        type=int,
        default=DEFAULT_TIMEOUT,
        help=f"Command timeout (s) [env default {DEFAULT_TIMEOUT}]",
    )
    return p.parse_args()


# ════════════════════════════════════════════════════════════════════════════
# Main routine
# ════════════════════════════════════════════════════════════════════════════
def main() -> None:
    args = _cli_args()
    repo = Path(args.repo).expanduser().resolve()

    if not (repo / ".git").exists():
        sys.exit("❌ Not a git repository: " + str(repo))

    # Short, stable session id for this run (used in error‑log chunk tags)
    session_id = uuid.uuid4().hex[:12]
    log.info("Session id: %s", session_id)

    state = _load_state(repo)
    driver = _chrome_driver()

    try:
        # ── RESUME ─────────────────────────────────────────────────────────
        if state and state.get("last_commit") == _current_commit(repo):
            log.info("Resuming conversation: %s", state["conversation_url"])
            driver.get(state["conversation_url"])
            _wait_composer(drv=driver, bounded=False)  # user might need to re‑login
            _save_state_quiet(repo, driver.current_url)

            if args.auto:
                _send_message(driver, "continue")
            else:
                input("Press <Enter> to resume …")
                _send_message(driver, "continue")

        # ── FRESH SESSION ─────────────────────────────────────────────────
        else:
            instr = Path(args.instructions).read_text(encoding="utf-8").strip()
            prompt = _initial_prompt(instr)

            log.debug("Initial prompt built (length=%d chars)", len(prompt))

            # Navigate & **persist early** so a resume is possible even if the
            # first send fails for any reason (network/UI hiccup).
            _navigate_to_chat(driver)
            _save_state_quiet(repo, driver.current_url)

            # Send the initial prompt and persist again (usually yields /c/<id>)
            _send_message(driver, prompt)
            _save_state_quiet(repo, driver.current_url)

        # ── MAIN LOOP ─────────────────────────────────────────────────────
        nudge_budget = NUDGE_RETRIES
        probe_budget = PROBE_RETRIES

        while True:
            reply = _wait_reply(driver)

            # Persist **as soon as** we have a complete assistant reply, keeping
            # the conversation pointer fresh even if patch application fails.
            _save_state_quiet(repo, driver.current_url)

            patch = _extract_patch(reply)

            # If no valid patch: gently ask to resend raw JSON (bounded retries)
            if not patch:
                if nudge_budget > 0:
                    attempt = (NUDGE_RETRIES - nudge_budget) + 1
                    log.warning(
                        "No valid JSON patch found; requesting raw‑JSON resend (attempt %d/%d).",
                        attempt,
                        NUDGE_RETRIES,
                    )
                    _send_message(driver, _nudge_resend_raw_json())
                    nudge_budget -= 1
                    continue

                # Nudges exhausted → capability probe
                if probe_budget > 0:
                    probe_attempt = (PROBE_RETRIES - probe_budget) + 1
                    log.warning(
                        "Raw‑JSON nudges exhausted. Sending capability probe (attempt %d/%d).",
                        probe_attempt,
                        PROBE_RETRIES,
                    )
                    _send_message(driver, _probe_prompt())
                    # Wait for probe response
                    probe_reply = _wait_reply(driver)
                    _save_state_quiet(repo, driver.current_url)
                    probe_patch = _extract_patch(probe_reply)

                    if probe_patch is None:
                        log.error("Probe response still not a valid JSON object.")
                        probe_budget -= 1
                        # Give the assistant one more clear nudge after the probe
                        if probe_budget > 0:
                            _send_message(driver, _nudge_resend_raw_json())
                        continue

                    # If we received the *marker* probe, skip applying; ask for real patch
                    if _is_probe_patch(probe_patch):
                        log.info("Capability probe succeeded (marker patch received).")
                        # Reset the nudge budget; ask for the *real* patch now
                        nudge_budget = NUDGE_RETRIES
                        _send_message(
                            driver,
                            "Thanks. Now please **resend the actual patch** as a single "
                            "**raw JSON** object only (no prose, no fences).",
                        )
                        # Loop for the real patch
                        continue

                    # We got a real valid patch (great!) → proceed with it
                    log.info("Probe returned a valid non‑marker patch; proceeding.")
                    patch = probe_patch

                else:
                    log.error("Exceeded capability probe attempts (%d). Stopping.", PROBE_RETRIES)
                    break

            # Reset nudge budget once we successfully parsed any valid patch
            nudge_budget = NUDGE_RETRIES

            # Guard: ignore the probe patch if it slipped through (extra safety)
            if _is_probe_patch(patch):
                log.info("Ignoring marker probe patch (no apply). Requesting real patch.")
                _send_message(driver, _nudge_resend_raw_json())
                continue

            # Apply patch via STDIN (avoids argv size limits)
            try:
                proc = subprocess.run(
                    [sys.executable, str(ROOT / "apply_patch.py"), "-", str(repo)],
                    input=json.dumps(patch),
                    capture_output=True,
                    text=True,
                )
                if proc.returncode != 0:
                    log.warning("Patch apply failed with code %s", proc.returncode)
                    _send_apply_error(
                        drv=driver,
                        session_id=session_id,
                        repo=repo,
                        patch=patch,
                        stdout=proc.stdout or "",
                        stderr=proc.stderr or "",
                        exit_code=proc.returncode,
                    )
                    # Conversation URL might change while sending logs
                    _save_state_quiet(repo, driver.current_url)
                    continue
            except Exception as exc:
                log.exception("Patch apply raised: %s", exc)
                _send_apply_error(
                    drv=driver,
                    session_id=session_id,
                    repo=repo,
                    patch=patch,
                    stdout="",
                    stderr=str(exc),
                    exit_code=1,
                )
                _save_state_quiet(repo, driver.current_url)
                continue

            # Run command if provided
            if args.cmd:
                ok, output, code = _run_cmd(args.cmd, repo, args.timeout)
                if not ok:
                    _send_error_chunks(
                        driver,
                        session_id=session_id,
                        repo=repo,
                        cmd=args.cmd,
                        exit_code=code,
                        output=output,
                    )
                    _save_state_quiet(repo, driver.current_url)
                    continue

            # Persist state after successful patch
            _save_state(repo, driver.current_url)

            # Finished?
            if patch["status"] == "completed":
                log.info("🎉 All done – tests pass and status=completed.")
                _clear_state(repo)
                break

            # Next chunk
            if args.auto:
                _send_message(driver, "continue")
            else:
                input("Press <Enter> for next patch …")
                _send_message(driver, "continue")

    finally:
        # Keep interactive sessions open; only auto‑quit in headless contexts.
        if os.getenv("GPT_REVIEW_HEADLESS"):
            try:
                driver.quit()
            except Exception:  # pragma: no cover
                pass


# ════════════════════════════════════════════════════════════════════════════
# CLI entry‑point
# ════════════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    main()
