#!/usr/bin/env bash
# =============================================================================
# GPT‑Review ▸ Update Helper
# =============================================================================
# Safely update the local installation of GPT‑Review:
#   - Pulls the latest from GitHub (branch: main by default)
#   - Optionally discards local changes with --force
#   - Re-installs the package into the project venv
#   - Refreshes launchers in /usr/local/bin
#   - Enforces executable bits on helper scripts
#
# Usage:
#   curl -sSL https://raw.githubusercontent.com/bekirdag/gpt_review/main/update.sh | sudo bash
#   # or:
#   sudo bash /opt/gpt-review/update.sh
#
# Flags:
#   --repo-dir DIR   Target repo directory (default: /opt/gpt-review)
#   --branch BRANCH  Git branch to track (default: main)
#   --force          Discard local changes (git reset --hard + clean -fd)
#
# Environment overrides:
#   PYTHON           Python interpreter for venv creation (default: python3)
#   VENV_DIR         Virtualenv directory (default: $REPO_DIR/venv)
#   BIN_DIR          Launcher directory (default: /usr/local/bin)
# =============================================================================

set -euo pipefail
IFS=$'\n\t'

# ----------------------------- logging helpers ------------------------------ #
timestamp() { date '+%Y-%m-%d %H:%M:%S'; }
log()  { printf '[%s] %b\n' "$(timestamp)" "$*"; }
die()  { printf '[%s] ❌ %b\n' "$(timestamp)" "$*" >&2; exit 1; }

# ----------------------------- defaults & args ------------------------------ #
REPO_URL="https://github.com/bekirdag/gpt_review.git"
REPO_DIR="/opt/gpt-review"
BRANCH="main"
FORCE=0

PYTHON="${PYTHON:-python3}"
VENV_DIR="${VENV_DIR:-$REPO_DIR/venv}"
BIN_DIR="${BIN_DIR:-/usr/local/bin}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo-dir) REPO_DIR="${2:-}"; shift 2 ;;
    --branch)   BRANCH="${2:-}";   shift 2 ;;
    --force)    FORCE=1;           shift   ;;
    -h|--help)
      sed -n '1,80p' "$0" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *)
      die "Unknown argument: $1 (use --help for usage)"
      ;;
  esac
done

log "ℹ️  Target repo : $REPO_DIR"
log "ℹ️  Branch      : $BRANCH"
log "ℹ️  Force reset : $FORCE"

# ----------------------------- prereqs checks ------------------------------- #
require_cmd() {
  command -v "$1" >/dev/null 2>&1 || die "Missing dependency: $1"
}
require_cmd git
require_cmd curl
require_cmd "$PYTHON"

mkdir -p "$REPO_DIR"

# ------------------------------- git helpers -------------------------------- #
git_in()          { git -C "$REPO_DIR" "$@"; }
git_is_repo()     { git -C "$REPO_DIR" rev-parse --is-inside-work-tree >/dev/null 2>&1; }
git_has_changes() { [[ -n "$(git -C "$REPO_DIR" status --porcelain 2>/dev/null || true)" ]]; }
ensure_origin() {
  if ! git_in remote get-url origin >/dev/null 2>&1; then
    log "ℹ️  Setting remote origin → $REPO_URL"
    git_in remote add origin "$REPO_URL"
  fi
}

# --------------------------------- clone/pull -------------------------------- #
if ! git_is_repo; then
  log "ℹ️  No git repo found. Cloning fresh…"
  rm -rf "$REPO_DIR"/*
  git clone --branch "$BRANCH" --depth 1 "$REPO_URL" "$REPO_DIR" || die "Git clone failed"
else
  ensure_origin
  if git_has_changes && [[ "$FORCE" -eq 0 ]]; then
    die "Local changes detected. Re‑run with --force to discard them."
  fi

  log "ℹ️  Fetching from origin…"
  git_in fetch --prune origin

  log "ℹ️  Checking out branch '$BRANCH'…"
  if git_in show-ref --verify --quiet "refs/heads/$BRANCH"; then
    git_in checkout "$BRANCH"
  else
    git_in checkout -B "$BRANCH" "origin/$BRANCH" || git_in checkout -B "$BRANCH"
  fi

  if [[ "$FORCE" -eq 1 ]]; then
    log "⚠️  Discarding local changes (forced reset)…"
    git_in reset --hard "origin/$BRANCH"
    git_in clean -fd
  else
    log "ℹ️  Attempting fast‑forward…"
    if ! git_in merge --ff-only "origin/$BRANCH"; then
      log "⚠️  Non fast‑forward. Performing safe reset to origin/$BRANCH…"
      git_in reset --hard "origin/$BRANCH"
      git_in clean -fd
    fi
  fi
fi

# ------------------------------- virtualenv --------------------------------- #
if [[ ! -d "$VENV_DIR" ]]; then
  log "ℹ️  Creating virtualenv at $VENV_DIR"
  "$PYTHON" -m venv "$VENV_DIR" || die "Failed to create venv"
fi

PIP="$VENV_DIR/bin/pip"
PY="$VENV_DIR/bin/python"
[[ -x "$PIP" ]] || die "pip not found in venv ($PIP)"

log "ℹ️  Upgrading pip/setuptools/wheel"
"$PY" -m pip install --upgrade --quiet pip setuptools wheel

log "ℹ️  Installing GPT‑Review (editable)"
# Use editable install to keep console entry points fresh
"$PIP" install -e "$REPO_DIR" >/dev/null

# ------------------------------- launchers ---------------------------------- #
link_file() {
  local SRC="$1" DST="$2"
  mkdir -p "$(dirname "$DST")"
  ln -sfn "$SRC" "$DST"
}

refresh_launchers() {
  log "ℹ️  Refreshing launchers in $BIN_DIR (best‑effort)…"

  # Console entry point installed by pip
  if [[ -x "$VENV_DIR/bin/gpt-review" ]]; then
    link_file "$VENV_DIR/bin/gpt-review" "$BIN_DIR/gpt-review"
    log "✅ Linked: $BIN_DIR/gpt-review"
  else
    log "⚠️  gpt-review console script not found in venv"
  fi

  # Ensure repo helpers are executable (git may drop +x)
  if [[ -f "$REPO_DIR/software_review.sh" ]]; then
    chmod 0755 "$REPO_DIR/software_review.sh" || true
    link_file "$REPO_DIR/software_review.sh" "$BIN_DIR/software_review.sh"
    chmod 0755 "$BIN_DIR/software_review.sh" || true
    log "✅ Linked: $BIN_DIR/software_review.sh → $REPO_DIR/software_review.sh"
  else
    log "⚠️  Missing: $REPO_DIR/software_review.sh"
  fi

  if [[ -f "$REPO_DIR/cookie_login.sh" ]]; then
    chmod 0755 "$REPO_DIR/cookie_login.sh" || true
    link_file "$REPO_DIR/cookie_login.sh" "$BIN_DIR/cookie_login.sh"
    chmod 0755 "$BIN_DIR/cookie_login.sh" || true
    log "✅ Linked: $BIN_DIR/cookie_login.sh → $REPO_DIR/cookie_login.sh"
  else
    log "⚠️  Missing: $REPO_DIR/cookie_login.sh"
  fi

  make_update_helper "$BIN_DIR/gpt-review-update"
  chmod 0755 "$BIN_DIR/gpt-review-update" || true
  log "✅ Linked: $BIN_DIR/gpt-review-update"
}

make_update_helper() {
  local OUT="$1"
  cat >"$OUT" <<'SH'
#!/usr/bin/env bash
# Small wrapper to always fetch & run the latest updater from GitHub.
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
}

refresh_launchers

# ------------------------------- finish up ---------------------------------- #
hash -r || true

log "🎉 Update complete."
log "Run   :  software_review.sh --help"
log "Login :  cookie_login.sh    # opens a visible browser to save cookies"
log "Update:  gpt-review-update  # fetches & runs this updater"
