###############################################################################
#  GPT‑Review ▸ Production container image
###############################################################################
#  * Multi‑arch (amd64 & arm64) because we rely on `webdriver‑manager`
#    to download a matching chromedriver at **runtime**, not build‑time.
#  * Image size ≈ 480 MB compressed – Chromium is the heavyweight.
#
#  Build:
#      docker build -t gpt-review .
#
#  Run (example):
#      docker run -it --rm \
#         -v $HOME/.cache/gpt-review/chrome:/home/nonroot/.cache/chrome \
#         -v $(pwd):/workspace \
#         gpt-review instructions.txt /workspace \
#         --cmd "pytest -q" --auto
#
#  The container is headless by default (`GPT_REVIEW_HEADLESS=1`) so no
#  display server is required **inside** the container.
###############################################################################

# ---------------------------------------------------------------------------
#  Base – Ubuntu 22.04 LTS
# ---------------------------------------------------------------------------
    FROM ubuntu:22.04

    ARG DEBIAN_FRONTEND=noninteractive
    
    # ---------------------------------------------------------------------------
    #  System dependencies
    # ---------------------------------------------------------------------------
    # • chromium-browser      – GUI browser (runs headless via --headless=new flag)
    # • xvfb                  – optional virtual framebuffer if user wants GUI
    # • build-essential etc.  – required by some Python wheels with native code
    # ---------------------------------------------------------------------------
    RUN apt-get update -y && \
        apt-get install -y --no-install-recommends \
            python3 python3-venv python3-pip git curl wget unzip ca-certificates \
            chromium-browser libglib2.0-0 libnss3 libgconf-2-4 libgtk-3-0 \
            libu2f-udev libasound2 xvfb build-essential && \
        rm -rf /var/lib/apt/lists/*
    
    # ---------------------------------------------------------------------------
    #  Create a non‑root user for security
    # ---------------------------------------------------------------------------
    RUN useradd -m nonroot
    
    # ---------------------------------------------------------------------------
    #  Copy project into /app
    # ---------------------------------------------------------------------------
    WORKDIR /app
    COPY . /app
    
    # ---------------------------------------------------------------------------
    #  Python virtual‑environment
    # ---------------------------------------------------------------------------
    RUN python3 -m venv /app/venv && \
        . /app/venv/bin/activate && \
        pip install --no-cache-dir -U pip && \
        pip install --no-cache-dir . && \
        deactivate
    
    # ---------------------------------------------------------------------------
    #  Environment variables
    # ---------------------------------------------------------------------------
    ENV PATH=/app/venv/bin:$PATH \
        GPT_REVIEW_PROFILE=/home/nonroot/.cache/chrome \
        GPT_REVIEW_HEADLESS=1 \
        PYTHONUNBUFFERED=1
    
    # ---------------------------------------------------------------------------
    #  Permissions – ensure nonroot can write profile & logs
    # ---------------------------------------------------------------------------
    RUN mkdir -p /home/nonroot/.cache/chrome /app/logs && \
        chown -R nonroot:nonroot /home/nonroot /app/logs
    
    USER nonroot
    
    # ---------------------------------------------------------------------------
    #  Entrypoint
    # ---------------------------------------------------------------------------
    #   `gpt-review` is exposed via [project.scripts] in pyproject.toml
    # ---------------------------------------------------------------------------
    ENTRYPOINT ["gpt-review"]
    CMD ["--help"]  # show help if no arguments supplied
    