#!/usr/bin/env bash
###############################################################################
# GPT‑Review ▸ Cookie Login Helper
###############################################################################
#
# Purpose
# -------
# GPT‑Review controls a **real Chromium/Chrome browser session**. You only need
# to sign in to ChatGPT **once** per Chrome profile; cookies then persist.
#
# What this script does
# ---------------------
# 1) Determines the profile directory (GPT_REVIEW_PROFILE or default).
# 2) Detects a suitable browser binary (CHROME_BIN override respected).
# 3) Launches a **visible** browser window with that profile.
# 4) Opens ChatGPT: primary URL (GPT_REVIEW_LOGIN_URL or https://chatgpt.com/)
#    with a fallback tab to https://chat.openai.com/ **unless identical**.
# 5) Prints friendly guidance and waits until you close the window (Linux),
#    or asks you to press <Enter> when done (macOS `open -a` fallback).
#
# Usage
# -----
#   ./cookie_login.sh
#
# Notes
# -----
# • This helper intentionally ignores headless mode; you must log in visibly.
# • On macOS, if we cannot exec the browser binary directly, we use `open -a`
#   and prompt you to press <Enter> to finish.
###############################################################################

set -euo pipefail

# ------------------------------------------------------------------------------
# Resolve profile directory
# ------------------------------------------------------------------------------
PROFILE_DIR="${GPT_REVIEW_PROFILE:-$HOME/.cache/gpt-review/chrome}"
mkdir -p "$PROFILE_DIR"

# Primary login URL can be overridden for SSO/region‑specific entrypoints
PRIMARY_URL="${GPT_REVIEW_LOGIN_URL:-https://chatgpt.com/}"
FALLBACK_URL="https://chat.openai.com/"

# ------------------------------------------------------------------------------
# Pretty colours (fallback to plain if not TTY)
# ------------------------------------------------------------------------------
if [[ -t 1 ]]; then
  C_INFO='\e[34m'; C_OK='\e[32m'; C_WARN='\e[33m'; C_ERR='\e[31m'; C_END='\e[0m'
else
  C_INFO=''; C_OK=''; C_WARN=''; C_ERR=''; C_END=''
fi
info()  { echo -e "${C_INFO}▶︎${C_END} $*"; }
warn()  { echo -e "${C_WARN}⚠${C_END} $*" >&2; }
ok()    { echo -e "${C_OK}✓${C_END} $*"; }
die()   { echo -e "${C_ERR}✖${C_END} $*" >&2; exit 1; }

# ------------------------------------------------------------------------------
# URL helpers
# ------------------------------------------------------------------------------
norm_url() {
  # Lowercase (portable; avoid Bash 4.x `${var,,}` for macOS Bash 3.2),
  # then strip a single trailing slash for equivalence checks.
  # shellcheck disable=SC2001
  local u
  u="$(printf "%s" "${1:-}" | tr '[:upper:]' '[:lower:]')"
  u="${u%/}"
  printf "%s" "$u"
}

SAME_AS_FALLBACK=0
if [[ "$(norm_url "$PRIMARY_URL")" == "$(norm_url "$FALLBACK_URL")" ]]; then
  SAME_AS_FALLBACK=1
fi

# Build the final URL list to open
URL_ARGS=("$PRIMARY_URL")
if [[ $SAME_AS_FALLBACK -eq 0 ]]; then
  URL_ARGS+=("$FALLBACK_URL")
fi

# ------------------------------------------------------------------------------
# Detect browser binary
# ------------------------------------------------------------------------------
detect_browser() {
  # 1) Explicit override
  if [[ -n "${CHROME_BIN:-}" ]]; then
    if [[ -x "${CHROME_BIN:-/nonexistent}" ]]; then
      echo "$CHROME_BIN"; return 0
    else
      warn "CHROME_BIN is set but not executable: ${CHROME_BIN}"
    fi
  fi

  # 2) Common PATH candidates (Linux/WSL)
  local candidate
  for candidate in google-chrome-stable google-chrome chromium chromium-browser chrome; do
    if command -v "$candidate" >/dev/null 2>&1; then
      command -v "$candidate"; return 0
    fi
  done

  # 3) macOS app bundles – return direct binary if present
  if [[ "$(uname -s)" == "Darwin" ]]; then
    local mac_bins=(
      "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
      "/Applications/Chromium.app/Contents/MacOS/Chromium"
    )
    for candidate in "${mac_bins[@]}"; do
      if [[ -x "$candidate" ]]; then
        echo "$candidate"; return 0
      fi
    done
  fi

  # None found
  echo ""
  return 1
}

BROWSER_BIN="$(detect_browser || true)"

# ------------------------------------------------------------------------------
# Banner
# ------------------------------------------------------------------------------
echo "───────────────────────────────────────────────────────────────────────────"
echo " GPT‑Review ▸ Cookie login helper"
echo "───────────────────────────────────────────────────────────────────────────"
echo "• Chrome profile : $PROFILE_DIR"
echo "• Primary URL    : $PRIMARY_URL"
if [[ $SAME_AS_FALLBACK -eq 1 ]]; then
  echo "• Fallback URL   : (suppressed – same as primary)"
else
  echo "• Fallback URL   : $FALLBACK_URL"
fi
if [[ -n "${GPT_REVIEW_HEADLESS:-}" ]]; then
  warn "Ignoring GPT_REVIEW_HEADLESS – login requires a visible browser."
fi
if [[ -n "$BROWSER_BIN" ]]; then
  echo "• Browser binary : $BROWSER_BIN"
else
  echo "• Browser binary : (not found on PATH)"
fi
echo
echo "Next steps:"
if [[ $SAME_AS_FALLBACK -eq 1 ]]; then
  echo "  1) A browser window will open with $PRIMARY_URL."
else
  echo "  1) A browser window will open with $PRIMARY_URL (and a fallback tab)."
fi
echo "  2) Sign in to ChatGPT and verify you can chat."
echo "  3) CLOSE the window to finish (or press Enter if prompted)."
echo "───────────────────────────────────────────────────────────────────────────"
echo

# ------------------------------------------------------------------------------
# Launch browser (platform‑aware)
# ------------------------------------------------------------------------------
if [[ -n "$BROWSER_BIN" ]]; then
  info "Launching: $BROWSER_BIN"
  # Launch tabs. Run in background then wait.
  "$BROWSER_BIN" \
    --user-data-dir="$PROFILE_DIR" \
    --window-size=1200,900 \
    --no-first-run \
    --no-default-browser-check \
    "${URL_ARGS[@]}" \
    >/dev/null 2>&1 &
  PID=$!
  # Trap ensures we don't leave a zombie on Ctrl-C
  trap 'kill -TERM $PID >/dev/null 2>&1 || true' INT TERM
  wait "$PID" || true
else
  # macOS fallback: use `open -a` if we couldn't find a direct binary
  if [[ "$(uname -s)" == "Darwin" ]]; then
    if [[ -d "/Applications/Google Chrome.app" ]]; then
      info "Launching via 'open -a Google Chrome' (macOS)"
      open -a "Google Chrome" --args \
        --user-data-dir="$PROFILE_DIR" \
        --window-size=1200,900 \
        --no-first-run \
        --no-default-browser-check \
        "${URL_ARGS[@]}" || true
    elif [[ -d "/Applications/Chromium.app" ]]; then
      info "Launching via 'open -a Chromium' (macOS)"
      open -a "Chromium" --args \
        --user-data-dir="$PROFILE_DIR" \
        --window-size=1200,900 \
        --no-first-run \
        --no-default-browser-check \
        "${URL_ARGS[@]}" || true
    else
      die "No Chrome/Chromium app found. Install Chrome or set CHROME_BIN."
    fi
    echo
    # Prompt only on TTY to avoid hanging in CI
    if [[ -t 0 ]]; then
      read -r -p "Press <Enter> once you have finished logging in and closed the browser…" _
    else
      warn "Non‑interactive shell detected; assuming login completed."
    fi
  else
    die "Chromium/Chrome not found. Install 'chromium' or 'google-chrome', or set CHROME_BIN."
  fi
fi

echo
echo "───────────────────────────────────────────────────────────────────────────"
ok "Cookies saved. You may now run:"
echo "   software_review.sh instructions.txt /repo"
echo "───────────────────────────────────────────────────────────────────────────"
