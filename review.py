#!/usr/bin/env python3
"""
===============================================================================
GPT‑Review – Browser‑driven *edit → run → fix* loop
===============================================================================

This driver automates a conversation with ChatGPT to iteratively patch a
local **Git** repository:

1. Show ChatGPT your *instructions*.
2. Receive exactly **one JSON patch** per reply (see schema in README).
3. Apply the patch, commit it, and **optionally run a shell command**
   (tests, linter, build, …).
4. If the command fails, send the *full* log back to ChatGPT and loop.
5. When the command passes **and** ChatGPT sets `"status": "completed"`,
   the session ends.

Key features
------------
* **All patch ops** – create, update, delete, rename, chmod, binary support.
* **Big‑log chunking** – splits failing output into safe 15 kB chunks.
* **Code‑fence tolerant** – accepts ```json … ``` wrappers.
* **Crash‑safe resume** – survives browser/VM crashes (state file).
* **Daily‑rotating logs** – see `logger.py`.
* **Multi‑arch / auto‑driver** – uses *webdriver‑manager*.

-------------------------------------------------------------------------------
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

# -----------------------------------------------------------------------------
# Constants
# -----------------------------------------------------------------------------
CHAT_URL: str = "https://chat.openai.com/"
WAIT_UI: int = 90                     # seconds to wait for chat UI / replies
CHUNK_SIZE: int = 15_000              # characters per error‑log message
RETRIES: int = 3                      # browser action retries
STATE_FILE: str = ".gpt-review-state.json"

# -----------------------------------------------------------------------------
# Logging
# -----------------------------------------------------------------------------
log = get_logger()                    # daily rotating file + console


# =============================================================================
# Utility ‑ Git state persistence
# =============================================================================
def _state_path(repo: Path) -> Path:
    """
    Returns the path to the JSON state file inside *repo*.
    """
    return repo / STATE_FILE


def _load_state(repo: Path) -> Optional[dict]:
    """
    Load the previous session state if it exists, otherwise **None**.
    """
    try:
        return json.loads(_state_path(repo).read_text())
    except FileNotFoundError:
        return None


def _current_commit(repo: Path) -> str:
    """
    Return HEAD commit SHA as a short string.
    """
    return (
        subprocess.check_output(["git", "-C", repo, "rev-parse", "HEAD"])
        .decode()
        .strip()
    )


def _save_state(repo: Path, conversation_url: str) -> None:
    """
    Persist conversation URL and last commit SHA to the state file.
    """
    data = {
        "conversation_url": conversation_url,
        "last_commit": _current_commit(repo),
        "timestamp": int(time.time()),
    }
    _state_path(repo).write_text(json.dumps(data, indent=2))


def _clear_state(repo: Path) -> None:
    """
    Remove the state file (called on normal completion).
    """
    try:
        _state_path(repo).unlink()
    except FileNotFoundError:
        pass


# =============================================================================
# Selenium helpers
# =============================================================================
def _chrome_driver() -> webdriver.Chrome:
    """
    Configure and launch Chromium.

    * Uses `webdriver‑manager` to auto‑download the correct chromedriver.
    * Respects environment variables:
        - `GPT_REVIEW_PROFILE`   – Chrome user‑data dir
        - `GPT_REVIEW_HEADLESS`  – any value → headless mode
    """
    profile_dir = Path(
        os.getenv("GPT_REVIEW_PROFILE", "~/.cache/gpt-review/chrome")
    ).expanduser()
    profile_dir.parent.mkdir(parents=True, exist_ok=True)

    opts = Options()
    opts.add_argument(f"--user-data-dir={profile_dir}")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--no-sandbox")
    if os.getenv("GPT_REVIEW_HEADLESS"):
        opts.add_argument("--headless=new")

    service = Service(ChromeDriverManager().install())
    return webdriver.Chrome(service=service, options=opts)


def _retry(action, what: str):
    """
    Retry *action* (callable) up to RETRIES times with exponential back‑off.
    """
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
    """
    Block until the chat textarea is present (also waits through login).
    """
    while True:
        try:
            WebDriverWait(drv, 5).until(
                EC.presence_of_element_located((By.TAG_NAME, "textarea"))
            )
            return
        except WebDriverException:
            log.info("Waiting for user to log in to chat.openai.com …")
            time.sleep(5)


def _send_message(drv, text: str) -> None:
    """
    Type *text* into ChatGPT and press ↵.
    """
    def _inner():
        textarea = WebDriverWait(drv, 10).until(
            EC.presence_of_element_located((By.TAG_NAME, "textarea"))
        )
        textarea.clear()
        textarea.send_keys(text)
        textarea.send_keys(Keys.ENTER)

    _retry(_inner, "send_message")
    log.debug("Sent %d chars", len(text))


def _last_assistant_block(drv):
    """
    Returns the most recent assistant message WebElement or **None**.
    """
    messages = drv.find_elements(By.CSS_SELECTOR, "div[data-message-author-role='assistant']")
    return messages[-1] if messages else None


def _wait_for_assistant_reply(drv) -> str:
    """
    Wait until the assistant finishes streaming and return its full text.
    """
    _wait_textarea(drv)
    start = time.time()
    last_text, last_change = "", time.time()

    while time.time() - start < WAIT_UI:
        block = _last_assistant_block(drv)
        if block:
            txt = block.text
            if txt != last_text:
                last_text, last_change = txt, time.time()
            # consider done if no change for 2 seconds
            if time.time() - last_change > 2:
                return txt
        time.sleep(1)

    raise TimeoutError("Assistant reply timeout")


# =============================================================================
# Patch extraction (fence‑tolerant, balanced‑brace)
# =============================================================================
_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.S)


def _strip_fence(text: str) -> str:
    """
    Remove ```json … ``` or ``` … ``` wrappers if present.
    """
    m = _FENCE_RE.search(text)
    return m.group(1) if m else text


def _balanced_json(text: str) -> Optional[str]:
    """
    Return the first *balanced* JSON object substring or **None**.
    """
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


def _extract_patch(raw_text: str) -> Optional[dict]:
    """
    Parse and JSON‑schema‑validate the assistant reply.
    """
    blob = _balanced_json(_strip_fence(raw_text))
    if not blob:
        return None
    try:
        return validate_patch(blob)
    except Exception as exc:
        log.error("Patch validation failed: %s", exc)
        return None


# =============================================================================
# Command execution helpers
# =============================================================================
def _run_command(cmd: str, repo: Path, timeout: int) -> Tuple[bool, str]:
    """
    Execute *cmd* inside *repo*; return (success, combined_output).
    """
    try:
        result = subprocess.run(
            cmd,
            cwd=repo,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        output = (exc.stdout or "") + (exc.stderr or "")
        return False, f"TIMEOUT after {timeout}s\n{output}"
    return result.returncode == 0, result.stdout + result.stderr


def _chunk(text: str, size: int = CHUNK_SIZE) -> List[str]:
    """
    Split *text* into ≤ size‑byte chunks (at least one).
    """
    return [text[i : i + size] for i in range(0, len(text), size)] or [""]


def _send_error_chunks(drv, command: str, log_text: str) -> None:
    """
    Post the failing command output back to ChatGPT in safe chunks.
    """
    pieces = _chunk(log_text)
    header = textwrap.dedent(
        f"""\
        The command **{command}** failed (chunk 1/{len(pieces)}). Please fix.

        ```text
        {pieces[0]}
        ```"""
    )
    _send_message(drv, header)

    for index, part in enumerate(pieces[1:], start=2):
        follow_up = f"Log chunk {index}/{len(pieces)}:\n```text\n{part}\n```"
        _send_message(drv, follow_up)


# =============================================================================
# Main driver routine
# =============================================================================
def _parse_args() -> argparse.Namespace:
    """
    CLI argument parsing.
    """
    parser = argparse.ArgumentParser(prog="gpt-review")
    parser.add_argument("instructions", help="Plain‑text instructions file")
    parser.add_argument("repo", help="Path to a local Git repository")
    parser.add_argument(
        "--cmd",
        help="Shell command to run after each patch (e.g. 'pytest -q')",
    )
    parser.add_argument(
        "--auto",
        action="store_true",
        help="Auto‑send 'continue' (otherwise press <Enter> each time)",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=300,
        help="Kill --cmd after N seconds (default: 300)",
    )
    return parser.parse_args()


def main() -> None:
    """
    Program entry‑point. Orchestrates the whole review loop.
    """
    args = _parse_args()
    repo = Path(args.repo).expanduser().resolve()

    if not (repo / ".git").exists():
        sys.exit("❌  The path is not a git repository.")

    # --------------------------------------------------------------------- #
    # 1. Browser setup & resume / fresh conversation
    # --------------------------------------------------------------------- #
    state = _load_state(repo)
    driver = _chrome_driver()

    try:
        # -------------------- Resume path --------------------
        if state and state.get("last_commit") == _current_commit(repo):
            log.info("Resuming previous session: %s", state['conversation_url'])
            driver.get(state["conversation_url"])
            _wait_textarea(driver)
            if args.auto:
                _send_message(driver, "continue")
            else:
                input("Press <Enter> to resume …")
                _send_message(driver, "continue")

        # -------------------- Fresh session ------------------
        else:
            instructions = Path(args.instructions).read_text(encoding="utf-8").strip()
            json_schema_blurb = (
                '{ "op": "create|update|delete|rename|chmod", '
                '"file": "...", "status": "in_progress|completed", ... }'
            )
            initial_prompt = (
                f"{instructions}\n\n"
                "Work **one file at a time**.\n\n"
                f"Return *exactly one* JSON object, no prose, matching:\n"
                f"{json_schema_blurb}\n\n"
                "Use `status = in_progress` while patches remain, "
                "`status = completed` when done."
            )

            driver.get(CHAT_URL)
            _wait_textarea(driver)
            _send_message(driver, initial_prompt)

            # Save the conversation URL for crash‑safe resume
            _save_state(repo, driver.current_url)

        # ---------------------------------------------------------------- #
        # 2. Main loop: wait → patch → apply → test → continue / finish
        # ---------------------------------------------------------------- #
        while True:
            assistant_reply = _wait_for_assistant_reply(driver)
            patch = _extract_patch(assistant_reply)

            if not patch:
                log.warning("No JSON patch found in assistant reply.")
                break

            # Apply patch via auxiliary script (handles safety checks)
            subprocess.run(
                [
                    sys.executable,
                    str(Path(__file__).with_name("apply_patch.py")),
                    json.dumps(patch),
                    str(repo),
                ],
                check=True,
            )

            # Optional: run user command
            if args.cmd:
                success, output = _run_command(args.cmd, repo, args.timeout)
                if not success:
                    _send_error_chunks(driver, args.cmd, output)
                    continue  # ask ChatGPT to fix and loop again

            # Persist state (conversation URL may change on refresh)
            _save_state(repo, driver.current_url)

            # Finished?
            if patch["status"] == "completed":
                log.info("🎉  Session completed successfully.")
                _clear_state(repo)
                break

            # Otherwise, request next chunk
            if args.auto:
                _send_message(driver, "continue")
            else:
                input("Press <Enter> for next patch …")
                _send_message(driver, "continue")

    finally:
        if os.getenv("GPT_REVIEW_HEADLESS"):
            driver.quit()


# =============================================================================
# CLI entry‑point
# =============================================================================
if __name__ == "__main__":
    main()
