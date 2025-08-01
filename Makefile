###############################################################################
# GPT‑Review ▸ Developer convenience Makefile
###############################################################################
# Requirements
# ------------
# • GNU Make (pre‑installed on most Unix‑like systems)
# • Python 3.9 +  (virtual‑env optional but recommended)
#
# Colours
# -------
# We add a minimal colour helper ($(C_INFO), $(C_OK), …) to make the output
# readable.  Falls back to plain text when stdout is not a TTY.
###############################################################################

ifndef NO_COLOR
C_INFO := \033[34m
C_OK   := \033[32m
C_WARN := \033[33m
C_ERR  := \033[31m
C_END  := \033[0m
else
C_INFO :=
C_OK   :=
C_WARN :=
C_ERR  :=
C_END  :=
endif

# Virtual‑env directory (customise via `make VENV=.venv install`)
VENV ?= venv
PIP = $(VENV)/bin/pip
PY  = $(VENV)/bin/python

# Where coverage writes HTML report
COV_HTML := htmlcov/index.html

.PHONY: help install lint test cov e2e clean docker

# ---------------------------------------------------------------------------#
# Default target
# ---------------------------------------------------------------------------#
help:
	@echo "$(C_INFO)Available targets:$(C_END)"
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[32m%-10s\033[0m %s\n", $$1, $$2}'

# ---------------------------------------------------------------------------#
# Environment setup
# ---------------------------------------------------------------------------#
$(VENV)/bin/activate: pyproject.toml ## Create a virtualenv and install deps
	@echo "$(C_INFO)[env] Creating venv '$(VENV)'$(C_END)"
	python -m venv $(VENV)
	$(PIP) install --upgrade pip
	$(PIP) install -e .[dev]
	@touch $@

install: $(VENV)/bin/activate ## Alias for env creation
	@echo "$(C_OK)✓ Environment ready$(C_END)"

# ---------------------------------------------------------------------------#
# Static analysis
# ---------------------------------------------------------------------------#
lint: $(VENV)/bin/activate ## Run flake8 + codespell
	@echo "$(C_INFO)[lint] Running flake8 …$(C_END)"
	$(VENV)/bin/flake8
	@echo "$(C_INFO)[lint] Running codespell …$(C_END)"
	$(VENV)/bin/codespell --ignore-words-list="te,ht" --skip="*.png,*.svg"
	@echo "$(C_OK)✓ Lint clean$(C_END)"

# ---------------------------------------------------------------------------#
# Unit tests
# ---------------------------------------------------------------------------#
test: $(VENV)/bin/activate ## Run pytest with coverage (terminal report)
	@echo "$(C_INFO)[test] Running pytest …$(C_END)"
	$(VENV)/bin/coverage run -m pytest -q
	$(VENV)/bin/coverage report -m

cov: test ## Generate HTML coverage report
	@echo "$(C_INFO)[cov] Generating HTML coverage report …$(C_END)"
	$(VENV)/bin/coverage html
	@echo "$(C_OK)Open $(COV_HTML) in your browser$(C_END)"

# ---------------------------------------------------------------------------#
# End‑to‑End smoke test (headless browser)
# ---------------------------------------------------------------------------#
e2e: $(VENV)/bin/activate ## Launch headless Chromium via Selenium
	@echo "$(C_INFO)[e2e] Smoke testing browser layer …$(C_END)"
	@GPT_REVIEW_HEADLESS=1 xvfb-run -a $(PY) - <<'PY'
from review import _chrome_driver
from logger import get_logger
log = get_logger("e2e")
try:
    drv = _chrome_driver()
    log.info("Browser version: %s", drv.capabilities["browserVersion"])
    drv.get("https://example.com")
    log.info("Page title: %s", drv.title)
    drv.quit()
    log.info("E2E smoke test passed ✓")
except Exception as exc:
    log.exception("E2E smoke test failed: %s", exc)
    raise SystemExit(1)
PY

# ---------------------------------------------------------------------------#
# Docker helpers
# ---------------------------------------------------------------------------#
docker: ## Build local docker image
	@echo "$(C_INFO)[docker] Building Docker image 'gpt-review:latest' …$(C_END)"
	docker build -t gpt-review .
	@echo "$(C_OK)✓ Docker image built$(C_END)"

# ---------------------------------------------------------------------------#
# Clean artefacts
# ---------------------------------------------------------------------------#
clean: ## Remove Python cache, coverage & build artefacts
	rm -rf $(VENV) __pycache__ .pytest_cache .coverage htmlcov build dist *.egg-info
	find . -name '*.py[cod]' -delete
	@echo "$(C_OK)✓ Cleaned$(C_END)"
