#!/usr/bin/env bash
###############################################################################
# GPT‑Review ▸ Thin CLI Wrapper (enhanced)
###############################################################################
#
# Purpose
# -------
# * Provide a memorable command name (`software_review.sh`) for newcomers.
# * Perform argument validation and print colourised usage/help.
# * Optionally load a `.env` file, dump effective environment, and tee output
#   to a timestamped log file for easier debugging.
# * Delegate to the underlying **gpt-review** Python CLI (or `python -m` fallback).
#
# Improvements in this revision
# -----------------------------
# • Aligned, readable `--env-dump` output (columnised).
# • Exit summary line with status, exit code, and wall‑clock duration.
# • If `--log-file` is provided, it **implies** logging (even if `--no-log` was set).
# • Clearer messages; consistent logging helpers.
#
# Usage
# -----
#   software_review.sh [wrapper‑opts] instructions.txt /path/to/repo [gpt‑review opts]
#
# Wrapper options (must appear *before* positional args):
#   --help                 Show this help and exit
#   --load-dotenv[=PATH]   Source a dotenv file before running (default: ./.env)
#   --env-dump             Print effective GPT_REVIEW_* / CHROME_BIN settings
#   --fresh                Remove .gpt-review-state.json to start a fresh session
#   --no-log               Do not write a wrapper log (console only)
#   --log-file PATH        Write wrapper + tool output to PATH (implies tee)
#
# Examples
# --------
#   software_review.sh instructions.txt  ~/my-project  --cmd "pytest -q" --auto
#   software_review.sh --load-dotenv --env-dump instructions.txt  /repo
#
# Notes
# -----
# * ChatGPT is automatically reminded to update **one file per reply** and to
#   ask you to **continue** between chunks. The driver enforces this contract.
# * Environment variables (see --env-dump):
#       GPT_REVIEW_PROFILE, GPT_REVIEW_CHAT_URL, GPT_REVIEW_HEADLESS,
#       GPT_REVIEW_WAIT_UI, GPT_REVIEW_STREAM_IDLE_SECS, GPT_REVIEW_RETRIES,
#       GPT_REVIEW_CHUNK_SIZE, GPT_REVIEW_COMMAND_TIMEOUT, GPT_REVIEW_LOG_DIR,
#       CHROME_BIN
###############################################################################

set -euo pipefail

# ─────────────────────────── colour helpers ────────────────────────────────
if [[ -t 1 ]]; then
  C_INFO="\e[34m"; C_OK="\e[32m"; C_WARN="\e[33m"; C_ERR="\e[31m"; C_END="\e[0m"
else
  C_INFO=""; C_OK=""; C_WARN=""; C_ERR=""; C_END=""
fi

# ───────────────────────────── small utils ─────────────────────────────────
_ts() { date "+%Y-%m-%d %H:%M:%S"; }

info()  { echo -e "${C_INFO}[$(_ts)] INFO ${C_END}$*"; }
warn()  { echo -e "${C_WARN}[$(_ts)] WARN ${C_END}$*" >&2; }
error() { echo -e "${C_ERR}[$(_ts)] ERROR${C_END} $*" >&2; }
die()   { error "$@"; exit 1; }

# Print a command array as a readable string
_join_cmd() {
  local out=""
  for tok in "$@"; do
    # Quote tokens with spaces for display only (not execution)
    if [[ "$tok" =~ [[:space:]] ]]; then
      out+="'$tok' "
    else
      out+="$tok "
    fi
  done
  printf "%s" "${out% }"
}

# Columnised key/value (aligned) for env‑dump
_env_kv() {
  # Usage: _env_kv "KEY" "VALUE"
  printf "  %-24s : %s\n" "$1" "$2"
}

# ───────────────────────────── usage banner ────────────────────────────────
usage() {
  local d_chat="https://chatgpt.com/"
  local d_prof="${HOME}/.cache/gpt-review/chrome"
  cat <<EOF
${C_INFO}Usage:${C_END} $(basename "$0") [wrapper‑opts] instructions.txt /path/to/repo [gpt‑review opts]

Wrapper options (before positional args):
  --help                 Show this help and exit
  --load-dotenv[=PATH]   Source dotenv file (default: ./.env)
  --env-dump             Print effective environment vars then continue
  --fresh                Remove .gpt-review-state.json to start fresh
  --no-log               Do not write a wrapper log (console only)
  --log-file PATH        Write output to PATH (implies tee)

Positional arguments:
  instructions.txt       Plain‑text instructions shown to ChatGPT
  /path/to/repo          Local Git repository to patch

Forwarded options (examples for gpt-review):
  --cmd "<shell>"        Run after each patch (e.g. "pytest -q")
  --auto                 Auto‑reply 'continue' (no key presses)
  --timeout N            Kill --cmd after N seconds (default env: GPT_REVIEW_COMMAND_TIMEOUT or 300)

Environment (current → default):
  GPT_REVIEW_CHAT_URL        = ${GPT_REVIEW_CHAT_URL:-<unset>}  → ${d_chat}
  GPT_REVIEW_PROFILE         = ${GPT_REVIEW_PROFILE:-<unset>}   → ${d_prof}
  GPT_REVIEW_HEADLESS        = ${GPT_REVIEW_HEADLESS:-<off>}    → <off>
  GPT_REVIEW_WAIT_UI         = ${GPT_REVIEW_WAIT_UI:-<unset>}   → 90
  GPT_REVIEW_STREAM_IDLE_SECS= ${GPT_REVIEW_STREAM_IDLE_SECS:-<unset>} → 2
  GPT_REVIEW_RETRIES         = ${GPT_REVIEW_RETRIES:-<unset>}   → 3
  GPT_REVIEW_CHUNK_SIZE      = ${GPT_REVIEW_CHUNK_SIZE:-<unset>}→ 15000
  GPT_REVIEW_COMMAND_TIMEOUT = ${GPT_REVIEW_COMMAND_TIMEOUT:-<unset>} → 300
  GPT_REVIEW_LOG_DIR         = ${GPT_REVIEW_LOG_DIR:-<unset>}   → ./logs
  CHROME_BIN                 = ${CHROME_BIN:-<auto>}             (auto‑detected if unset)

Examples:
  $(basename "$0") docs/example_instructions.txt  ~/my-project  --cmd "pytest -q" --auto
  $(basename "$0") --load-dotenv --env-dump instructions.txt /repo

EOF
}

# ───────────────────────────── parse wrapper opts ───────────────────────────
WRAP_LOG=1
LOG_FILE=""
LOAD_DOTENV=""
ENV_DUMP=0
FRESH=0

# Collect wrapper options that must precede positional args
while [[ $# -gt 0 ]]; do
  case "$1" in
    --help) usage; exit 0 ;;
    --no-log) WRAP_LOG=0; shift ;;
    --log-file) LOG_FILE="${2:-}"; [[ -n "$LOG_FILE" ]] || die "--log-file needs a path"; shift 2 ;;
    --load-dotenv) LOAD_DOTENV="./.env"; shift ;;
    --load-dotenv=*) LOAD_DOTENV="${1#*=}"; shift ;;
    --env-dump) ENV_DUMP=1; shift ;;
    --fresh) FRESH=1; shift ;;
    --) shift; break ;;                 # explicit end of wrapper options
    -* ) break ;;                       # begin gpt-review / positional args
    *  ) break ;;                       # first positional arg encountered
  esac
done

# If a log file was explicitly requested, it implies logging.
if [[ -n "$LOG_FILE" ]]; then
  WRAP_LOG=1
fi

# ───────────────────────────── positional args ──────────────────────────────
if [[ $# -lt 2 ]]; then
  error "Missing required positional arguments."
  usage >&2
  exit 1
fi
INSTRUCTIONS="$1"
REPO="$2"
shift 2  # remaining args forwarded verbatim to gpt-review
FORWARD_ARGS=("$@")

# ───────────────────────────── dotenv (optional) ────────────────────────────
if [[ -n "$LOAD_DOTENV" ]]; then
  if [[ -f "$LOAD_DOTENV" ]]; then
    info "Loading environment from: $LOAD_DOTENV"
    # shellcheck disable=SC1090
    set -a; . "$LOAD_DOTENV"; set +a
  else
    warn "Requested dotenv file not found: $LOAD_DOTENV"
  fi
fi

# ───────────────────────────── validations ──────────────────────────────────
[[ -f "$INSTRUCTIONS" ]] || die "'$INSTRUCTIONS' not found."
[[ -r "$INSTRUCTIONS" ]] || die "'$INSTRUCTIONS' is not readable."
[[ -d "$REPO/.git" ]]    || die "'$REPO' is not a git repository."

# Optional fresh start: remove state file
if [[ $FRESH -eq 1 ]]; then
  if [[ -f "$REPO/.gpt-review-state.json" ]]; then
    info "Removing existing state: $REPO/.gpt-review-state.json"
    rm -f "$REPO/.gpt-review-state.json"
  fi
fi

# ───────────────────────────── env dump (optional) ──────────────────────────
if [[ $ENV_DUMP -eq 1 ]]; then
  echo
  echo -e "${C_INFO}─ Environment dump ─────────────────────────────${C_END}"
  _env_kv "CHAT URL"            "${GPT_REVIEW_CHAT_URL:-https://chatgpt.com/}"
  _env_kv "PROFILE DIR"         "${GPT_REVIEW_PROFILE:-$HOME/.cache/gpt-review/chrome}"
  _env_kv "HEADLESS"            "${GPT_REVIEW_HEADLESS:-<off>}"
  _env_kv "WAIT_UI (s)"         "${GPT_REVIEW_WAIT_UI:-90}"
  _env_kv "STREAM_IDLE (s)"     "${GPT_REVIEW_STREAM_IDLE_SECS:-2}"
  _env_kv "RETRIES"             "${GPT_REVIEW_RETRIES:-3}"
  _env_kv "CHUNK_SIZE (chars)"  "${GPT_REVIEW_CHUNK_SIZE:-15000}"
  _env_kv "CMD TIMEOUT (s)"     "${GPT_REVIEW_COMMAND_TIMEOUT:-300}"
  _env_kv "LOG DIR"             "${GPT_REVIEW_LOG_DIR:-logs}"
  _env_kv "CHROME_BIN"          "${CHROME_BIN:-<auto>}"
  echo
fi

# ───────────────────────────── logging (tee to file) ────────────────────────
if [[ $WRAP_LOG -eq 1 ]]; then
  LOG_DIR="${GPT_REVIEW_LOG_DIR:-logs}"
  mkdir -p "$LOG_DIR"
  if [[ -z "$LOG_FILE" ]]; then
    LOG_FILE="$LOG_DIR/wrapper-$(date '+%Y%m%d-%H%M%S').log"
  fi
  info "Wrapper log → $LOG_FILE"
  # Redirect all subsequent stdout/stderr through tee (keeps exit codes intact)
  exec > >(tee -a "$LOG_FILE") 2>&1
fi

# ───────────────────────────── runner resolution ────────────────────────────
# Prefer the installed CLI if present; otherwise fall back to python -m.
RUNNER="gpt-review"
RUNNER_PATH="$(command -v "$RUNNER" || true)"
PY_PATH="$(command -v python3 || command -v python || true)"

if [[ -n "$RUNNER_PATH" ]]; then
  info "Using CLI runner: $RUNNER_PATH"
  RUNNER_CMD=("$RUNNER_PATH")
else
  if [[ -z "$PY_PATH" ]]; then
    cat >&2 <<'EOF'
No usable runner found.

Neither the 'gpt-review' CLI nor a Python interpreter ('python3' or 'python') is available.
👉  Fixes:
    • Ensure your virtual‑env is activated:     source venv/bin/activate
    • Or install the package:                   pip install .  (or  pip install -e .)
    • Or add Python to PATH:                    sudo apt install python3  (Ubuntu/Debian)

EOF
    die "Runner resolution failed."
  fi

  info "Using fallback runner: $PY_PATH -m gpt_review"
  # Verify that this interpreter can import gpt_review to fail fast with guidance.
  if ! "$PY_PATH" - <<'PY' >/dev/null 2>&1
import sys
try:
    import gpt_review  # noqa
except Exception:
    sys.exit(42)
sys.exit(0)
PY
  then
    cat >&2 <<EOF
The Python interpreter at '$PY_PATH' cannot import the 'gpt_review' module.

👉  Fixes:
    • Activate the correct virtual‑env:
        ${C_INFO}source venv/bin/activate${C_END}
    • Or install the package into this interpreter:
        ${C_INFO}$PY_PATH -m pip install .${C_END}
      (for editable/dev installs: ${C_INFO}$PY_PATH -m pip install -e .[dev]${C_END})
    • Current PATH:
        $(command -v python3 || true)
        $(command -v python || true)

EOF
    die "Python interpreter found but gpt_review is not installed in it."
  fi

  RUNNER_CMD=("$PY_PATH" "-m" "gpt_review")
fi

info "Resolved runner command: $(_join_cmd "${RUNNER_CMD[@]}")"

# ───────────────────────────── kickoff ──────────────────────────────────────
info "▶︎ Launching GPT‑Review …"
instr_bytes="$(wc -c <"$INSTRUCTIONS" | tr -d '[:space:]')"
info "• Instructions : $INSTRUCTIONS (${instr_bytes} bytes)"
info "• Repository   : $REPO"
[[ ${#FORWARD_ARGS[@]} -gt 0 ]] && info "• Forwarded CLI: $(_join_cmd "${FORWARD_ARGS[@]}")"

# Make CLI colours visible by default in most environments
export PY_COLORS="${PY_COLORS:-1}"

# Execute the tool
START_EPOCH="$(date +%s)"
set +e  # we want to capture exit code and print a friendly summary
"${RUNNER_CMD[@]}" "$INSTRUCTIONS" "$REPO" "${FORWARD_ARGS[@]}"
EXIT_CODE=$?
set -e
END_EPOCH="$(date +%s)"
DURATION="$(( END_EPOCH - START_EPOCH ))"

if [[ $EXIT_CODE -eq 0 ]]; then
  info "✓ GPT‑Review finished successfully"
  echo "Result: SUCCESS | exit=${EXIT_CODE} | duration=${DURATION}s"
else
  error "✖ GPT‑Review exited with code $EXIT_CODE"
  echo "Result: FAILURE | exit=${EXIT_CODE} | duration=${DURATION}s"
fi

# Also emit a machine‑parsable line for log scrapers
echo "EXIT_CODE=${EXIT_CODE}"

exit $EXIT_CODE
