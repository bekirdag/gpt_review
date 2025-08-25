#!/usr/bin/env bash
###############################################################################
# GPT‑Review ▸ Update Helper
###############################################################################
#
# Safely update a local installation of GPT‑Review:
#   • Pull latest from GitHub (branch: main by default)
#   • Optionally discard local changes with --force
#   • (Re)create virtual‑env and reinstall the package
#   • Refresh launchers in /usr/local/bin:
#       - gpt-review            → venv Python driver
#       - software_review.sh    → thin wrapper (browser/API)
#       - cookie_login.sh       → visible login helper (if present)
#       - gpt-review-update     → fetch & run this script next time
#
# Usage:
#   curl -sSL https://raw.githubusercontent.com/bekirdag/gpt_review/main/update.sh | sudo bash -s -- [flags]
#   # or (after a prior install to /opt/gpt-review):
#   sudo /opt/gpt-review/update.sh [flags]
#
# Flags:
#   --repo-dir DIR     Target repo directory (default: /opt/gpt-review)
#   --branch NAME      Git branch to track (default: main)
#   --force            Discard local changes (git reset --hard + clean -fd)
#   --dev              Install with dev extras (equivalent to INSTALL_DEV=1)
#   --no-dev           Install without dev extras (default)
#
# Environment overrides:
#   REPO_URL           Git remote (default: https://github.com/bekirdag/gpt_review.git)
#   PYTHON             Python interpreter for venv (default: python3)
#   VENV_DIR           Virtual‑env directory (default: $REPO_DIR/venv)
#   BIN_DIR            Launcher directory (default: /usr/local/bin)
#   INSTALL_DEV        1 to force dev extras, 0 to skip (overridden by --dev/--no-dev)
#
###############################################################################
set -euo pipefail
IFS=$'\n\t'

# ------------------------------ pretty logging ------------------------------ #
if [[ -t 1 ]]; then
  C_INFO=$'\e[34m'; C_OK=$'\e[32m'; C_WARN=$'\e[33m'; C_ERR=$'\e[31m'; C_END=$'\e[0m'
else
  C_INFO=""; C_OK=""; C_WARN=""; C_ERR=""; C_END=""
fi
_ts() { date '+%Y-%m-%d %H:%M:%S'; }
info()  { echo -e "${C_INFO}[$(_ts)] INFO ${C_END}$*"; }
ok()    { echo -e "${C_OK}[$(_ts)] OK   ${C_END}$*"; }
warn()  { echo -e "${C_WARN}[$(_ts)] WARN ${C_END}$*" >&2; }
error() { echo -e "${C_ERR}[$(_ts)] ERROR${C_END} $*" >&2; }
die()   { error "$@"; exit 1; }

require_root() {
  if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
    cat >&2 <<'EOF'
This updater needs root privileges to refresh launchers in /usr/local/bin.

Re-run with:   sudo bash update.sh   (or use the curl | sudo bash pattern)
EOF
    exit 1
  fi
}

# ----------------------------- defaults & flags ----------------------------- #
REPO_URL="${REPO_URL:-https://github.com/bekirdag/gpt_review.git}"
REPO_DIR="/opt/gpt-review"
BRANCH="main"
FORCE=0
DEV_CLI=""  # "yes" or "no" when set via flags

PYTHON="${PYTHON:-python3}"
VENV_DIR_DEFAULT="\$REPO_DIR/venv"  # keep literal for help text; we resolve later
BIN_DIR="${BIN_DIR:-/usr/local/bin}"

# Parse args
while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo-dir) REPO_DIR="${2:-}"; [[ -n "$REPO_DIR" ]] || die "--repo-dir needs a path"; shift 2 ;;
    --repo-dir=*) REPO_DIR="${1#*=}"; shift ;;
    --branch) BRANCH="${2:-}"; [[ -n "$BRANCH" ]] || die "--branch needs a name"; shift 2 ;;
    --branch=*) BRANCH="${1#*=}"; shift ;;
    --force) FORCE=1; shift ;;
    --dev) DEV_CLI="yes"; shift ;;
    --no-dev) DEV_CLI="no"; shift ;;
    -h|--help)
      sed -n '1,120p' "$0" | sed 's/^# \{0,1\}//'
      exit 0 ;;
    *) die "Unknown argument: $1 (use --help)";;
  esac
done

require_root

# Resolve VENV_DIR now that REPO_DIR is final
VENV_DIR="${VENV_DIR:-$REPO_DIR/venv}"

# Decide dev extras
if [[ -n "$DEV_CLI" ]]; then
  INSTALL_DEV=$([[ "$DEV_CLI" == "yes" ]] && echo 1 || echo 0)
else
  INSTALL_DEV=${INSTALL_DEV:-0}
fi

info "Repo URL        : ${REPO_URL}"
info "Target dir      : ${REPO_DIR}"
info "Branch          : ${BRANCH}"
info "Force reset     : $([[ $FORCE -eq 1 ]] && echo yes || echo no)"
info "Install dev     : $([[ $INSTALL_DEV -eq 1 ]] && echo yes || echo no)"
info "Python          : ${PYTHON}"
info "Virtual‑env dir : ${VENV_DIR}"
info "Launchers dir   : ${BIN_DIR}"

# ------------------------------ prereq checks ------------------------------- #
require_cmd() { command -v "$1" >/dev/null 2>&1 || die "Missing dependency: $1"; }
require_cmd git
require_cmd "$PYTHON"

mkdir -p "$REPO_DIR"

# ------------------------------ clone / update ------------------------------ #
git_in()      { git -C "$REPO_DIR" "$@"; }
is_repo()     { git -C "$REPO_DIR" rev-parse --is-inside-work-tree >/dev/null 2>&1; }
has_changes() { [[ -n "$(git -C "$REPO_DIR" status --porcelain 2>/dev/null || true)" ]]; }

if ! is_repo; then
  info "No git repo found at $REPO_DIR → cloning fresh"
  rm -rf "$REPO_DIR"/*
  git clone --branch "$BRANCH" --depth 1 "$REPO_URL" "$REPO_DIR" || die "Git clone failed"
else
  info "Fetching updates from origin …"
  git_in fetch --prune origin

  info "Checking out branch '$BRANCH' …"
  if git_in show-ref --verify --quiet "refs/heads/$BRANCH"; then
    git_in checkout -q "$BRANCH"
  else
    git_in checkout -B "$BRANCH" "origin/$BRANCH" || git_in checkout -B "$BRANCH"
  fi

  if [[ $FORCE -eq 1 ]]; then
    warn "Discarding local changes (forced reset) …"
    git_in reset --hard "origin/$BRANCH"
    git_in clean -fd
  else
    if has_changes; then
      warn "Local changes detected; attempting fast‑forward merge"
    fi
    if ! git_in merge --ff-only "origin/$BRANCH"; then
      warn "Non fast‑forward. Resetting to origin/$BRANCH …"
      git_in reset --hard "origin/$BRANCH"
      git_in clean -fd
    fi
  fi
fi

# ------------------------------- virtual‑env -------------------------------- #
if [[ ! -d "$VENV_DIR" ]]; then
  info "Creating virtual‑env → $VENV_DIR"
  "$PYTHON" -m venv "$VENV_DIR" || die "Failed to create venv"
fi
# shellcheck disable=SC1090
. "$VENV_DIR/bin/activate"

info "Upgrading pip/setuptools/wheel …"
python -m pip install --upgrade --quiet pip setuptools wheel

# Install (editable) with or without dev extras
if [[ $INSTALL_DEV -eq 1 ]]; then
  info "Installing package with dev extras (editable) …"
  pip install -e "$REPO_DIR[dev]"
else
  info "Installing package (editable) …"
  pip install -e "$REPO_DIR"
fi

# Quick import sanity
python - <<'PY'
import sys
try:
    import gpt_review  # noqa
except Exception as exc:
    print("Import failed:", exc, file=sys.stderr); sys.exit(1)
print("ok")
PY
ok "Python package import OK."

# ------------------------------- launchers ---------------------------------- #
write_launcher() {
  local path="$1"
  local body="$2"
  echo "#!/usr/bin/env bash" > "$path"
  echo "set -euo pipefail" >> "$path"
  printf "%s\n" "$body" >> "$path"
  chmod 0755 "$path"
}

link_file() {
  local src="$1" dst="$2"
  mkdir -p "$(dirname "$dst")"
  ln -sfn "$src" "$dst"
}

info "Refreshing launchers in $BIN_DIR …"
mkdir -p "$BIN_DIR"

# gpt-review: stable wrapper that always uses the venv interpreter
write_launcher "${BIN_DIR}/gpt-review" \
"VENV='${VENV_DIR}'
exec \"\${VENV}/bin/python\" -m gpt_review \"\$@\""
ok "Installed launcher: ${BIN_DIR}/gpt-review"

# software_review.sh → symlink to repo version
if [[ -f "${REPO_DIR}/software_review.sh" ]]; then
  chmod 0755 "${REPO_DIR}/software_review.sh" || true
  link_file "${REPO_DIR}/software_review.sh" "${BIN_DIR}/software_review.sh"
  chmod 0755 "${BIN_DIR}/software_review.sh" || true
  ok "Linked: ${BIN_DIR}/software_review.sh → ${REPO_DIR}/software_review.sh"
else
  warn "Missing ${REPO_DIR}/software_review.sh"
fi

# cookie_login.sh → symlink if present
if [[ -f "${REPO_DIR}/cookie_login.sh" ]]; then
  chmod 0755 "${REPO_DIR}/cookie_login.sh" || true
  link_file "${REPO_DIR}/cookie_login.sh" "${BIN_DIR}/cookie_login.sh"
  chmod 0755 "${BIN_DIR}/cookie_login.sh" || true
  ok "Linked: ${BIN_DIR}/cookie_login.sh → ${REPO_DIR}/cookie_login.sh"
else
  warn "Missing ${REPO_DIR}/cookie_login.sh (browser mode login helper)"
fi

# gpt-review-update → fetch & run latest update script
cat > "${BIN_DIR}/gpt-review-update" <<'SH'
#!/usr/bin/env bash
set -euo pipefail
if ! command -v curl >/dev/null 2>&1; then
  echo "curl is required" >&2; exit 1
fi
tmp="$(mktemp)"; trap 'rm -f "$tmp"' EXIT
curl -fsSL https://raw.githubusercontent.com/bekirdag/gpt_review/main/update.sh -o "$tmp"
if [ "$(id -u)" -ne 0 ]; then
  exec sudo bash "$tmp" "$@"
else
  exec bash "$tmp" "$@"
fi
SH
chmod 0755 "${BIN_DIR}/gpt-review-update"
ok "Installed helper: ${BIN_DIR}/gpt-review-update"

hash -r || true

# --------------------------------- summary ---------------------------------- #
cat <<EOT

${C_OK}Update complete.${C_END}

Launchers:
  • ${BIN_DIR}/gpt-review
  • ${BIN_DIR}/software_review.sh
  • ${BIN_DIR}/cookie_login.sh    (if present)
  • ${BIN_DIR}/gpt-review-update

Examples:
  software_review.sh instructions.txt /path/to/repo --cmd "pytest -q" --auto
  software_review.sh instructions.txt /repo --api --model \${GPT_REVIEW_MODEL:-gpt-5-pro} --cmd "npm test --silent" --auto

Tip:
  Configure environment in .env (see .env.example). API mode requires OPENAI_API_KEY.

EOT
