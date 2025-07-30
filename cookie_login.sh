#!/usr/bin/env bash
# One‑time helper: open Chromium with the same profile directory so you can log in to chat.openai.com manually.
set -euo pipefail

PROFILE_DIR="${GPT_REVIEW_PROFILE:-$HOME/.cache/gpt-review/chrome}"
mkdir -p "$PROFILE_DIR"

CHROME_BIN=$(command -v chromium-browser 2>/dev/null || command -v chromium)
if [[ -z "$CHROME_BIN" ]]; then
  echo "Chromium not found. Run install.sh first." >&2
  exit 1
fi

echo "Launching Chromium with GPT‑Review profile at: $PROFILE_DIR"
echo "→ Log in to chat.openai.com, then close the window."
exec "$CHROME_BIN" --user-data-dir="$PROFILE_DIR"
