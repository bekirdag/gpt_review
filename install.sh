#!/usr/bin/env bash
###############################################################################
# GPT‑Review ▸ Installer (Debian/Ubuntu)
###############################################################################
#
# This script installs GPT‑Review system‑wide:
#   • Installs OS dependencies (Python, Git, Chromium or Google Chrome, Xvfb)
#   • Clones the repository (default: /opt/gpt-review)
#   • Creates a Python virtual‑env and installs the package
#   • Installs convenient launchers in /usr/local/bin:
#       - gpt-review           → runs the Python CLI
#       - software_review.sh   → thin wrapper (browser/API mode)
#       - cookie_login.sh      → first‑time visible login helper (if present)
#       - gpt-review-update    → pull & reinstall on demand
#
# Usage (run as root or via sudo):
#   curl -sSL https://raw.githubusercontent.com/bekirdag/gpt_review/main/install.sh | \
#     sudo INSTALL_GOOGLE_CHROME=1 bash -s -- [options]
#
# Options (flags after `--`):
#   -d, --dir PATH        Install directory (default: /opt/gpt-review)
#   -b, --branch NAME     Git branch to checkout (default: main)
#   -f, --force           Force reset local changes when updating an existing clone
#       --no-dev          Do not install dev extras (pre-commit, pytest, etc.)
#
# Environment toggles:
#   INSTALL_GOOGLE_CHROME=1  Install Google Chrome Stable (recommended on Ubuntu)
#   INSTALL_DEV=1            Install dev extras regardless of --no-dev
#   SKIP_BROWSER=1           Skip installing any browser (use API mode only)
#
# After install, try:
#   software_review.sh example_instructions.txt /path/to/repo --cmd "pytest -q" --auto
#
###############################################################################
set -euo pipefail
IFS=$'\n\t'

# --------------------------------- helpers --------------------------------- #
C_INFO=$'\e[34m'; C_OK=$'\e[32m'; C_WARN=$'\e[33m'; C_ERR=$'\e[31m'; C_END=$'\e[0m'
_ts() { date "+%Y-%m-%d %H:%M:%S"; }
info()  { echo -e "${C_INFO}[$(_ts)] INFO ${C_END}$*"; }
ok()    { echo -e "${C_OK}[$(_ts)] OK   ${C_END}$*"; }
warn()  { echo -e "${C_WARN}[$(_ts)] WARN ${C_END}$*" >&2; }
error() { echo -e "${C_ERR}[$(_ts)] ERROR${C_END} $*" >&2; }
die()   { error "$@"; exit 1; }

require_root() {
  if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
    cat >&2 <<'EOF'
This installer needs root privileges.

Re-run with:   sudo bash install.sh  (or use the curl | sudo bash pattern)
EOF
    exit 1
  fi
}

on_debian_like() {
  if [[ -f /etc/os-release ]]; then
    . /etc/os-release
    case "${ID_LIKE:-$ID}" in
      *debian*|*ubuntu*|ubuntu|debian) return 0 ;;
    esac
  fi
  return 1
}

apt_install() {
  local -a pkgs=("$@")
  DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends "${pkgs[@]}"
}

ensure_cmd() {
  command -v "$1" >/dev/null 2>&1
}

write_launcher() {
  local path="$1"
  local body="$2"
  echo "#!/usr/bin/env bash" > "$path"
  echo "set -euo pipefail" >> "$path"
  printf "%s\n" "$body" >> "$path"
  chmod 0755 "$path"
  ok "Installed launcher: $path"
}

# -------------------------------- defaults -------------------------------- #
REPO_DIR="/opt/gpt-review"
BRANCH="main"
FORCE_RESET=0
NO_DEV_OPT=0
INSTALL_DEV_ENV="${INSTALL_DEV:-0}"  # environment override, default off

# ------------------------------- parse flags ------------------------------- #
while [[ $# -gt 0 ]]; do
  case "$1" in
    -d|--dir)
      REPO_DIR="${2:-}"; [[ -n "$REPO_DIR" ]] || die "--dir requires a path"
      shift 2 ;;
    --dir=*)
      REPO_DIR="${1#*=}"; shift ;;
    -b|--branch)
      BRANCH="${2:-}"; [[ -n "$BRANCH" ]] || die "--branch requires a name"
      shift 2 ;;
    --branch=*)
      BRANCH="${1#*=}"; shift ;;
    -f|--force)
      FORCE_RESET=1; shift ;;
    --no-dev)
      NO_DEV_OPT=1; shift ;;
    *)
      die "Unknown option: $1" ;;
  esac
done

require_root

# Decide final dev‑extras setting now (for accurate logging and behavior)
# Priority: INSTALL_DEV=1 env → dev ON; otherwise --no-dev disables; default OFF.
if [[ "$INSTALL_DEV_ENV" == "1" ]]; then
  INSTALL_DEV_FINAL=1
else
  if [[ $NO_DEV_OPT -eq 1 ]]; then
    INSTALL_DEV_FINAL=0
  else
    INSTALL_DEV_FINAL=0
  fi
fi

info "Install directory : ${REPO_DIR}"
info "Git branch        : ${BRANCH}"
info "Force reset       : $([[ $FORCE_RESET -eq 1 ]] && echo yes || echo no)"
info "Install dev extras: $([[ $INSTALL_DEV_FINAL -eq 1 ]] && echo yes || echo no)"
info "Google Chrome     : $([[ ${INSTALL_GOOGLE_CHROME:-0} -eq 1 ]] && echo 'install' || echo 'skip')"
info "Skip browser      : $([[ ${SKIP_BROWSER:-0} -eq 1 ]] && echo yes || echo no)"

# --------------------------- OS prerequisites ------------------------------ #
if ! on_debian_like; then
  warn "This installer targets Debian/Ubuntu. Proceeding may fail on other distros."
fi

info "Updating apt metadata …"
apt-get update -y

info "Installing base packages …"
apt_install ca-certificates curl git python3 python3-venv python3-pip

# Xvfb is useful for visible login sessions on headless servers (best-effort)
apt_install xvfb >/dev/null 2>&1 || true

# ------------------------------ Browser setup ------------------------------ #
if [[ "${SKIP_BROWSER:-0}" -ne 1 ]]; then
  if [[ "${INSTALL_GOOGLE_CHROME:-0}" -eq 1 ]]; then
    info "Setting up Google Chrome Stable APT repository …"
    apt_install wget gpg
    if [[ ! -f /usr/share/keyrings/google-linux.gpg ]]; then
      wget -qO- https://dl.google.com/linux/linux_signing_key.pub \
        | gpg --dearmor \
        > /usr/share/keyrings/google-linux.gpg
    fi
    echo "deb [arch=amd64 signed-by=/usr/share/keyrings/google-linux.gpg] http://dl.google.com/linux/chrome/deb/ stable main" \
      > /etc/apt/sources.list.d/google-chrome.list
    apt-get update -y
    info "Installing Google Chrome Stable …"
    apt_install google-chrome-stable || warn "Failed to install Google Chrome; continuing."
  else
    info "Installing Chromium (fallback) …"
    apt_install chromium || apt_install chromium-browser || warn "Chromium not available; continuing."
  fi
else
  info "Skipping browser installation (API‑only usage requested)."
fi

# ---------------------------- Clone / update repo -------------------------- #
if [[ -d "$REPO_DIR/.git" ]]; then
  info "Repository already exists: $REPO_DIR"
  pushd "$REPO_DIR" >/dev/null
  git fetch --all --tags
  if [[ $FORCE_RESET -eq 1 ]]; then
    warn "Forcing reset to origin/${BRANCH}"
    git reset --hard "origin/${BRANCH}"
    git checkout -q "${BRANCH}" || git checkout -b "${BRANCH}" "origin/${BRANCH}"
    git pull --ff-only || true
  else
    git checkout -q "${BRANCH}" || git checkout -b "${BRANCH}" "origin/${BRANCH}" || true
    git pull --ff-only || true
  fi
  popd >/dev/null
else
  info "Cloning repository → $REPO_DIR"
  mkdir -p "$(dirname "$REPO_DIR")"
  git clone --branch "${BRANCH}" --depth 1 "https://github.com/bekirdag/gpt_review.git" "$REPO_DIR"
fi

# ------------------------------ Python venv -------------------------------- #
VENV="${REPO_DIR}/venv"
if [[ ! -d "$VENV" ]]; then
  info "Creating virtual environment → $VENV"
  python3 -m venv "$VENV"
fi

# shellcheck disable=SC1090
. "${VENV}/bin/activate"
python -m pip install --upgrade pip wheel setuptools

info "Installing GPT‑Review package ($([[ $INSTALL_DEV_FINAL -eq 1 ]] && echo 'with dev extras' || echo 'core only')) …"
if [[ $INSTALL_DEV_FINAL -eq 1 ]]; then
  # If extras aren't defined, fall back to core install gracefully.
  pip install -e "${REPO_DIR}[dev]" || pip install -e "${REPO_DIR}"
else
  pip install -e "${REPO_DIR}"
fi

# Basic sanity check
info "Verifying CLI import …"
python - <<'PY'
import sys
try:
    import gpt_review  # noqa: F401
except Exception as exc:
    print("Import failed:", exc, file=sys.stderr)
    sys.exit(1)
print("ok")
PY
ok "Python package import OK."

# --------------------------- Install launchers ----------------------------- #
BIN_DIR="/usr/local/bin"
mkdir -p "$BIN_DIR"

# gpt-review launcher – always points to the venv’s Python
write_launcher "${BIN_DIR}/gpt-review" \
"VENV='${VENV}'
exec \"\${VENV}/bin/python\" -m gpt_review \"\$@\""

# software_review.sh – symlink to repo version (keeps latest features)
if [[ -f "${REPO_DIR}/software_review.sh" ]]; then
  ln -sf "${REPO_DIR}/software_review.sh" "${BIN_DIR}/software_review.sh"
  chmod 0755 "${REPO_DIR}/software_review.sh"
  ok "Linked: ${BIN_DIR}/software_review.sh → ${REPO_DIR}/software_review.sh"
else
  warn "software_review.sh not found in repo; skipping symlink."
fi

# cookie_login.sh – if present in repo, link it; otherwise provide a minimal helper
if [[ -f "${REPO_DIR}/cookie_login.sh" ]]; then
  ln -sf "${REPO_DIR}/cookie_login.sh" "${BIN_DIR}/cookie_login.sh"
  chmod 0755 "${REPO_DIR}/cookie_login.sh"
  ok "Linked: ${BIN_DIR}/cookie_login.sh → ${REPO_DIR}/cookie_login.sh"
else
  warn "cookie_login.sh not found in repo; installing a minimal helper."
  write_launcher "${BIN_DIR}/cookie_login.sh" \
'LOGIN_URL="${GPT_REVIEW_LOGIN_URL:-https://chatgpt.com/}"
FALLBACK="https://chat.openai.com/"
CHROME="${CHROME_BIN:-$(command -v google-chrome-stable || command -v google-chrome || command -v chromium || command -v chromium-browser || true)}"
if [[ -z "$CHROME" ]]; then
  echo "No Chrome/Chromium found. Set CHROME_BIN or install a browser." >&2
  exit 1
fi
echo "Opening login tabs with: $CHROME"
"$CHROME" --new-window "$LOGIN_URL" "$FALLBACK" >/dev/null 2>&1 & disown
echo "Browser started. Complete login, then close the window."
'
fi

# gpt-review-update – convenience updater
write_launcher "${BIN_DIR}/gpt-review-update" \
"set -euo pipefail
REPO_DIR='${REPO_DIR}'
VENV='${VENV}'
BRANCH='${BRANCH}'
echo 'Updating GPT‑Review in' \"\$REPO_DIR\"
cd \"\$REPO_DIR\"
git fetch --all --tags
git checkout -q \"\$BRANCH\" || git checkout -b \"\$BRANCH\" \"origin/\$BRANCH\" || true
git pull --ff-only || true
\"\$VENV/bin/python\" -m pip install --upgrade pip wheel setuptools
# Reinstall in editable mode to pick up changes (fallback to core if extras missing)
\"\$VENV/bin/pip\" install -e .[dev] || \"\$VENV/bin/pip\" install -e .
echo 'Done.'"

# ------------------------------- Post‑install ------------------------------- #
# Try to detect browser version (best‑effort)
if ensure_cmd google-chrome-stable; then
  CHROME_BIN_DETECTED="$(command -v google-chrome-stable)"
elif ensure_cmd google-chrome; then
  CHROME_BIN_DETECTED="$(command -v google-chrome)"
elif ensure_cmd chromium; then
  CHROME_BIN_DETECTED="$(command -v chromium)"
elif ensure_cmd chromium-browser; then
  CHROME_BIN_DETECTED="$(command -v chromium-browser)"
else
  CHROME_BIN_DETECTED=""
fi

if [[ -n "$CHROME_BIN_DETECTED" ]]; then
  info "Detected browser: $CHROME_BIN_DETECTED"
  "$CHROME_BIN_DETECTED" --version || true
else
  warn "No Chrome/Chromium detected. API mode will still work. Install a browser for UI mode."
fi

cat <<'EOT'

─────────────────────────── Installation Complete ────────────────────────────
Launchers installed:
  • gpt-review
  • software_review.sh
  • cookie_login.sh
  • gpt-review-update

Next steps:
  1) (Optional, for browser mode) Save cookies with a visible login:
       cookie_login.sh
  2) Run a review session (browser mode):
       software_review.sh instructions.txt /path/to/repo --cmd "pytest -q" --auto
     Or API mode (no browser):
       software_review.sh instructions.txt /path/to/repo --api --model gpt-5-pro --cmd "pytest -q" --auto

Tips:
  • Configure environment in a ./.env file (see .env.example in the repo).
  • API mode requires OPENAI_API_KEY and (optionally) OPENAI_BASE_URL.

EOT

ok "All set."
