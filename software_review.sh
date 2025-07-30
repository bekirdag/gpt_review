#!/usr/bin/env bash
# Thin wrapper for gpt-review
set -euo pipefail

if [[ $# -lt 2 ]]; then
  echo "Usage: $(basename "$0") instructions.txt /path/to/git/repo [--auto]" >&2
  exit 1
fi

command -v gpt-review >/dev/null 2>&1 || {
  echo "gpt-review not found. Did you run install.sh?" >&2
  exit 1
}

exec gpt-review "$@"
