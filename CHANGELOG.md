# Changelog

All notable changes to **GPT‑Review** are documented in this file.  
The format is inspired by [Keep a Changelog](https://keepachangelog.com/) and the project follows [Semantic Versioning](https://semver.org/).

---

## [Unreleased]

### Changed
- **cookie_login.sh** — honors `GPT_REVIEW_LOGIN_URL` for the primary tab (default remains `https://chatgpt.com/`), keeps a `chat.openai.com` fallback with de‑duplication, and improves user guidance; login stays **non‑headless** by design.
- **review.py** — increased DOM resilience (additional textarea selectors with aria/placeholder hints and a contenteditable fallback), robust draft clearing (Select‑All + Backspace) before sending, safer assistant‑block selection, and reply completion gated on non‑empty text.
- **.env.example** — clarified the distinction between `GPT_REVIEW_LOGIN_URL` (login helper) and `GPT_REVIEW_CHAT_URL` (driver navigation).
- **README.md** — documented the login override and aligned first‑time login guidance.
- **Dockerfile** — cosmetic cleanups, consistent indentation, explicit `set -eux` build logging, and minor environment flag improvements.

### Fixed
- **.dockerignore** — removed duplicate `.env` pattern and tightened comments.

> Note: Version remains **0.3.0** in `pyproject.toml` / `__init__.py`. These entries will be rolled into the next tagged release (e.g., `0.3.1`).

---

## [0.3.0] — 2025‑08‑01

### Added
- **Chrome/Chromium auto‑detection** and correct driver stream selection (Google vs Chromium) in `review.py`. Logs browser & driver versions for support.
- **Environment tunables** (documented in `.env.example` and README):
  - `GPT_REVIEW_CHAT_URL`, `GPT_REVIEW_WAIT_UI`, `GPT_REVIEW_STREAM_IDLE_SECS`,
    `GPT_REVIEW_RETRIES`, `GPT_REVIEW_CHUNK_SIZE`, `GPT_REVIEW_COMMAND_TIMEOUT`.
- **CLI smoke tests** for entry points (`tests/test_cli_entrypoints.py`).

### Changed
- **Primary login domain → `https://chatgpt.com/`** with `https://chat.openai.com/` fallback in `cookie_login.sh` and driver navigation.
- **Dockerfile** rebuilt on **Debian 12 (slim)** with system **Chromium**; sets `CHROME_BIN=/usr/bin/chromium`, headless by default, non‑root user.
- **CI**: Lint job now runs **pre‑commit** (isort, black, flake8, codespell, JSON/YAML/TOML checks). Unit test matrix kept for py3.10/3.11/3.12.
- **E2E workflow** installs **Google Chrome Stable** via `browser-actions/setup-chrome` and validates Selenium startup.
- **Makefile**: new targets `fmt`, `precommit`, `smoke`, `login`, `docker-run`; clearer logging.
- **Wrapper** (`software_review.sh`): dotenv support, environment dump, fresh-session flag, tee logs, robust runner fallback.
- **Module entrypoint** (`gpt_review/__main__.py`): non‑recursive, fast `--version`, structured logging.
- **README** overhauled: updated domains, Docker, env vars, troubleshooting.

### Fixed
- **JSON fence parsing** in extractor tests: proper ```json fences; improved robustness.
- **Unsafe chmod test**: avoids double‑create; asserts permissions unchanged on failure.
- Minor typos and logging improvements across scripts.
- Installer hardening (`install.sh`): idempotent, best‑effort Chromium install, clearer guidance.

---

## [0.2.0]

Foundational release with the browser‑driven **edit → run → fix** loop:
- One‑file‑at‑a‑time patching protocol (create/update/delete/rename/chmod).
- JSON‑Schema validation for assistant patches.
- Git commit per operation with safety checks (path traversal, local changes).
- Error log chunking back to ChatGPT.
- Basic CI (flake8 + pytest) and packaging metadata.
