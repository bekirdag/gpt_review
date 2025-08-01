#!/usr/bin/env python3
"""
===============================================================================
GPT‑Review ▸ Main Driver
===============================================================================

Automates an **edit → run → fix** conversation between you and ChatGPT.

Flow
----
1. Present *instructions* to ChatGPT.
2. Receive **one JSON patch** per reply (see README for schema).
3. Apply the patch to a Git repository & commit.
4. Optionally run *any shell command* (tests, linter, build, …).
5. If the command fails, send the full log back to ChatGPT.
6. Repeat until the command passes **and** `"status": "completed"`.

Extra rule (added 2025‑08‑01)
-----------------------------
*ChatGPT must:*

* deliver **one script per answer** (chunk‑by‑chunk),
* explicitly ask the user to **continue** before proceeding.

These constraints ensure deterministic patching and a clean Git history.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import textwrap
import time
from pathlib import Path
from typing import List, Optional, Tuple

from selenium import webdriver
from selenium.common.exceptions import WebDriverException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from webdriver_manager.chrome import ChromeDriverManager

from logger import get_logger
from patch_validator import validate_patch

# ════════════════════════════════════════════════════════════════════════════
# Globals & constants
# ════════════════════════════════════════════════════════════════════════════
CHAT_URL: str = "https://chat.openai.com/"
WAIT_UI: int = 90                     # Seconds to wait for UI / replies
CHUNK_SIZE: int = 15_000              # Error‑log chunk size
RETRIES: int = 3                      # Browser action retries
STATE_FILE: str = ".gpt-review-state.json"

EXTRA_RULES: str = (
    "Your fixes must be **chunk by chunk**. "
    "Provide a fix for **one script only** in each answer. "
    "Ask me to **continue** when you are done with one script."
)

ROOT = Path(__file__).resolve().parent
log = get_logger(__name__)

# ════════════════════════════════════════════════════════════════════════════
# State helpers – crash‑safe resume
# ════════════════════════════════════════════════════════════════════════════
def _state_path(repo: Path) -> Path:  # noqa: D401
    """Return path of the persistent state‑file inside *repo*."""
    return repo / STATE_FILE


def _load_state(repo: Path) -> Optional[dict]:
    """Load state‑file if present, else *None*."""
    try:
        return json.loads(_state_path(repo).read_text())
    except FileNotFoundError:
        return None


def _current_commit(repo: Path) -> str:
    """Return SHA of HEAD commit in *repo*."""
    return (
        subprocess.check_output(["git", "-C", repo, "rev-parse", "HEAD"])
        .decode()
        .strip()
    )


def _save_state(repo: Path, url: str) -> None:
    """Write resume metadata to disk."""
    data = {
        "conversation_url": url,
        "last_commit": _current_commit(repo),
        "timestamp": int(time.time()),
    }
    _state_path(repo).write_text(json.dumps(data, indent=2))


def _clear_state(repo: Path) -> None:
    """Delete state‑file if it exists."""
    try:
        _state_path(repo).unlink()
    except FileNotFoundError:
        pass


# ════════════════════════════════════════════════════════════════════════════
# Selenium helpers
# ════════════════════════════════════════════════════════════════════════════
def _chrome_driver() -> webdriver.Chrome:
    """Launch Chromium with a persistent profile (headless‑optional)."""
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

    service = Service(ChromeDriverManager().install())
    return webdriver.Chrome(service=service, options=opts)


def _retry(action, what: str):
    """Retry *action* up to RETRIES times with exponential back‑off."""
    pause = 2
    for attempt in range(1, RETRIES + 1):
        try:
            return action()
        except WebDriverException as exc:
            if attempt == RETRIES:
                raise
            log.warning("%s failed (attempt %d/%d): %s", what, attempt, RETRIES, exc)
            time.sleep(pause)
            pause *= 2


def _wait_textarea(drv) -> None:
    """Block until chat textarea appears (user may need to sign‑in)."""
    while True:
        try:
            WebDriverWait(drv, 5).until(
                EC.presence_of_element_located((By.TAG_NAME, "textarea"))
            )
            return
        except WebDriverException:
            log.info("Waiting for user to sign in to ChatGPT …")
            time.sleep(5)


def _send_message(drv, text: str) -> None:
    """Send *text* to ChatGPT (clears any previous draft)."""

    def _inner():
        area = WebDriverWait(drv, 10).until(
            EC.presence_of_element_located((By.TAG_NAME, "textarea"))
        )
        area.clear()
        area.send_keys(text)
        area.send_keys(Keys.ENTER)

    _retry(_inner, "send_message")
    log.debug("Sent %d chars", len(text))


def _assistant_block(drv):
    """Return the most recent assistant message element (or *None*)."""
    blocks = drv.find_elements(
        By.CSS_SELECTOR, "div[data-message-author-role='assistant']"
    )
    return blocks[-1] if blocks else None


def _wait_reply(drv) -> str:
    """Wait for ChatGPT to finish streaming and return the reply text."""
    _wait_textarea(drv)
    start = time.time()
    last_txt, last_change = "", time.time()

    while time.time() - start < WAIT_UI:
        block = _assistant_block(drv)
        if block:
            txt = block.text
            if txt != last_txt:
                last_txt, last_change = txt, time.time()
            # 2 s of inactivity ≈ finished streaming
            if time.time() - last_change > 2:
                return txt
        time.sleep(1)

    raise TimeoutError("Assistant reply timeout")


# ════════════════════════════════════════════════════════════════════════════
# Patch extraction – code‑fence tolerant, balanced braces
# ════════════════════════════════════════════════════════════════════════════
_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.S)


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
# Command execution helpers
# ════════════════════════════════════════════════════════════════════════════
def _run_cmd(cmd: str, repo: Path, timeout: int) -> Tuple[bool, str]:
    """Execute *cmd* in *repo*; return (success, combined output)."""
    try:
        res = subprocess.run(
            cmd,
            cwd=repo,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        out = (exc.stdout or "") + (exc.stderr or "")
        return False, f"TIMEOUT after {timeout}s\n{out}"
    return res.returncode == 0, res.stdout + res.stderr


def _chunk(text: str, size: int = CHUNK_SIZE) -> List[str]:
    """Split *text* into ≤*size* chunks (never empty list)."""
    return [text[i : i + size] for i in range(0, len(text), size)] or [""]


def _send_error_chunks(drv, cmd: str, output: str) -> None:
    """Post failing log back to ChatGPT in safe‑sized chunks."""
    chunks = _chunk(output)
    header = textwrap.dedent(
        f"""\
        The command **{cmd}** failed (chunk 1/{len(chunks)}). Please fix.

        ```text
        {chunks[0]}
        ```"""
    )
    _send_message(drv, header)

    for idx, part in enumerate(chunks[1:], start=2):
        _send_message(drv, f"Log chunk {idx}/{len(chunks)}:\n```text\n{part}\n```")


# ════════════════════════════════════════════════════════════════════════════
# CLI parsing
# ════════════════════════════════════════════════════════════════════════════
def _cli_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="gpt-review")
    p.add_argument("instructions", help="Plain‑text instructions file")
    p.add_argument("repo", help="Path to Git repository")
    p.add_argument("--cmd", help="Shell command to run after each patch")
    p.add_argument("--auto", action="store_true", help="Auto‑send 'continue'")
    p.add_argument("--timeout", type=int, default=300, help="Command timeout (s)")
    return p.parse_args()


# ════════════════════════════════════════════════════════════════════════════
# Main routine
# ════════════════════════════════════════════════════════════════════════════
def main() -> None:
    args = _cli_args()
    repo = Path(args.repo).expanduser().resolve()

    if not (repo / ".git").exists():
        sys.exit("❌ Not a git repository: " + str(repo))

    state = _load_state(repo)
    driver = _chrome_driver()

    try:
        # ── RESUME ─────────────────────────────────────────────────────────
        if state and state.get("last_commit") == _current_commit(repo):
            log.info("Resuming conversation: %s", state["conversation_url"])
            driver.get(state["conversation_url"])
            _wait_textarea(driver)
            if args.auto:
                _send_message(driver, "continue")
            else:
                input("Press <Enter> to resume …")
                _send_message(driver, "continue")

        # ── FRESH SESSION ─────────────────────────────────────────────────
        else:
            instr = Path(args.instructions).read_text(encoding="utf-8").strip()
            schema_blurb = (
                '{ "op": "create|update|delete|rename|chmod", '
                '"file": "...", "status": "in_progress|completed", ... }'
            )
            prompt = (
                f"{instr}\n\n"
                "Work **one file at a time**.\n\n"
                f"{EXTRA_RULES}\n\n"
                f"Return *exactly* one JSON object (no extra text):\n{schema_blurb}\n\n"
                "Use `status = in_progress` while patches remain, "
                "`status = completed` when done."
            )

            log.debug("Initial prompt built (length=%d chars)", len(prompt))
            driver.get(CHAT_URL)
            _wait_textarea(driver)
            _send_message(driver, prompt)
            _save_state(repo, driver.current_url)

        # ── MAIN LOOP ─────────────────────────────────────────────────────
        while True:
            reply = _wait_reply(driver)
            patch = _extract_patch(reply)

            if not patch:
                log.warning("Assistant reply contained no valid patch.")
                break

            # Apply patch
            subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "apply_patch.py"),
                    json.dumps(patch),
                    str(repo),
                ],
                check=True,
            )

            # Run command if provided
            if args.cmd:
                ok, output = _run_cmd(args.cmd, repo, args.timeout)
                if not ok:
                    _send_error_chunks(driver, args.cmd, output)
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
        if os.getenv("GPT_REVIEW_HEADLESS"):
            driver.quit()


# ════════════════════════════════════════════════════════════════════════════
# CLI entry‑point
# ════════════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    main()
