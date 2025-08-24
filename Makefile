###############################################################################
# GPT‑Review ▸ Developer convenience Makefile
###############################################################################
# Requirements
# ------------
# • GNU Make (pre‑installed on most Unix‑like systems)
# • Python 3.9+  (virtual‑env optional but recommended)
#
# Quality‑of‑life
# ---------------
# • Use bash with strict flags for reliability in recipes.
# • Configurable Docker binary and image tag via variables.
###############################################################################

# Use bash with "strict mode" for every recipe
SHELL := /bin/bash
.SHELLFLAGS := -euo pipefail -c

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

# Virtual‑env directory (customise via: make VENV=.venv install)
VENV ?= venv
PIP = $(VENV)/bin/pip
PY  = $(VENV)/bin/python

# Where coverage writes HTML report
COV_HTML := htmlcov/index.html

# Default instruction file for examples / docker-run (inside repo root)
INSTR ?= example_instructions.txt

# Docker knobs
DOCKER ?= docker
IMAGE  ?= gpt-review:latest

.PHONY: help install fmt precommit lint test cov e2e smoke login docker docker-run changelog clean

# ---------------------------------------------------------------------------#
# Default target
# ---------------------------------------------------------------------------#
help: ## Show this help
	@echo "$(C_INFO)Available targets:$(C_END)"
	@grep -E '^[a-zA-Z0-9_-]+:.*?## ' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[32m%-12s\033[0m %s\n", $$1, $$2}'

# ---------------------------------------------------------------------------#
# Environment setup
# ---------------------------------------------------------------------------#
$(VENV)/bin/activate: pyproject.toml ## Create a virtualenv and install dev deps
	@echo "$(C_INFO)[env] Creating venv '$(VENV)'$(C_END)"
	python -m venv $(VENV)
	$(PIP) install --upgrade pip
	$(PIP) install -e .[dev]
	@touch $@

install: $(VENV)/bin/activate ## Alias for env creation
	@echo "$(C_OK)✓ Environment ready$(C_END)"

# ---------------------------------------------------------------------------#
# Formatting & static analysis
# ---------------------------------------------------------------------------#
fmt: $(VENV)/bin/activate ## Run isort → black
	@echo "$(C_INFO)[fmt] Sorting imports (isort) …$(C_END)"
	$(VENV)/bin/isort .
	@echo "$(C_INFO)[fmt] Formatting code (black) …$(C_END)"
	$(VENV)/bin/black .
	@echo "$(C_OK)✓ Formatting complete$(C_END)"

precommit: $(VENV)/bin/activate ## Run all pre‑commit hooks locally
	@echo "$(C_INFO)[pre-commit] Running hooks …$(C_END)"
	$(VENV)/bin/pre-commit run --all-files --show-diff-on-failure
	@echo "$(C_OK)✓ Hooks passed$(C_END)"

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
    log.info("Browser version: %s", getattr(drv, "capabilities", {}).get("browserVersion"))
    drv.get("https://example.com")
    log.info("Page title: %s", drv.title)
    drv.quit()
    log.info("E2E smoke test passed ✓")
except Exception as exc:
    log.exception("E2E smoke test failed: %s", exc)
    raise SystemExit(1)
PY

# ---------------------------------------------------------------------------#
# Quick CLI sanity checks
# ---------------------------------------------------------------------------#
smoke: $(VENV)/bin/activate ## Check CLI entrypoints (version/help)
	@echo "$(C_INFO)[smoke] python -m gpt_review --version$(C_END)"
	@$(PY) -m gpt_review --version
	@echo "$(C_INFO)[smoke] gpt-review --help$(C_END)"
	@$(VENV)/bin/gpt-review --help >/dev/null && echo "$(C_OK)✓ CLI help ok$(C_END)"

# ---------------------------------------------------------------------------#
# Login helper
# ---------------------------------------------------------------------------#
login: ## Open visible browser for cookie login (respects GPT_REVIEW_LOGIN_URL)
	@echo "$(C_INFO)[login] Opening browser for cookie login …$(C_END)"
	@bash cookie_login.sh

# ---------------------------------------------------------------------------#
# Docker helpers
# ---------------------------------------------------------------------------#
docker: ## Build local Docker image (set IMAGE=mytag if desired)
	@echo "$(C_INFO)[docker] Building Docker image '$(IMAGE)' …$(C_END)"
	$(DOCKER) build -t $(IMAGE) .
	@echo "$(C_OK)✓ Docker image built$(C_END)"

docker-run: ## Run Docker image (mount profile & current repo); set INSTR=path if needed
	@echo "$(C_INFO)[docker] Running container with mounted workspace …$(C_END)"
	@echo "$(C_INFO)      INSTR file: $(INSTR) (must exist inside repo root)$(C_END)"
	@if [[ ! -f "$(INSTR)" ]]; then \
		echo "$(C_ERR)✖ INSTR '$(INSTR)' not found in $(PWD)$(C_END)"; exit 1; \
	fi
	$(DOCKER) run -it --rm \
		-v "$(HOME)/.cache/gpt-review/chrome:/home/nonroot/.cache/chrome" \
		-v "$(PWD)":/workspace \
		$(IMAGE) "/workspace/$(INSTR)" "/workspace" --cmd "pytest -q" --auto

# ---------------------------------------------------------------------------#
# Changelog helper
# ---------------------------------------------------------------------------#
changelog: ## Print “Unreleased” (or latest) section from CHANGELOG.md
	@echo "$(C_INFO)[changelog] Printing 'Unreleased' (or latest) section from CHANGELOG.md …$(C_END)"
	@python3 - <<'PY'
import re, sys, pathlib
p = pathlib.Path("CHANGELOG.md")
if not p.exists():
    print("CHANGELOG.md not found", file=sys.stderr); sys.exit(1)
text = p.read_text(encoding="utf-8")
m = re.search(r"^##\s+\[Unreleased\].*?(?=^##\s+\[|\Z)", text, re.M | re.S)
if not m:
    m = re.search(r"^##\s+\[[^\]]+\].*?(?=^##\s+\[|\Z)", text, re.M | re.S)
if not m:
    print("No sections found in CHANGELOG.md", file=sys.stderr); sys.exit(1)
print(m.group(0).rstrip())
PY
	@echo "$(C_OK)✓ Done$(C_END)"

# ---------------------------------------------------------------------------#
# Clean artefacts
# ---------------------------------------------------------------------------#
clean: ## Remove Python cache, coverage & build artefacts
	@rm -rf $(VENV) __pycache__ .pytest_cache .coverage htmlcov build dist *.egg-info
	@find . -name '*.py[cod]' -delete
	@echo "$(C_OK)✓ Cleaned$(C_END)"
