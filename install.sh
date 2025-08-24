#!/usr/bin/env bash
###############################################################################
# GPT‑Review ▸ One‑shot installer (Debian/Ubuntu & derivatives)
###############################################################################
#
# What it does (idempotent; safe to rerun)
# ----------------------------------------
# 1) Installs prerequisite **system packages** (Python, Git, Chrome/Chromium,
#    Xvfb, etc.)
# 2) Clones or updates GPT‑Review to /opt/gpt-review   (override via $REPO_DIR)
# 3) Creates a **Python virtual‑environment** at        /opt/gpt-review/venv
# 4) Installs the package in *editable* mode (pip install -e .)
# 5) Creates three launchers:
#       • /usr/local/bin/gpt-review         → console script from the venv
#       • /usr/local/bin/software_review.sh → convenience Bash wrapper
#       • /usr/local/bin/cookie_login.sh    → visible browser login helper
#
# New in this revision
# --------------------
# • Optional non‑snap **Google Chrome** install when INSTALL_GOOGLE_CHROME=1.
# • Clear guidance for Snap Chromium vs non‑snap Chrome.
# • No need to create a root wrapper: cookie_login.sh auto‑adds --no-sandbox.
#
# Usage
# -----
#   curl -sSL https://raw.githubusercontent.com/bekirdag/gpt_review/main/install.sh \
#     | sudo INSTALL_GOOGLE_CHROME=1 bash
#
###############################################################################

set -euo pipefail

# ────────────────────────────────────────────────────────────────────────────
# Pretty colours (fallback to plain if not TTY)
# ────────────────────────────────────────────────────────────────────────────
if [[ -t 1 ]]; then
  GREEN='\e[32m'; YELLOW='\e[33m'; BLUE='\e[34m'; RED='\e[31m'; RESET='\e[0m'
else
  GREEN=''; YELLOW=''; BLUE=''; RED=''; RESET=''
fi
info()  { echo -e "${BLUE}▶︎${RESET} $*"; }
warn()  { echo -e "${YELLOW}⚠${RESET} $*" >&2; }
ok()    { echo -e "${GREEN}✓${RESET} $*"; }
fatal() { echo -e "${RED}✖${RESET} $*" >&2; exit 1; }

# ────────────────────────────────────────────────────────────────────────────
# Require root
# ────────────────────────────────────────────────────────────────────────────
[[ ${EUID:-$(id -u)} -eq 0 ]] || fatal "Please run as root (use sudo)."

# ────────────────────────────────────────────────────────────────────────────
# Variables (override by exporting before running)
# ────────────────────────────────────────────────────────────────────────────
REPO_URL="${REPO_URL:-https://github.com/bekirdag/gpt_review.git}"
REPO_DIR="${REPO_DIR:-/opt/gpt-review}"
VENV_DIR="${VENV_DIR:-$REPO_DIR/venv}"
INSTALL_GOOGLE_CHROME="${INSTALL_GOOGLE_CHROME:-0}"

WRAPPER_BIN="/usr/local/bin/gpt-review"             # console entrypoint
WRAPPER_SH="/usr/local/bin/software_review.sh"      # thin bash helper
LOGIN_HELPER="/usr/local/bin/cookie_login.sh"       # visible login helper

info "Installing GPT‑Review into $REPO_DIR"

# ────────────────────────────────────────────────────────────────────────────
# System packages
# ────────────────────────────────────────────────────────────────────────────
info "Installing system packages …"
apt-get update -y
# Base tools
apt-get install -y --no-install-recommends \
  python3 python3-venv python3-pip git curl wget unzip ca-certificates \
  gnupg xvfb

# ────────────────────────────────────────────────────────────────────────────
# Browser: Google Chrome (optional) or Chromium (fallback)
# ────────────────────────────────────────────────────────────────────────────
arch="$(dpkg --print-architecture || echo amd64)"
if [[ "$INSTALL_GOOGLE_CHROME" == "1" ]]; then
  if [[ "$arch" != "amd64" ]]; then
    warn "INSTALL_GOOGLE_CHROME=1 requested, but arch is '$arch' (Google Chrome apt repo is amd64). Skipping Chrome install."
  else
    info "Adding Google Chrome APT repository (amd64) …"
    wget -qO- https://dl.google.com/linux/linux_signing_key.pub | gpg --dearmor > /usr/share/keyrings/google-linux.gpg
    echo "deb [arch=amd64 signed-by=/usr/share/keyrings/google-linux.gpg] http://dl.google.com/linux/chrome/deb/ stable main" > /etc/apt/sources.list.d/google-chrome.list
    apt-get update -y
    info "Installing Google Chrome Stable …"
    if apt-get install -y --no-install-recommends google-chrome-stable; then
      ok "Installed google-chrome-stable"
    else
      warn "Failed to install google-chrome-stable. Falling back to Chromium."
    fi
  fi
fi

# Fallback to Chromium if Chrome not present
if ! command -v google-chrome >/dev/null 2>&1; then
  if ! command -v chromium >/dev/null 2>&1 && ! command -v chromium-browser >/dev/null 2>&1; then
    info "Installing Chromium (APT) …"
    if apt-get install -y --no-install-recommends chromium; then
      ok "Installed chromium"
    elif apt-get install -y --no-install-recommends chromium-browser; then
      ok "Installed chromium-browser"
    else
      warn "Could not install Chromium via APT. You can install Chrome/Chromium later."
      warn "If installed in a non-default location, set CHROME_BIN=/path/to/browser"
    fi
  else
    ok "Chromium already present on system PATH"
  fi
else
  ok "Google Chrome detected on PATH"
fi

# ────────────────────────────────────────────────────────────────────────────
# Clone or update repository
# ────────────────────────────────────────────────────────────────────────────
if [[ -d "$REPO_DIR/.git" ]]; then
  info "Repository exists – pulling latest changes"
  git -C "$REPO_DIR" pull --ff-only
else
  info "Cloning repository …"
  git clone "$REPO_URL" "$REPO_DIR"
fi

# ────────────────────────────────────────────────────────────────────────────
# Python virtual‑environment & package install
# ────────────────────────────────────────────────────────────────────────────
info "Creating / Updating virtualenv at $VENV_DIR …"
python3 -m venv "$VENV_DIR"
# shellcheck source=/dev/null
source "$VENV_DIR/bin/activate"
python -m pip install --upgrade pip
info "Installing GPT‑Review (editable mode) …"
pip install -e "$REPO_DIR"
deactivate
ok "Virtualenv ready"

# ────────────────────────────────────────────────────────────────────────────
# Launchers
# ────────────────────────────────────────────────────────────────────────────
# Console script – symlink the venv entrypoint
info "Linking CLI → $WRAPPER_BIN"
ln -sf "$VENV_DIR/bin/gpt-review" "$WRAPPER_BIN"
chmod +x "$WRAPPER_BIN"

# Bash helper – thin wrapper for convenience
info "Linking Bash helper → $WRAPPER_SH"
ln -sf "$REPO_DIR/software_review.sh" "$WRAPPER_SH"
chmod +x "$WRAPPER_SH"

# Visible login helper – opens a real browser to save cookies
info "Linking login helper → $LOGIN_HELPER"
ln -sf "$REPO_DIR/cookie_login.sh" "$LOGIN_HELPER"
chmod +x "$LOGIN_HELPER"

# ────────────────────────────────────────────────────────────────────────────
# Final hints
# ────────────────────────────────────────────────────────────────────────────
ok "GPT‑Review installed successfully."
echo "Run   :  software_review.sh --help"
echo "Login :  cookie_login.sh    # opens a visible browser to save cookies"
echo "Update:  sudo $0            # rerun this script anytime"
echo
echo "Tips:"
echo "  • If your browser isn’t auto-detected, export CHROME_BIN=/usr/bin/google-chrome (or /usr/bin/chromium)"
echo "  • For CI/servers, set GPT_REVIEW_HEADLESS=1 (headless runs after cookies exist)"
echo "  • On Snap Chromium, prefer a snap‑writable profile:"
echo "        export GPT_REVIEW_PROFILE=\"\$HOME/snap/chromium/current/gpt-review-profile\""
echo "  • To change the login domain for the helper, set GPT_REVIEW_LOGIN_URL"
echo "        (default: https://chatgpt.com/ ; fallback: https://chat.openai.com/)"
