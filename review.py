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

High‑impact robustness
----------------------
• Driver provisioning order:
  CHROMEDRIVER → Selenium Manager → webdriver‑manager.
• Composer detection/clearing (textarea → contenteditable fallback).
• Apply‑failure reporting (non‑fatal).
• Patch delivery to apply tool via STDIN.
• Safe commit lookup on fresh repos.
• Non‑BMP input fallback (emoji, etc.) via JS fill.
• Send‑button fallback if Enter doesn’t submit.
• Reply wait keeps going while content is still streaming.
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
    'Use status="in_progress" until the last patch, then "completed".'
)

PROBE_FILE_MAGIC = "__gpt_review_probe__"
PROBE_BODY_EXPECTED = "ok"

ROOT = Path(__file__).resolve().parent
log = get_logger(__name__)

# ════════════════════════════════════════════════════════════════════════════
# State helpers – crash‑safe resume
# ════════════════════════════════════════════════════════════════════════════
def _state_path(repo: Path) -> Path:
    return repo / STATE_FILE


def _load_state(repo: Path) -> Optional[dict]:
    try:
        return json.loads(_state_path(repo).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None


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


def _save_state(repo: Path, url: str) -> None:
    data = {
        "conversation_url": url,
        "last_commit": _current_commit(repo),
        "timestamp": int(time.time()),
    }
    _state_path(repo).write_text(json.dumps(data, indent=2), encoding="utf-8")
    log.debug("State saved: %s", data)


def _clear_state(repo: Path) -> None:
    try:
        _state_path(repo).unlink()
    except FileNotFoundError:
        pass


def _save_state_quiet(repo: Path, url: str) -> None:
    try:
        _save_state(repo, url)
    except Exception as exc:  # pragma: no cover
        log.warning("Non‑fatal: failed to persist state (%s): %s", url, exc)


# ════════════════════════════════════════════════════════════════════════════
# Browser detection & Selenium helpers
# ════════════════════════════════════════════════════════════════════════════
def _detect_browser_binary() -> Optional[str]:
    env_bin = os.getenv("CHROME_BIN", "")
    if env_bin and os.path.exists(env_bin) and os.access(env_bin, os.X_OK):
        return env_bin

    for name in (
        "google-chrome-stable",
        "google-chrome",
        "chromium",
        "chromium-browser",
        "chrome",
    ):
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
    try:
        caps = getattr(drv, "capabilities", {}) or {}
        browser_version = caps.get("browserVersion", "unknown")
        chrome_block = caps.get("chrome", {}) if isinstance(caps, dict) else {}
        driver_version = (
            chrome_block.get("chromedriverVersion", "unknown").split(" ") or ["unknown"]
        )[0]
        log.info(
            "Browser launched – version=%s, driver=%s", browser_version, driver_version
        )
    except Exception:
        log.info("Browser launched.")


def _chrome_driver() -> webdriver.Chrome:
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

    # 1) CHROMEDRIVER
    explicit_driver = os.getenv("CHROMEDRIVER")
    if explicit_driver and os.path.exists(explicit_driver) and os.access(
        explicit_driver, os.X_OK
    ):
        log.info("Using CHROMEDRIVER from env: %s", explicit_driver)
        service = Service(explicit_driver)
        drv = webdriver.Chrome(service=service, options=opts)
        _log_driver_versions(drv)
        return drv

    # 2) Selenium Manager
    try:
        drv = webdriver.Chrome(options=opts)
        _log_driver_versions(drv)
        return drv
    except Exception as exc:
        log.warning("Selenium Manager failed to provision driver: %s", exc)

    # 3) webdriver‑manager
    if _WDM_AVAILABLE:
        try:
            service = Service(ChromeDriverManager().install())
            drv = webdriver.Chrome(service=service, options=opts)
            _log_driver_versions(drv)
            return drv
        except Exception as exc:  # pragma: no cover
            log.warning("webdriver‑manager failed as fallback: %s", exc)

    raise RuntimeError(
        "Unable to provision a Chrome driver. "
        "Set CHROMEDRIVER, enable Selenium Manager downloads, or install webdriver‑manager."
    )


def _retry(action, what: str):
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
    # 1) Known selector
    try:
        els = drv.find_elements(
            By.CSS_SELECTOR, "textarea[data-testid='composer-textarea']"
        )
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

    # 3) contenteditable
    try:
        els = drv.find_elements(By.CSS_SELECTOR, "div[contenteditable='true']")
    except WebDriverException:
        els = []
    visible = [e for e in els if _is_interactable(e)]
    if visible:
        return visible[-1]

    return None


def _wait_composer(drv, *, bounded: bool = False, max_wait: int = WAIT_UI) -> None:
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


# Back‑compat aliases (internal use only)
def _find_textarea(drv):  # pragma: no cover
    return _find_composer(drv)


def _wait_textarea(drv, *, bounded: bool = False, max_wait: int = WAIT_UI):  # pragma: no cover
    return _wait_composer(drv, bounded=bounded, max_wait=max_wait)


# ────────────────────────────────────────────────────────────────────────────
#  Non‑BMP input + reliable send
# ────────────────────────────────────────────────────────────────────────────
def _contains_non_bmp(text: str) -> bool:
    return any(ord(ch) > 0xFFFF for ch in text)


def _js_fill_input(drv, el, text: str) -> None:
    js = """
    const el = arguments[0];
    const val = arguments[1];
    const tag = (el.tagName || '').toLowerCase();
    if (tag === 'textarea') {
      el.value = val;
    } else {
      el.innerHTML = '';
      el.textContent = val;
    }
    el.dispatchEvent(new Event('input', {bubbles: true}));
    el.dispatchEvent(new Event('change', {bubbles: true}));
    """
    drv.execute_script(js, el, text)


def _get_input_value(drv, el) -> str:
    js = """
    const el = arguments[0];
    const tag = (el.tagName || '').toLowerCase();
    return tag === 'textarea' ? (el.value || '') : (el.textContent || '');
    """
    return (drv.execute_script(js, el) or "").strip()


def _click_send_button(drv) -> bool:
    """
    Best‑effort: click Send button if present.
    Returns True if a click was attempted.
    """
    selectors = [
        "button[data-testid='send-button']",
        "button[aria-label='Send']",
        "button[type='submit']",
    ]
    for sel in selectors:
        try:
            btns = drv.find_elements(By.CSS_SELECTOR, sel)
        except WebDriverException:
            btns = []
        for b in btns:
            try:
                if _is_interactable(b):
                    b.click()
                    return True
            except WebDriverException:
                continue
    return False


def _clear_and_send(area, text: str) -> None:
    """Focus, clear any draft, input *text*, submit via Enter; click‑send fallback."""
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

    drv = getattr(area, "parent", None) or getattr(area, "_parent", None)

    # Input
    try:
        if _contains_non_bmp(text) and drv is not None:
            log.debug("Non‑BMP detected; using JS fill (%d chars).", len(text))
            _js_fill_input(drv, area, text)
        else:
            area.send_keys(text)
    except WebDriverException as exc:
        if (("BMP" in str(exc)) or _contains_non_bmp(text)) and drv is not None:
            log.warning("send_keys rejected chars; JS fill fallback (%d chars).", len(text))
            _js_fill_input(drv, area, text)
        else:
            raise

    # Submit via Enter; then verify; if still present, click Send.
    area.send_keys(Keys.ENTER)
    try:
        time.sleep(0.25)
        if drv is not None and _get_input_value(drv, area):
            clicked = _click_send_button(drv)
            if clicked:
                log.debug("Clicked Send button as fallback.")
    except Exception:
        pass


def _send_message(drv, text: str) -> None:
    def _inner():
        area = _find_composer(drv)
        if not area:
            raise WebDriverException("Composer not found")
        _clear_and_send(area, text)

    _retry(_inner, "send_message")
    log.debug("Sent %d chars", len(text))


# ────────────────────────────────────────────────────────────────────────────
#  Waiting for replies (progress‑aware)
# ────────────────────────────────────────────────────────────────────────────
def _assistant_block(drv):
    try:
        blocks = drv.find_elements(
            By.CSS_SELECTOR, "div[data-message-author-role='assistant']"
        )
    except WebDriverException:
        blocks = []
    return blocks[-1] if blocks else None


def _wait_reply(drv) -> str:
    """
    Wait until streaming stops. Keep waiting past WAIT_UI if we still
    observe progress; time out only after *IDLE_SECS* of no change.
    """
    _wait_composer(drv, bounded=True, max_wait=WAIT_UI)
    start = time.time()
    last_txt, last_change = "", time.time()

    while True:
        block = _assistant_block(drv)
        if block:
            txt = block.text
            if txt != last_txt:
                last_txt, last_change = txt, time.time()
            # If idle long enough and we have *some* text, return it
            if (time.time() - last_change) > IDLE_SECS and last_txt:
                return last_txt

        # Hard stop only if we've exceeded WAIT_UI **and** there was no change for IDLE_SECS
        if (time.time() - start) > WAIT_UI and (time.time() - last_change) > IDLE_SECS:
            raise TimeoutError("Assistant reply timeout")

        time.sleep(0.5)


def _navigate_to_chat(drv) -> None:
    for url in (CHAT_URL, CHAT_URL_FALLBACK):
        try:
            drv.get(url)
            _wait_composer(drv, bounded=False)
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


# ════════════════════════════════════════════════════════════════════════════
# Command execution & error‑chunking
# ════════════════════════════════════════════════════════════════════════════
def _run_cmd(cmd: str, repo: Path, timeout: int) -> Tuple[bool, str, int]:
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
    return [text[i : i + size] for i in range(0, len(text), size)] or [""]


def _now_iso_utc() -> str:
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
    chunks = _chunk(output)
    N = len(chunks)
    commit = _current_commit(repo)
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

    session_id = uuid.uuid4().hex[:12]
    log.info("Session id: %s", session_id)

    state = _load_state(repo)
    driver = _chrome_driver()

    try:
        # ── RESUME ─────────────────────────────────────────────────────────
        if state and state.get("last_commit") == _current_commit(repo):
            log.info("Resuming conversation: %s", state["conversation_url"])
            driver.get(state["conversation_url"])
            _wait_composer(drv=driver, bounded=False)
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

            _navigate_to_chat(driver)
            _save_state_quiet(repo, driver.current_url)

            _send_message(driver, prompt)
            _save_state_quiet(repo, driver.current_url)

        # ── MAIN LOOP ─────────────────────────────────────────────────────
        nudge_budget = NUDGE_RETRIES
        probe_budget = PROBE_RETRIES

        while True:
            reply = _wait_reply(driver)
            _save_state_quiet(repo, driver.current_url)

            patch = _extract_patch(reply)

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

                if probe_budget > 0:
                    probe_attempt = (PROBE_RETRIES - probe_budget) + 1
                    log.warning(
                        "Raw‑JSON nudges exhausted. Sending capability probe (attempt %d/%d).",
                        probe_attempt,
                        PROBE_RETRIES,
                    )
                    _send_message(driver, _probe_prompt())
                    probe_reply = _wait_reply(driver)
                    _save_state_quiet(repo, driver.current_url)
                    probe_patch = _extract_patch(probe_reply)

                    if probe_patch is None:
                        log.error("Probe response still not a valid JSON object.")
                        probe_budget -= 1
                        if probe_budget > 0:
                            _send_message(driver, _nudge_resend_raw_json())
                        continue

                    if _is_probe_patch(probe_patch):
                        log.info("Capability probe succeeded (marker patch received).")
                        nudge_budget = NUDGE_RETRIES
                        _send_message(
                            driver,
                            "Thanks. Now please **resend the actual patch** as a single "
                            "**raw JSON** object only (no prose, no fences).",
                        )
                        continue

                    log.info("Probe returned a valid non‑marker patch; proceeding.")
                    patch = probe_patch

                else:
                    log.error(
                        "Exceeded capability probe attempts (%d). Stopping.",
                        PROBE_RETRIES,
                    )
                    break

            nudge_budget = NUDGE_RETRIES

            if _is_probe_patch(patch):
                log.info("Ignoring marker probe patch (no apply). Requesting real patch.")
                _send_message(driver, _nudge_resend_raw_json())
                continue

            # Apply patch via STDIN
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

            _save_state(repo, driver.current_url)

            if patch["status"] == "completed":
                log.info("🎉 All done – tests pass and status=completed.")
                _clear_state(repo)
                break

            if args.auto:
                _send_message(driver, "continue")
            else:
                input("Press <Enter> for next patch …")
                _send_message(driver, "continue")

    finally:
        if os.getenv("GPT_REVIEW_HEADLESS"):
            try:
                driver.quit()
            except Exception:  # pragma: no cover
                pass


if __name__ == "__main__":
    main()
