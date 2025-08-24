#!/usr/bin/env bash
###############################################################################
# GPT‑Review ▸ In‑place Updater
#
# Purpose
# -------
# Update an existing GPT‑Review installation in /opt/gpt-review (default) or a
# user‑provided clone. The script:
#   • pulls the latest code from GitHub (branch = main by default),
#   • (re)creates/uses a Python venv at <repo>/venv,
#   • reinstalls the package in editable mode (prefers extras: [dev]),
#   • refreshes launchers in /usr/local/bin:
#       - gpt-review
#       - software_review.sh
#       - cookie_login.sh
#       - gpt-review-update  (symlink to this script)
#
# Usage
# -----
#   sudo bash update.sh [--repo /opt/gpt-review] [--branch main] [--force]
#
#   Env overrides:
#     REPO_DIR=/custom/path
#     BRANCH=feature/foo
#
# Safety
# ------
# • Default is a fast‑forward pull and **will refuse** if local changes exist.
# • Use --force to discard local changes (git reset --hard + clean).
###############################################################################
set -Eeuo pipefail

# --- Pretty logging -----------------------------------------------------------
ts()      { date '+%Y-%m-%d %H:%M:%S'; }
log()     { printf '[%s] %s\n' "$(ts)" "$*"; }
info()    { log "ℹ️  $*"; }
ok()      { log "✅ $*"; }
warn()    { log "⚠️  $*" >&2; }
err()     { log "❌ $*" >&2; }
die()     { err "$*"; exit 1; }

# --- Usage -------------------------------------------------------------------
usage() {
  cat <<'USAGE'
Usage: update.sh [options]

Options:
  -d, --repo   PATH   Target repo dir (default: /opt/gpt-review)
  -b, --branch NAME   Git branch to update to (default: main)
  -f, --force         Discard local changes (hard reset + clean)
  -h, --help          Show this help and exit

Env:
  REPO_DIR=/opt/gpt-review   BRANCH=main

Examples:
  sudo bash update.sh
  sudo bash update.sh --repo /srv/gpt-review --branch main
  sudo bash update.sh -f
USAGE
}

# --- Defaults ----------------------------------------------------------------
REPO_DIR="${REPO_DIR:-/opt/gpt-review}"
BRANCH="${BRANCH:-main}"
FORCE=0

# --- Arg parsing --------------------------------------------------------------
while [[ $# -gt 0 ]]; do
  case "$1" in
    -d|--repo|--dir) REPO_DIR="${2:-}"; shift 2 ;;
    -b|--branch)     BRANCH="${2:-}";   shift 2 ;;
    -f|--force)      FORCE=1;           shift   ;;
    -h|--help)       usage; exit 0              ;;
    *) die "Unknown argument: $1 (use --help)";;
  esac
done

# --- Preflight ---------------------------------------------------------------
command -v git >/dev/null    || die "git is required"
PY_BIN="$(command -v python3 || true)"
[[ -n "$PY_BIN" ]] || PY_BIN="$(command -v python || true)"
[[ -n "$PY_BIN" ]] || die "python (3.x) is required"

info "Target repo : $REPO_DIR"
info "Branch      : $BRANCH"
info "Force reset : $FORCE"

# Ensure target directory exists
mkdir -p "$REPO_DIR"

# --- Clone/pull ---------------------------------------------------------------
ORIGIN_URL="https://github.com/bekirdag/gpt_review.git"

if [[ ! -d "$REPO_DIR/.git" ]]; then
  info "No git repo found. Cloning fresh into: $REPO_DIR"
  git clone --depth=1 --branch "$BRANCH" "$ORIGIN_URL" "$REPO_DIR"
else
  info "Existing git repo detected. Updating…"
  pushd "$REPO_DIR" >/dev/null
  # Validate remote if possible (best‑effort)
  if git remote get-url origin >/dev/null 2>&1; then
    :
  else
    warn "No 'origin' remote set; configuring origin → $ORIGIN_URL"
    git remote add origin "$ORIGIN_URL"
  fi

  git fetch --prune origin
  git checkout "$BRANCH"

  if [[ "$FORCE" -eq 1 ]]; then
    warn "--force set: discarding local changes"
    git reset --hard "origin/$BRANCH"
    git clean -fdx
  else
    # Refuse to proceed on local changes (user can re‑run with --force).
    if [[ -n "$(git status --porcelain)" ]]; then
      die "Local changes detected. Re‑run with --force to discard them."
    fi
    git pull --ff-only origin "$BRANCH"
  fi
  popd >/dev/null
fi

# --- Virtualenv ---------------------------------------------------------------
VENV="${VENV:-$REPO_DIR/venv}"
if [[ ! -x "$VENV/bin/python" ]]; then
  info "Creating virtualenv: $VENV"
  "$PY_BIN" -m venv "$VENV"
fi

# --- Install / Upgrade --------------------------------------------------------
info "Upgrading pip/setuptools/wheel…"
"$VENV/bin/python" -m pip install --upgrade pip setuptools wheel >/dev/null

# Install with dev extras if available; fall back to runtime only.
pushd "$REPO_DIR" >/dev/null
info "Installing GPT‑Review (editable)…"
if "$VENV/bin/python" -m pip install -e '.[dev]' >/dev/null 2>&1; then
  ok "Installed with dev extras."
else
  warn "Dev extras not available or failed; installing runtime package only."
  "$VENV/bin/python" -m pip install -e . >/dev/null
  ok "Installed runtime package."
fi
popd >/dev/null

# --- Link launchers (best‑effort) --------------------------------------------
link_if_exists() {
  local src="$1" dst="$2"
  if [[ -e "$src" ]]; then
    ln -sf "$src" "$dst"
    ok "Linked: $dst → $src"
    return 0
  fi
  return 1
}

info "Refreshing launchers in /usr/local/bin (best‑effort)…"
# gpt-review console script (from venv)
if [[ -x "$VENV/bin/gpt-review" ]]; then
  ln -sf "$VENV/bin/gpt-review" /usr/local/bin/gpt-review
  ok "Linked: /usr/local/bin/gpt-review"
else
  warn "Console entry-point not found at $VENV/bin/gpt-review"
fi

# Script locations vary by repository layout → try both root/ and scripts/
link_if_exists "$REPO_DIR/software_review.sh"      /usr/local/bin/software_review.sh \
  || link_if_exists "$REPO_DIR/scripts/software_review.sh" /usr/local/bin/software_review.sh \
  || warn "software_review.sh not found in repo."

link_if_exists "$REPO_DIR/cookie_login.sh"         /usr/local/bin/cookie_login.sh \
  || link_if_exists "$REPO_DIR/scripts/cookie_login.sh" /usr/local/bin/cookie_login.sh \
  || warn "cookie_login.sh not found in repo."

# Self link for convenience
if [[ -e "$REPO_DIR/update.sh" ]]; then
  ln -sf "$REPO_DIR/update.sh" /usr/local/bin/gpt-review-update
  ok "Linked: /usr/local/bin/gpt-review-update"
fi

# --- Versions (best‑effort) ---------------------------------------------------
info "Verifying installation…"
if "$VENV/bin/python" -m gpt_review --version >/dev/null 2>&1; then
  ver="$("$VENV/bin/python" -m gpt_review --version)"
  ok "GPT‑Review version: $ver"
else
  warn "Could not query version via python -m gpt_review --version"
fi

ok "GPT‑Review updated successfully."
info "You can now run:  software_review.sh --help"
