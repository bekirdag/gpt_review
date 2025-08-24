###############################################################################
#  GPT‑Review ▸ Production container image
###############################################################################
#  • Base: Debian 12 (slim) + system Chromium (apt) – reliable driver pairing
#  • Headless by default (GPT_REVIEW_HEADLESS=1) – no display server required
#  • Non‑root user for safer defaults
#
#  Build:
#      docker build -t gpt-review .
#
#  Run (example):
#      docker run -it --rm \
#         -v $HOME/.cache/gpt-review/chrome:/home/nonroot/.cache/chrome \
#         -v "$(pwd)":/workspace \
#         gpt-review /workspace/example_instructions.txt /workspace \
#         --cmd "pytest -q" --auto
###############################################################################

FROM debian:12-slim

LABEL org.opencontainers.image.title="gpt-review" \
      org.opencontainers.image.description="Browser‑driven, ChatGPT‑powered code‑review loop with auto‑test execution." \
      org.opencontainers.image.source="https://github.com/bekirdag/gpt_review" \
      org.opencontainers.image.licenses="MIT"

# Keep apt non‑interactive within the RUN layer only.
ARG DEBIAN_FRONTEND=noninteractive

# -----------------------------------------------------------------------------
# System dependencies
# -----------------------------------------------------------------------------
# • python3 / pip / venv – runtime for the app
# • chromium            – browser (headless via --headless=new)
# • fonts               – readable pages & emoji in logs
# • common chrome deps  – shared libs Chromium expects even in headless mode
# The `set -eux` provides verbose, fail-fast logs at build time.
# -----------------------------------------------------------------------------
RUN set -eux; \
    echo "[deps] Updating apt index …"; \
    apt-get update -y; \
    echo "[deps] Installing system packages …"; \
    apt-get install -y --no-install-recommends \
        python3 python3-venv python3-pip git ca-certificates curl wget unzip \
        chromium fonts-liberation fonts-noto-color-emoji \
        libglib2.0-0 libnss3 libx11-6 libx11-xcb1 libxcomposite1 libxdamage1 \
        libxext6 libxfixes3 libxrandr2 libxtst6 libxss1 libatk1.0-0 \
        libatk-bridge2.0-0 libgtk-3-0 libgbm1 libdrm2 libxcb-dri3-0 \
        libxkbcommon0 libxshmfence1 libasound2 libu2f-udev; \
    echo "[deps] Cleaning apt caches …"; \
    rm -rf /var/lib/apt/lists/*

# -----------------------------------------------------------------------------
# Create a non‑root user for security
# -----------------------------------------------------------------------------
RUN useradd -m nonroot

# -----------------------------------------------------------------------------
# Workdir & project copy
# -----------------------------------------------------------------------------
WORKDIR /app
COPY . /app

# -----------------------------------------------------------------------------
# Python virtual‑environment & install
# -----------------------------------------------------------------------------
RUN set -eux; \
    echo "[venv] Creating virtualenv …"; \
    python3 -m venv /app/venv; \
    . /app/venv/bin/activate; \
    echo "[venv] Upgrading pip & installing package …"; \
    pip install --no-cache-dir -U pip; \
    pip install --no-cache-dir .; \
    deactivate

# -----------------------------------------------------------------------------
# Environment variables
# -----------------------------------------------------------------------------
# • CHROME_BIN tells Selenium which browser binary to launch
# • GPT_REVIEW_PROFILE stores persistent cookies (mounted via -v in docker run)
# -----------------------------------------------------------------------------
ENV PATH=/app/venv/bin:$PATH \
    CHROME_BIN=/usr/bin/chromium \
    GPT_REVIEW_PROFILE=/home/nonroot/.cache/chrome \
    GPT_REVIEW_HEADLESS=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8

# -----------------------------------------------------------------------------
# Permissions – ensure nonroot can write profile & logs
# -----------------------------------------------------------------------------
RUN set -eux; \
    mkdir -p /home/nonroot/.cache/chrome /app/logs; \
    chown -R nonroot:nonroot /home/nonroot /app/logs

USER nonroot

# -----------------------------------------------------------------------------
# Entrypoint
# -----------------------------------------------------------------------------
# `gpt-review` is exposed via [project.scripts] in pyproject.toml
# -----------------------------------------------------------------------------
ENTRYPOINT ["gpt-review"]
# Show help if no arguments supplied
CMD ["--help"]
