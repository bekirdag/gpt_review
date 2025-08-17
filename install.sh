#!/usr/bin/env bash
###############################################################################
# GPT‑Review ▸ One‑shot installer  (Debian/Ubuntu & derivatives)
###############################################################################
#
# What it does (idempotent; safe to rerun)
# ----------------------------------------
# 1) Installs prerequisite **system packages** (Python, Git, Chromium/Chrome,
#    Xvfb, etc.)
# 2) Clones or updates GPT‑Review to /opt/gpt-review   (override via $REPO_DIR)
# 3) Creates a **Python virtual‑environment** at        /opt/gpt-review/venv
# 4) Installs the package in *editable* mode (pip install -e .)
# 5) Creates three launchers:
#       • /usr/local/bin/gpt-review         → console script from the venv
#       • /usr/local/bin/software_review.sh → convenience Bash wrapper
#       • /usr/local/bin/cookie_login.sh    → visible browser login helper
#
# Usage
# -----
#   curl -sSL https://raw.githubusercontent.com/your-org/gpt-review/main/install.sh \
#     | sudo bash
#
# Notes
# -----
# • We try to install **chromium** (APT) automatically; if it’s unavailable,
#   we fall back to **chromium-browser**. If neither is available, we warn and
#   you may install Chrome/Chromium manually; GPT‑Review can still run if a
#   browser is already present on PATH or set via CHROME_BIN.
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
[[ $EUID -eq 0 ]] || fatal "Please run as root (use sudo)."

# ────────────────────────────────────────────────────────────────────────────
# Variables (override by exporting before running)
# ────────────────────────────────────────────────────────────────────────────
REPO_URL="${REPO_URL:-https://github.com/your-org/gpt-review.git}"
REPO_DIR="${REPO_DIR:-/opt/gpt-review}"
VENV_DIR="${VENV_DIR:-$REPO_DIR/venv}"

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
  xvfb

# Best effort browser install (Chromium from APT)
if ! command -v chromium >/dev/null 2>&1 && ! command -v chromium-browser >/dev/null 2>&1 && ! command -v google-chrome >/dev/null 2>&1; then
  if apt-get install -y --no-install-recommends chromium; then
    ok "Installed chromium"
  elif apt-get install -y --no-install-recommends chromium-browser; then
    ok "Installed chromium-browser"
  else
    warn "Could not install Chromium via APT. You can install Chrome/Chromium manually later."
    warn "If installed in a non-default location, set CHROME_BIN=/path/to/browser"
  fi
else
  ok "Browser already present on system PATH"
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
echo "  • If your browser isn’t auto-detected, export CHROME_BIN=/path/to/chrome"
echo "  • For CI/servers, set GPT_REVIEW_HEADLESS=1"
echo "  • To change the login domain for the helper, set GPT_REVIEW_LOGIN_URL"
echo "      (default: https://chatgpt.com/ ; fallback tab: https://chat.openai.com/)"

