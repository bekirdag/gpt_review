#!/usr/bin/env bash
###############################################################################
# GPT‑Review ▸ Thin CLI wrapper
###############################################################################
#
# Why this script?
# ----------------
# * Provides a memorable command name (`software_review.sh`) for newcomers.
# * Performs basic argument validation and prints colourised usage/help.
# * Simply forwards all options to the underlying **gpt-review** Python CLI.
#
# Example
# -------
#   ./software_review.sh instructions.txt  /path/to/repo  --cmd "pytest -q" --auto
#
# Runtime behaviour
# -----------------
# ChatGPT is automatically reminded that **each reply must patch exactly ONE
# file** and that it must *ask you to continue* before sending the next patch.
# The driver enforces this contract.
#
# The script exits with **the same status code** as gpt-review.
###############################################################################

set -euo pipefail

# ──────────────────────────── colour helpers ────────────────────────────────
if [[ -t 1 ]]; then
  C_INFO="\e[34m"; C_ERR="\e[31m"; C_END="\e[0m"
else
  C_INFO=""; C_ERR=""; C_END=""
fi

# ───────────────────────────── usage banner ─────────────────────────────────
usage() {
  cat <<EOF
${C_INFO}Usage:${C_END} $(basename "$0") instructions.txt /path/to/repo [options]

Required positional arguments:
  instructions.txt      Plain-text instructions shown to ChatGPT
  /path/to/repo         Local Git repository to patch

Common options (forwarded to gpt-review):
  --cmd "<shell>"       Run after each patch (e.g. "pytest -q")
  --auto                Auto‑reply 'continue' (no key presses)
  --timeout N           Kill --cmd after N seconds (default 300)

Important:
  • ChatGPT will patch **one script per reply** and then ask you to *continue*.
    Press <Enter> (or use --auto) to accept the next chunk.

Environment variables:
  GPT_REVIEW_HEADLESS   Set to 1 to force headless Chromium
  GPT_REVIEW_PROFILE    Chrome user‑data dir (stores cookies)

Example:
  $(basename "$0") docs/example_instructions.txt  ~/my-project  --cmd "pytest -q" --auto
EOF
}

# ──────────────────────────── arg validation ────────────────────────────────
if [[ $# -lt 2 ]]; then
  echo -e "${C_ERR}Error:${C_END} missing required arguments." >&2
  usage >&2
  exit 1
fi

INSTRUCTIONS="$1"
REPO="$2"
shift 2  # remaining args forwarded

[[ -f "$INSTRUCTIONS" ]] || { echo -e "${C_ERR}Error:${C_END} '$INSTRUCTIONS' not found." >&2; exit 1; }
[[ -d "$REPO/.git" ]]    || { echo -e "${C_ERR}Error:${C_END} '$REPO' is not a git repository." >&2; exit 1; }

# ───────────────────────────── delegation ───────────────────────────────────
command -v gpt-review >/dev/null 2>&1 || {
  echo -e "${C_ERR}Error:${C_END} gpt-review command not found. Did you run install.sh?" >&2
  exit 1
}

echo -e "${C_INFO}▶︎ Launching GPT‑Review …${C_END}"
gpt-review "$INSTRUCTIONS" "$REPO" "$@"
EXIT_CODE=$?

if [[ $EXIT_CODE -eq 0 ]]; then
  echo -e "${C_INFO}✓ GPT‑Review finished successfully${C_END}"
else
  echo -e "${C_ERR}✖ GPT‑Review exited with code $EXIT_CODE${C_END}"
fi

exit $EXIT_CODE
