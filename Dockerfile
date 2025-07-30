FROM ubuntu:22.04

ARG DEBIAN_FRONTEND=noninteractive

# ── system deps (chromium only) ──────────────────────────────────────────────
RUN apt-get update -y && \
    apt-get install -y --no-install-recommends \
        python3 python3-venv python3-pip git curl wget unzip jq build-essential \
        chromium-browser ca-certificates libglib2.0-0 libnss3 libgconf-2-4 \
        libgtk-3-0 libu2f-udev libasound2 xvfb && \
    rm -rf /var/lib/apt/lists/*

# ── copy project ────────────────────────────────────────────────────────────
WORKDIR /app
COPY . /app

# ── python env & deps (includes webdriver-manager) ──────────────────────────
RUN python3 -m venv /app/venv && \
    . /app/venv/bin/activate && \
    pip install --no-cache-dir -U pip && \
    pip install --no-cache-dir . && \
    deactivate

# ── runtime ─────────────────────────────────────────────────────────────────
RUN useradd -m nonroot
USER nonroot
ENV PATH=/app/venv/bin:$PATH \
    GPT_REVIEW_PROFILE=/home/nonroot/.cache/chrome \
    GPT_REVIEW_HEADLESS=1

ENTRYPOINT ["gpt-review"]
