#!/usr/bin/env bash
###############################################################################
# GPT‑Review ▸ One‑shot installer  (Ubuntu 22.04 + Debian derivatives)
###############################################################################
#
# What it does
# ------------
# 1) Installs prerequisite **system packages** (Python, Git, Chromium, etc.)
# 2) Clones / updates GPT‑Review to /opt/gpt-review   (override via $REPO_DIR)
# 3) Creates a **Python virtual‑environment** under   /opt/gpt-review/venv
# 4) Installs the package in *editable* mode (`pip install -e .`)
# 5) Symlinks:
#       • gpt-review           → Python entry‑point
#       • software_review.sh   → convenience Bash wrapper
#
# Idempotent – Safe to run multiple times.
#
# Example
# -------
#   curl -sSL https://raw.githubusercontent.com/your‑org/gpt-review/main/install.sh \
#     | sudo bash
###############################################################################

set -euo pipefail

# ---------------------------------------------------------------------------
# Pretty colours (fallback to plain if not TTY)
# ---------------------------------------------------------------------------
if [[ -t 1 ]]; then
  GREEN='\e[32m'; YELLOW='\e[33m'; BLUE='\e[34m'; RESET='\e[0m'
else
  GREEN=''; YELLOW=''; BLUE=''; RESET=''
fi
info()  { echo -e "${BLUE}▶︎${RESET} $*"; }
warn()  { echo -e "${YELLOW}⚠${RESET} $*" >&2; }
fatal() { echo -e "${YELLOW}✖${RESET} $*" >&2; exit 1; }

# ---------------------------------------------------------------------------
# Require root
# ---------------------------------------------------------------------------
[[ $EUID -eq 0 ]] || fatal "Please run as root (use sudo)."

# ---------------------------------------------------------------------------
# Variables (override by exporting before running)
# ---------------------------------------------------------------------------
REPO_URL="${REPO_URL:-https://github.com/your-org/gpt-review.git}"
REPO_DIR="${REPO_DIR:-/opt/gpt-review}"
VENV_DIR="${VENV_DIR:-$REPO_DIR/venv}"
WRAPPER_BIN="/usr/local/bin/gpt-review"
SHELL_WRAPPER="/usr/local/bin/software_review.sh"

info "Installing GPT‑Review into $REPO_DIR"

# ---------------------------------------------------------------------------
# System dependencies
# ---------------------------------------------------------------------------
info "Installing system packages …"
apt-get update -y
apt-get install -y --no-install-recommends \
  python3 python3-venv python3-pip git curl wget unzip jq build-essential \
  chromium-browser xvfb || \
apt-get install -y --no-install-recommends chromium

# ---------------------------------------------------------------------------
# Clone or update repository
# ---------------------------------------------------------------------------
if [[ -d "$REPO_DIR/.git" ]]; then
  info "Repository exists – pulling latest changes"
  git -C "$REPO_DIR" pull --ff-only
else
  git clone "$REPO_URL" "$REPO_DIR"
fi

# ---------------------------------------------------------------------------
# Python virtual‑environment
# ---------------------------------------------------------------------------
info "Creating / Updating virtualenv at $VENV_DIR …"
python3 -m venv "$VENV_DIR"
# shellcheck source=/dev/null
source "$VENV_DIR/bin/activate"
pip install -q --upgrade pip
info "Installing GPT‑Review + deps (this can take a minute) …"
pip install -q -e "$REPO_DIR"
deactivate

# ---------------------------------------------------------------------------
# Wrapper script – Python entry‑point
# ---------------------------------------------------------------------------
info "Linking CLI wrapper → $WRAPPER_BIN"
cat >"$WRAPPER_BIN" <<EOF
#!/usr/bin/env bash
source "$VENV_DIR/bin/activate"
exec python "$REPO_DIR/review.py" "\$@"
EOF
chmod +x "$WRAPPER_BIN"

# ---------------------------------------------------------------------------
# Wrapper script – Bash helper
# ---------------------------------------------------------------------------
info "Linking Bash helper  → $SHELL_WRAPPER"
ln -sf "$REPO_DIR/software_review.sh" "$SHELL_WRAPPER"
chmod +x "$SHELL_WRAPPER"

# ---------------------------------------------------------------------------
# Done!
# ---------------------------------------------------------------------------
echo -e "${GREEN}✓${RESET} GPT‑Review installed successfully."
echo "Run   :  software_review.sh --help"
echo "Update:  sudo $0   # rerun this script anytime"
echo
