#!/usr/bin/env bash
# GPT‑review installer — Ubuntu 22.04+  (chromedriver handled by webdriver‑manager)
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "Run as root (sudo)" >&2; exit 1; }

REPO_DIR="/opt/gpt-review"
PY_ENV="$REPO_DIR/venv"

# 1. Base packages
apt-get update -y
apt-get install -y python3 python3-venv python3-pip git curl wget unzip jq build-essential \
                   chromium-browser || apt-get install -y chromium

# 2. Project skeleton
git clone https://github.com/your-org/gpt-review.git "$REPO_DIR" 2>/dev/null || true
mkdir -p "$REPO_DIR/logs"

# 3. Python venv
python3 -m venv "$PY_ENV"
source "$PY_ENV/bin/activate"
pip install -U pip
pip install -e "$REPO_DIR"        # pulls webdriver-manager via pyproject.toml
deactivate

# 4. Wrapper
cat >/usr/local/bin/gpt-review <<EOF
#!/usr/bin/env bash
source "$PY_ENV/bin/activate"
python "$REPO_DIR/review.py" "\$@"
EOF
chmod +x /usr/local/bin/gpt-review

echo "✅  Installation complete.  Use: gpt-review instructions.txt /path/to/repo --auto"
