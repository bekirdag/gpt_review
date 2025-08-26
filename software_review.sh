#!/usr/bin/env bash
###############################################################################
# GPT‑Review ▸ Thin CLI Wrapper (browser or API mode)
###############################################################################
#
# Purpose
#   - Provide a memorable entrypoint: software_review.sh
#   - Validate arguments and print helpful usage
#   - Optionally load a .env file, dump effective environment, and tee logs
#   - Delegate to the underlying "gpt-review" Python CLI (or python -m fallback)
#   - Convenience switches:
#       --api         -> request API mode (no browser)  == --mode api
#       --model NAME  -> select API model (forwarded to the Python CLI)
#
# Usage
#   software_review.sh [wrapper-opts] instructions.txt /path/to/repo [gpt-review opts...]
#
# Wrapper options (must appear before positional args):
#   -h, --help             Show this help and exit
#       --version          Print underlying gpt-review version and exit
#   --load-dotenv[=PATH]   Source a dotenv file before running (default: ./.env)
#   --env-dump             Print effective environment and continue
#   --fresh                Remove .gpt-review-state.json to start fresh
#   --no-log               Do not write a wrapper log (console only)
#   --log-file PATH        Write wrapper + tool output to PATH (implies tee)
#   --api                  Convenience: use API driver (equivalent to --mode api)
#   --model NAME           Convenience: model for API mode (e.g. gpt-5-pro)
#
# Notes
#   - You can also pass --mode/--model directly after the positional args; this
#     wrapper forwards them unchanged. The --api flag is a convenience that is
#     rewritten to "--mode api".
#   - Environment variables commonly used:
#       GPT_REVIEW_PROFILE, GPT_REVIEW_CHAT_URL, GPT_REVIEW_HEADLESS,
#       GPT_REVIEW_WAIT_UI, GPT_REVIEW_STREAM_IDLE_SECS, GPT_REVIEW_RETRIES,
#       GPT_REVIEW_CHUNK_SIZE, GPT_REVIEW_COMMAND_TIMEOUT, GPT_REVIEW_LOG_DIR,
#       CHROME_BIN
#     API mode adds:
#       OPENAI_API_KEY, OPENAI_BASE_URL (or OPENAI_API_BASE), GPT_REVIEW_MODEL,
#       GPT_REVIEW_CTX_TURNS, GPT_REVIEW_LOG_TAIL_CHARS, GPT_REVIEW_API_TIMEOUT,
#       GPT_REVIEW_MODE=api (if you want API mode by default)
###############################################################################

set -euo pipefail
IFS=$'\n\t'

# -------------------------------- color helpers ---------------------------- #
if [[ -t 1 ]]; then
  C_INFO=$'\e[34m'; C_OK=$'\e[32m'; C_WARN=$'\e[33m'; C_ERR=$'\e[31m'; C_END=$'\e[0m'
else
  C_INFO=""; C_OK=""; C_WARN=""; C_ERR=""; C_END=""
fi

# --------------------------------- logging --------------------------------- #
_ts() { date "+%Y-%m-%d %H:%M:%S"; }
info()  { echo -e "${C_INFO}[$(_ts)] INFO ${C_END}$*"; }
warn()  { echo -e "${C_WARN}[$(_ts)] WARN ${C_END}$*" >&2; }
error() { echo -e "${C_ERR}[$(_ts)] ERROR${C_END} $*" >&2; }
die()   { error "$@"; exit 1; }

# Render a command array nicely (for logs only)
_join_cmd() {
  local out=""
  local tok
  for tok in "$@"; do
    if [[ "$tok" =~ [[:space:]] ]]; then
      out+="'$tok' "
    else
      out+="$tok "
    fi
  done
  printf "%s" "${out% }"
}

# --------------------------------- usage ----------------------------------- #
usage() {
  local d_chat="${GPT_REVIEW_CHAT_URL:-https://chatgpt.com/}"
  local d_prof="${GPT_REVIEW_PROFILE:-$HOME/.cache/gpt-review/chrome}"
  cat <<EOF
Usage: $(basename "$0") [wrapper-opts] instructions.txt /path/to/repo [gpt-review opts...]

Wrapper options (before positional args):
  -h, --help             Show this help and exit
      --version          Print underlying gpt-review version and exit
  --load-dotenv[=PATH]   Source dotenv file (default: ./.env)
  --env-dump             Print effective environment then continue
  --fresh                Remove .gpt-review-state.json to start fresh
  --no-log               Do not write a wrapper log (console only)
  --log-file PATH        Write output to PATH (implies tee)
  --api                  Use API mode (no browser) [same as --mode api]
  --model NAME           API model name (e.g. gpt-5-pro)

Positional arguments:
  instructions.txt       Plain-text instructions shown to the assistant
  /path/to/repo          Local Git repository to patch

Forwarded options (examples for gpt-review):
  --cmd "<shell>"        Run after each patch (e.g. "pytest -q")
  --auto                 Auto-reply 'continue'
  --timeout N            Kill --cmd after N seconds (default env: 300)
  --mode {browser,api}   Explicitly choose driver
  --model NAME           Model for API mode

Environment (current -> default):
  GPT_REVIEW_CHAT_URL         = ${GPT_REVIEW_CHAT_URL:-<unset>}  -> ${d_chat}
  GPT_REVIEW_PROFILE          = ${GPT_REVIEW_PROFILE:-<unset>}   -> ${d_prof}
  GPT_REVIEW_HEADLESS         = ${GPT_REVIEW_HEADLESS:-<unset>}  -> <off>
  GPT_REVIEW_WAIT_UI          = ${GPT_REVIEW_WAIT_UI:-<unset>}   -> 90
  GPT_REVIEW_STREAM_IDLE_SECS = ${GPT_REVIEW_STREAM_IDLE_SECS:-<unset>} -> 2
  GPT_REVIEW_RETRIES          = ${GPT_REVIEW_RETRIES:-<unset>}   -> 3
  GPT_REVIEW_CHUNK_SIZE       = ${GPT_REVIEW_CHUNK_SIZE:-<unset>} -> 15000
  GPT_REVIEW_COMMAND_TIMEOUT  = ${GPT_REVIEW_COMMAND_TIMEOUT:-<unset>} -> 300
  GPT_REVIEW_LOG_DIR          = ${GPT_REVIEW_LOG_DIR:-<unset>}   -> ./logs
  CHROME_BIN                  = ${CHROME_BIN:-<unset>}            (auto if unset)
  GPT_REVIEW_MODE             = ${GPT_REVIEW_MODE:-<unset>}       (default CLI mode: browser)

API mode extras:
  OPENAI_API_KEY              = $( [[ -n "${OPENAI_API_KEY:-}" ]] && echo "<set>" || echo "<unset>" )
  OPENAI_BASE_URL             = ${OPENAI_BASE_URL:-${OPENAI_API_BASE:-<unset>}}
  GPT_REVIEW_MODEL            = ${GPT_REVIEW_MODEL:-gpt-5-pro}
  GPT_REVIEW_CTX_TURNS        = ${GPT_REVIEW_CTX_TURNS:-6}
  GPT_REVIEW_LOG_TAIL_CHARS   = ${GPT_REVIEW_LOG_TAIL_CHARS:-20000}
  GPT_REVIEW_API_TIMEOUT      = ${GPT_REVIEW_API_TIMEOUT:-120}

Examples:
  $(basename "$0") docs/example_instructions.txt  ~/proj  --cmd "pytest -q" --auto
  $(basename "$0") --load-dotenv --env-dump instructions.txt /repo
  $(basename "$0") instructions.txt /repo --api --model gpt-5-pro --cmd "npm test --silent" --auto
EOF
}

# ----------------------------- parse wrapper opts --------------------------- #
WRAP_LOG=1
LOG_FILE=""
LOAD_DOTENV=""
ENV_DUMP=0
FRESH=0
WRAP_API=0
WRAP_MODEL=""
SHOW_VERSION=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    -h|--help) usage; exit 0 ;;
    --version) SHOW_VERSION=1; shift; break ;;  # handled later without requiring positionals
    --no-log) WRAP_LOG=0; shift ;;
    --log-file)
      LOG_FILE="${2:-}"; [[ -n "$LOG_FILE" ]] || die "--log-file requires a path"
      shift 2
      ;;
    --load-dotenv) LOAD_DOTENV="./.env"; shift ;;
    --load-dotenv=*) LOAD_DOTENV="${1#*=}"; shift ;;
    --env-dump) ENV_DUMP=1; shift ;;
    --fresh) FRESH=1; shift ;;
    --api) WRAP_API=1; shift ;;
    --model)
      WRAP_MODEL="${2:-}"; [[ -n "$WRAP_MODEL" ]] || die "--model requires a value"
      shift 2
      ;;
    --model=*)
      WRAP_MODEL="${1#*=}"; [[ -n "$WRAP_MODEL" ]] || die "--model requires a value"
      shift
      ;;
    --) shift; break ;;
    -*) break ;;    # begin positional/gpt-review args
    *)  break ;;
  esac
done

# If only --version was requested, run underlying CLI and exit early.
if [[ $SHOW_VERSION -eq 1 ]]; then
  if command -v gpt-review >/dev/null 2>&1; then
    exec gpt-review --version
  fi
  # Fallback to python -m
  PY_PATH="$(command -v python3 || command -v python || true)"
  [[ -n "$PY_PATH" ]] || die "No Python interpreter found for --version."
  exec "$PY_PATH" -m gpt_review --version
fi

# ------------------------------ positional args ---------------------------- #
if [[ $# -lt 2 ]]; then
  error "Missing required positional arguments."
  usage >&2
  exit 1
fi
INSTRUCTIONS="$1"
REPO="$2"
shift 2
FORWARD_ARGS=("$@")

# ------------------------------ dotenv (optional) -------------------------- #
if [[ -n "$LOAD_DOTENV" ]]; then
  if [[ -f "$LOAD_DOTENV" ]]; then
    info "Loading environment from: $LOAD_DOTENV"
    # shellcheck disable=SC1090
    set -a; . "$LOAD_DOTENV"; set +a
  else
    warn "Requested dotenv file not found: $LOAD_DOTENV"
  fi
fi

# -------------------------------- validations ------------------------------ #
[[ -f "$INSTRUCTIONS" ]] || die "'$INSTRUCTIONS' not found"
[[ -r "$INSTRUCTIONS" ]] || die "'$INSTRUCTIONS' is not readable"
[[ -d "$REPO/.git"   ]] || die "'$REPO' is not a git repository"

if [[ $FRESH -eq 1 ]]; then
  if [[ -f "$REPO/.gpt-review-state.json" ]]; then
    info "Removing existing state: $REPO/.gpt-review-state.json"
    rm -f "$REPO/.gpt-review-state.json"
  fi
fi

# --------------- normalize/augment forwarded args for mode/model ----------- #
NEW_ARGS=()
EXPLICIT_MODE_PRESENT=0
EXPLICIT_MODE_VALUE=""
EXPLICIT_MODEL_PRESENT=0
INLINE_API=0
MODEL_EXPLICIT=""

i=0
while [[ $i -lt ${#FORWARD_ARGS[@]} ]]; do
  arg="${FORWARD_ARGS[$i]}"
  case "$arg" in
    --mode)
      EXPLICIT_MODE_PRESENT=1
      if [[ $((i+1)) -lt ${#FORWARD_ARGS[@]} ]]; then
        EXPLICIT_MODE_VALUE="${FORWARD_ARGS[$((i+1))]}"
        NEW_ARGS+=("$arg" "$EXPLICIT_MODE_VALUE")
        i=$((i+2))
      else
        die "--mode requires a value"
      fi
      continue
      ;;
    --mode=*)
      EXPLICIT_MODE_PRESENT=1
      EXPLICIT_MODE_VALUE="${arg#*=}"
      NEW_ARGS+=("$arg")
      i=$((i+1)); continue
      ;;
    --model)
      EXPLICIT_MODEL_PRESENT=1
      if [[ $((i+1)) -lt ${#FORWARD_ARGS[@]} ]]; then
        MODEL_EXPLICIT="${FORWARD_ARGS[$((i+1))]}"
        NEW_ARGS+=("$arg" "$MODEL_EXPLICIT")
        i=$((i+2))
      else
        die "--model requires a value"
      fi
      continue
      ;;
    --model=*)
      EXPLICIT_MODEL_PRESENT=1
      MODEL_EXPLICIT="${arg#*=}"
      [[ -n "$MODEL_EXPLICIT" ]] || die "--model requires a value"
      NEW_ARGS+=("$arg")
      i=$((i+1)); continue
      ;;
    --api)
      INLINE_API=1
      i=$((i+1)); continue
      ;;
    *)
      NEW_ARGS+=("$arg")
      i=$((i+1)); continue
      ;;
  esac
done

# Compute effective mode:
# Priority: explicit --mode > wrapper --api/INLINE_API > env GPT_REVIEW_MODE > browser
if [[ $EXPLICIT_MODE_PRESENT -eq 1 ]]; then
  MODE_EFFECTIVE="$EXPLICIT_MODE_VALUE"
else
  if [[ $WRAP_API -eq 1 || $INLINE_API -eq 1 ]]; then
    MODE_EFFECTIVE="api"
  else
    MODE_EFFECTIVE="${GPT_REVIEW_MODE:-browser}"
  fi
fi

# Decide effective model (only relevant when api mode is active)
MODEL_EFFECTIVE=""
if [[ "$MODE_EFFECTIVE" == "api" ]]; then
  if [[ $EXPLICIT_MODEL_PRESENT -eq 1 ]]; then
    MODEL_EFFECTIVE="$MODEL_EXPLICIT"
  elif [[ -n "$WRAP_MODEL" ]]; then
    MODEL_EFFECTIVE="$WRAP_MODEL"
  else
    MODEL_EFFECTIVE="${GPT_REVIEW_MODEL:-gpt-5-pro}"
  fi
fi

# Apply convenience rewrites if mode was not explicitly provided
if [[ $EXPLICIT_MODE_PRESENT -eq 0 ]]; then
  NEW_ARGS+=(--mode "$MODE_EFFECTIVE")
fi
# Provide --model if we decided one and user did not set it explicitly
if [[ -n "$MODEL_EFFECTIVE" && $EXPLICIT_MODEL_PRESENT -eq 0 ]]; then
  NEW_ARGS+=(--model "$MODEL_EFFECTIVE")
fi

# -------------------------------- env dump -------------------------------- #
if [[ $ENV_DUMP -eq 1 ]]; then
  cat <<EOT
${C_INFO}- Environment dump -------------------------------------------------${C_END}
CHAT URL       : ${GPT_REVIEW_CHAT_URL:-https://chatgpt.com/}
PROFILE DIR    : ${GPT_REVIEW_PROFILE:-$HOME/.cache/gpt-review/chrome}
HEADLESS       : ${GPT_REVIEW_HEADLESS:-<off>}
WAIT_UI        : ${GPT_REVIEW_WAIT_UI:-90} (s)
STREAM_IDLE    : ${GPT_REVIEW_STREAM_IDLE_SECS:-2} (s)
RETRIES        : ${GPT_REVIEW_RETRIES:-3}
CHUNK_SIZE     : ${GPT_REVIEW_CHUNK_SIZE:-15000} (chars)
CMD TIMEOUT    : ${GPT_REVIEW_COMMAND_TIMEOUT:-300} (s)
LOG DIR        : ${GPT_REVIEW_LOG_DIR:-logs}
CHROME BIN     : ${CHROME_BIN:-<auto>}
MODE           : ${MODE_EFFECTIVE}
OPENAI KEY     : $( [[ -n "${OPENAI_API_KEY:-}" ]] && echo "<set>" || echo "<unset>" )
OPENAI URL     : ${OPENAI_BASE_URL:-${OPENAI_API_BASE:-<unset>}}
API MODEL      : ${MODEL_EFFECTIVE:-${GPT_REVIEW_MODEL:-gpt-5-pro}}
EOT
fi

# -------------------------------- logging ---------------------------------- #
if [[ $WRAP_LOG -eq 1 ]]; then
  LOG_DIR="${GPT_REVIEW_LOG_DIR:-logs}"
  mkdir -p "$LOG_DIR"
  if [[ -z "$LOG_FILE" ]]; then
    LOG_FILE="$LOG_DIR/wrapper-$(date '+%Y%m%d-%H%M%S').log"
  else
    mkdir -p "$(dirname "$LOG_FILE")"
  fi
  info "Wrapper log -> $LOG_FILE"
  exec > >(tee -a "$LOG_FILE") 2>&1
fi

# -------------------------- API preflight warnings ------------------------- #
# Respect both OPENAI_BASE_URL and alias OPENAI_API_BASE
if [[ -z "${OPENAI_BASE_URL:-}" && -n "${OPENAI_API_BASE:-}" ]]; then
  export OPENAI_BASE_URL="${OPENAI_API_BASE}"
fi
if [[ "$MODE_EFFECTIVE" == "api" && -z "${OPENAI_API_KEY:-}" ]]; then
  warn "API mode selected but OPENAI_API_KEY is not set. The run will likely fail."
fi

# ------------------------------ runner resolution -------------------------- #
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
Fixes:
  - Ensure your virtualenv is activated:     source venv/bin/activate
  - Or install the package:                  pip install .   (or: pip install -e .)
  - Or add Python to PATH:                   sudo apt install python3
EOF
    die "Runner resolution failed"
  fi

  info "Using fallback runner: $PY_PATH -m gpt_review"
  if ! "$PY_PATH" - <<'PY' >/dev/null 2>&1
import sys
try:
    import gpt_review  # noqa: F401
except Exception:
    sys.exit(42)
sys.exit(0)
PY
  then
    cat >&2 <<EOF
The Python interpreter at '$PY_PATH' cannot import the 'gpt_review' module.

Fixes:
  - Activate the correct virtualenv:
      ${C_INFO}source venv/bin/activate${C_END}
  - Or install the package into this interpreter:
      ${C_INFO}$PY_PATH -m pip install .${C_END}
    (for editable/dev installs:
      ${C_INFO}$PY_PATH -m pip install -e .[dev]${C_END})

Current PATH:
  $(command -v python3 || true)
  $(command -v python || true)
EOF
    die "Python interpreter found but gpt_review is not installed in it"
  fi

  RUNNER_CMD=("$PY_PATH" "-m" "gpt_review")
fi

info "Resolved runner command: $(_join_cmd "${RUNNER_CMD[@]}")"

# -------------------------------- kickoff ---------------------------------- #
BYTES="$(wc -c <"$INSTRUCTIONS" | tr -d '[:space:]' || echo 0)"
info "▶ Launching GPT‑Review …"
info "  • Instructions : $INSTRUCTIONS (${BYTES} bytes)"
info "  • Repository   : $REPO"
info "  • Mode         : $MODE_EFFECTIVE"
[[ ${#NEW_ARGS[@]} -gt 0 ]] && info "  • Forwarded CLI: $(_join_cmd "${NEW_ARGS[@]}")"

# Make Python colored logs visible by default where supported
export PY_COLORS="${PY_COLORS:-1}"

set +e
"${RUNNER_CMD[@]}" "$INSTRUCTIONS" "$REPO" "${NEW_ARGS[@]}"
EXIT_CODE=$?
set -e

if [[ $EXIT_CODE -eq 0 ]]; then
  info "✓ Completed successfully"
else
  error "✖ GPT‑Review exited with code $EXIT_CODE"
fi

exit $EXIT_CODE
