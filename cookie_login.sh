#!/usr/bin/env bash
###############################################################################
# GPT‑Review ▸ Cookie Login Helper
###############################################################################
#
# Why?
# ----
# GPT‑Review controls a **real Chromium browser session**.  You only need to
# sign in to chat.openai.com *once* per Chrome profile; after that, cookies
# are reused on every run.
#
# What this script does
# ---------------------
# 1. Determines the profile directory (`GPT_REVIEW_PROFILE` or default).
# 2. Launches Chromium (non‑headless) with that profile.
# 3. Prints friendly guidance:
#      • "Log in to ChatGPT"
#      • "Close the window when done"
#
# Usage
# -----
#   ./cookie_login.sh
#
# Run again any time your OpenAI session expires or you want to switch
# accounts.  Standard output is safe to copy & paste in bug reports.
###############################################################################

set -euo pipefail

# ---------------------------------------------------------------------------
# 1.  Resolve profile directory
# ---------------------------------------------------------------------------
PROFILE_DIR="${GPT_REVIEW_PROFILE:-$HOME/.cache/gpt-review/chrome}"
mkdir -p "$PROFILE_DIR"

echo "───────────────────────────────────────────────────────────────────────────"
echo " GPT‑Review ▸ Cookie login helper"
echo "───────────────────────────────────────────────────────────────────────────"
echo "• Chrome profile: $PROFILE_DIR"
echo "• This directory now exists (if not before)."
echo
echo "Next steps:"
echo "  1. A Chromium window will open."
echo "  2. Sign in to https://chat.openai.com/ and verify you can chat."
echo "  3. CLOSE the window.  Done!"
echo "───────────────────────────────────────────────────────────────────────────"
echo

# ---------------------------------------------------------------------------
# 2.  Locate Chromium executable
# ---------------------------------------------------------------------------
# Priority: explicit CHROME_BIN env var → chromium-browser → chromium → google-chrome
CHROME_BIN="${CHROME_BIN:-}"
if [[ -z "$CHROME_BIN" ]]; then
  for candidate in chromium-browser chromium google-chrome; do
    if command -v "$candidate" >/dev/null 2>&1; then
      CHROME_BIN="$candidate"
      break
    fi
  done
fi

if [[ -z "$CHROME_BIN" ]]; then
  echo "❌  Chromium / Chrome not found in PATH." >&2
  echo "    • Ubuntu: sudo apt install chromium-browser" >&2
  echo "    • macOS : brew install chromium" >&2
  exit 1
fi

echo "Launching: $CHROME_BIN   (Ctrl‑C to abort)"
echo

# ---------------------------------------------------------------------------
# 3.  Launch browser
# ---------------------------------------------------------------------------
"$CHROME_BIN" --user-data-dir="$PROFILE_DIR" \
              --window-size=1200,900 \
              https://chat.openai.com/ \
              >/dev/null 2>&1 &

PID=$!
wait $PID

echo
echo "───────────────────────────────────────────────────────────────────────────"
echo "✔ Cookies saved.  You may now run:"
echo "   software_review.sh instructions.txt /repo"
echo "───────────────────────────────────────────────────────────────────────────"
