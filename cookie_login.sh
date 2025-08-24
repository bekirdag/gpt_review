#!/usr/bin/env bash
###############################################################################
# GPT‑Review ▸ Cookie Login Helper (robust for SSH/X11 & root)
###############################################################################
#
# Purpose
# -------
# GPT‑Review drives a **real Chrome/Chromium session**. You must sign in once;
# cookies then persist in the selected Chrome profile directory.
#
# What’s new
# ----------
# • Adds `--no-sandbox` automatically when running as **root** (Chrome/Chromium
#   refuse to start as root without it).
# • Validates that a **DISPLAY** is available on Linux (XQuartz/X11/VNC).
# • Detects **Snap Chromium** and avoids dot‑dir profiles by default (uses a
#   snap‑writable profile unless you explicitly override GPT_REVIEW_PROFILE).
# • Captures browser logs to a temp file and shows diagnostics if startup fails.
#
# Usage
# -----
#   cookie_login.sh
#
# Common environment overrides
# ----------------------------
#   GPT_REVIEW_PROFILE   – where Chrome stores cookies (default varies; see below)
#   GPT_REVIEW_LOGIN_URL – primary URL to open (default: https://chatgpt.com/)
#   CHROME_BIN           – explicit browser binary
#
###############################################################################

set -euo pipefail

# ------------------------------------------------------------------------------
# Pretty colours (fallback to plain if not TTY)
# ------------------------------------------------------------------------------
if [[ -t 1 ]]; then
  C_INFO='\e[34m'; C_OK='\e[32m'; C_WARN='\e[33m'; C_ERR='\e[31m'; C_END='\e[0m'
else
  C_INFO=''; C_OK=''; C_WARN=''; C_ERR=''; C_END=''
fi
info()  { echo -e "${C_INFO}▶︎${C_END} $*"; }
ok()    { echo -e "${C_OK}✓${C_END} $*"; }
warn()  { echo -e "${C_WARN}⚠${C_END} $*" >&2; }
die()   { echo -e "${C_ERR}✖${C_END} $*" >&2; exit 1; }

# ------------------------------------------------------------------------------
# URL helpers
# ------------------------------------------------------------------------------
norm_url() {
  local u
  u="$(printf "%s" "${1:-}" | tr '[:upper:]' '[:lower:]')"
  u="${u%/}"
  printf "%s" "$u"
}

PRIMARY_URL="${GPT_REVIEW_LOGIN_URL:-https://chatgpt.com/}"
FALLBACK_URL="https://chat.openai.com/"
SAME_AS_FALLBACK=0
[[ "$(norm_url "$PRIMARY_URL")" == "$(norm_url "$FALLBACK_URL")" ]] && SAME_AS_FALLBACK=1

# ------------------------------------------------------------------------------
# Determine profile directory (with Snap‑aware defaults)
# ------------------------------------------------------------------------------
DEFAULT_PROFILE="$HOME/.cache/gpt-review/chrome"
USER_SET_PROFILE="${GPT_REVIEW_PROFILE+x}"   # defined -> user explicitly set
PROFILE_DIR="${GPT_REVIEW_PROFILE:-$DEFAULT_PROFILE}"

# ------------------------------------------------------------------------------
# Detect browser binary
# ------------------------------------------------------------------------------
detect_browser() {
  # 1) Explicit override
  if [[ -n "${CHROME_BIN:-}" && -x "${CHROME_BIN:-/nonexistent}" ]]; then
    echo "$CHROME_BIN"; return 0
  fi

  # 2) Common PATH candidates (Linux/WSL)
  for candidate in google-chrome-stable google-chrome chromium chromium-browser chrome; do
    if command -v "$candidate" >/dev/null 2>&1; then
      echo "$(command -v "$candidate")"; return 0
    fi
  done

  # 3) macOS app bundles – return direct binary if present
  if [[ "$(uname -s)" == "Darwin" ]]; then
    local mac_bins=(
      "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
      "/Applications/Chromium.app/Contents/MacOS/Chromium"
    )
    for p in "${mac_bins[@]}"; do
      if [[ -x "$p" ]]; then
        echo "$p"; return 0
      fi
    done
  fi

  echo ""; return 1
}

BROWSER_BIN="$(detect_browser || true)"
IS_LINUX=0; [[ "$(uname -s)" == "Linux" ]] && IS_LINUX=1
IS_ROOT=0;  [[ "${EUID:-$(id -u)}" -eq 0 ]] && IS_ROOT=1
IS_SNAP=0;  [[ "$BROWSER_BIN" == *"/snap/"* ]] && IS_SNAP=1

# ------------------------------------------------------------------------------
# Linux DISPLAY sanity (headless login is impossible)
# ------------------------------------------------------------------------------
if [[ $IS_LINUX -eq 1 ]]; then
  if [[ -z "${DISPLAY:-}" ]]; then
    die "No DISPLAY found. Login requires a visible browser.
Hints:
  • macOS: install XQuartz, log out/in, run:  xhost +localhost ; ssh -Y user@server
  • Or start a virtual display:  Xvfb :99 &  export DISPLAY=:99  (use x11vnc/Fluxbox for VNC)
  • Or run this on a machine with a desktop session."
  fi
fi

# ------------------------------------------------------------------------------
# Snap Chromium profile quirk handling
# ------------------------------------------------------------------------------
if [[ $IS_SNAP -eq 1 && $IS_LINUX -eq 1 ]]; then
  # Snap apps are confined and often cannot write to $HOME/.cache/...
  if [[ -z "$USER_SET_PROFILE" && "$PROFILE_DIR" == "$DEFAULT_PROFILE" ]]; then
    # Auto-switch to a snap‑writable path (do not override explicit user choice)
    PROFILE_DIR="$HOME/snap/chromium/current/gpt-review-profile"
    warn "Using Snap Chromium – switching profile to: $PROFILE_DIR"
  elif [[ "$PROFILE_DIR" == "$HOME/."* || "$PROFILE_DIR" == "$HOME/.cache/"* ]]; then
    warn "Snap Chromium may not write to: $PROFILE_DIR
→ Consider:  export GPT_REVIEW_PROFILE=\"\$HOME/snap/chromium/current/gpt-review-profile\""
  fi
fi

mkdir -p "$PROFILE_DIR"

# ------------------------------------------------------------------------------
# Compose URL args
# ------------------------------------------------------------------------------
URL_ARGS=("$PRIMARY_URL")
[[ $SAME_AS_FALLBACK -eq 0 ]] && URL_ARGS+=("$FALLBACK_URL")

# ------------------------------------------------------------------------------
# Root sandbox note (Chrome/Chromium refuse to run as root without it)
# ------------------------------------------------------------------------------
EXTRA_ARGS=()
if [[ $IS_ROOT -eq 1 ]]; then
  EXTRA_ARGS+=("--no-sandbox")
fi

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
if [[ $IS_LINUX -eq 1 ]]; then
  echo "• DISPLAY        : ${DISPLAY:-<unset>}"
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
# Launch browser (platform‑aware with diagnostics)
# ------------------------------------------------------------------------------
TMP_LOG="$(mktemp -t gpt-review-browser-XXXXXX.log || echo "/tmp/gpt-review-browser.log")"

if [[ -n "$BROWSER_BIN" ]]; then
  info "Launching: $BROWSER_BIN"
  # Launch tabs. Capture logs for troubleshooting and run in background.
  # shellcheck disable=SC2086
  nohup "$BROWSER_BIN" \
    --user-data-dir="$PROFILE_DIR" \
    --window-size=1200,900 \
    --no-first-run \
    --no-default-browser-check \
    ${EXTRA_ARGS[@]+"${EXTRA_ARGS[@]}"} \
    "${URL_ARGS[@]}" \
    >"$TMP_LOG" 2>&1 &

  PID=$!
  # Give it a moment; if it dies immediately, show logs.
  sleep 2
  if ! ps -p "$PID" >/dev/null 2>&1; then
    warn "Browser exited immediately. Recent log:"
    echo "────────────────────  $TMP_LOG  ────────────────────"
    tail -n 80 "$TMP_LOG" || true
    echo "────────────────────────────────────────────────────"
    die "Unable to start a visible browser. See hints above for XQuartz/Xvfb/VNC."
  fi

  # Wait until the user closes the window.
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
    read -r -p "Press <Enter> once you have finished logging in and closed the browser…" _
  else
    die "Chrome/Chromium not found. Install Google Chrome or set CHROME_BIN."
  fi
fi

echo
echo "───────────────────────────────────────────────────────────────────────────"
ok "Cookies saved. You may now run:"
echo "   software_review.sh instructions.txt /repo"
echo "───────────────────────────────────────────────────────────────────────────"
