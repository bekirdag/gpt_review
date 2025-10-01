# System Design Specification (SDS)

## 1. Introduction
### 1.1 Purpose
Document the architecture, components, data flows, and operational practices for GPT-Review so engineering teams can operate, extend, and integrate the automation confidently.

### 1.2 Scope
Covers CLI surfaces, orchestration flow, API driver, patch pipeline, data persistence, external dependencies, security, observability, and deployment considerations.

### 1.3 Definitions and Acronyms
- **Blueprints**: Repository-level markdown docs created under `.gpt-review/blueprints/` to ground the model (Whitepaper, Build Guide, SDS, Project Instructions).
- **Plan-first**: Initial context-gathering iteration that produces `INITIAL_REVIEW_PLAN.md` and `.gpt-review/initial_plan.json` before any patches.
- **Run command**: User-provided shell command (e.g., `pytest -q`) executed after each patch or iteration.
- **Patch payload**: JSON object matching `gpt_review/schema.json`, containing one-file actions (create/update/delete/rename/chmod).

## 2. System Overview
GPT-Review is a Python toolchain that automates the edit-run-fix loop. Inputs include instructions, repository path or URL, and optional command. Outputs include committed changes, logs, blueprint docs, and plan artifacts. The system offers two transports:
- **Browser mode**: Selenium automation of chatgpt.com/chat.openai.com with retries and headless support.
- **API mode**: Calls to GPT-Codex endpoints using tool/function calls with strict schema enforcement.

Core phases:
1. **Initialization**: Validate inputs, load environment, clone repo if needed.
2. **Blueprint generation**: Ensure blueprint documents exist and summarise them for prompts.
3. **Plan-first iteration**: Collect overview, run/test commands, and hints; persist plan files.
4. **Iteration loop**: For each iteration, request full-file replacements, apply via patch pipeline, commit, and optionally run commands.
5. **Error-fix loop**: Trigger run command, send failing tails, accept subsequent patches until success or limit.
6. **Finalization**: Write review plan and guide, push branch, optionally open PR, rotate logs.

## 3. Architectural Components
### 3.1 CLI Layer
- **`gpt-review`**: Main entry point with subcommands (`iterate`, `api`, `scan`, `validate`, `schema`, `version`). Handles argument parsing, environment defaults, repo resolution (including Git URLs), and dispatch.
- **`software_review.sh`**: Bash wrapper that loads `.env`, normalises mode/model arguments, manages logging, and delegates to `gpt-review` or `python -m gpt_review`.
- **`python -m gpt_review`**: Module entry point that prints a runtime banner, supports fast `--version`, and delegates to CLI for consistency.

### 3.2 Orchestration Engine
- **Modules**: `gpt_review.workflow`, `gpt_review.orchestrator`, `gpt_review.iterate`.
- **Responsibilities**: Blueprint generation, plan-first prompts, iteration branch management (`iteration1`-`iteration3`), per-file API interactions, optional run command execution, commit sequencing, optional push/PR.
- **Configuration**: Controlled via CLI flags (`--model`, `--iterations`, `--run`, `--branch-prefix`, `--remote`, `--no-push`) and environment variables (`GPT_REVIEW_MODEL`, `GPT_REVIEW_ITERATIONS`, `GPT_REVIEW_BRANCH_PREFIX`, `GPT_REVIEW_REMOTE`, `GPT_REVIEW_CREATE_PR`, `GPT_REVIEW_MAX_ERROR_ROUNDS`).

### 3.3 API Driver
- **Module**: `gpt_review.api_driver` with helper `gpt_review.api_client` and `gpt_review.fullfile_api_driver`.
- **Workflow**: Build system prompt, include blueprint summaries, call the GPT-Codex API with tool schema (`submit_patch`), apply patches, run commands, tail logs, manage conversation history (`GPT_REVIEW_CTX_TURNS`), and enforce blueprint inclusion (`GPT_REVIEW_INCLUDE_BLUEPRINTS`).
- **Offline Support**: Accepts injected client for tests (`test_api_driver_offline.py`).

### 3.4 Patch Pipeline
- **Module**: `apply_patch.py` (standalone script) and `patch_validator.py`.
- **Safety Guarantees**:
  - Validate payload against JSON Schema plus custom guards (`is_safe_repo_rel_posix`).
  - Deny `.git` writes, absolute/Windows/parent traversal paths.
  - Normalize text to LF and ensure trailing newline.
  - Accept Base64 for binary create/update via `body_b64`.
  - Commit after staging only the target file(s); skip no-op updates.
  - Support rename/chmod with restricted modes (644/755).
- **Git Integration**: Uses subprocess calls to `git add`, `git commit`, `git status`, `git diff`, ensuring clean index states.

### 3.5 Blueprint Utilities and Repo Scanning
- **Modules**: `gpt_review.blueprints_util`, `gpt_review.file_scanner`, `gpt_review.repo_scanner`, `gpt_review.fs_utils`.
- **Functions**:
  - Ensure blueprint directory exists, list missing docs, summarise contents (`summarize_blueprints`).
  - Classify files (code/doc/deferred) to decide iteration ordering.
  - Produce manifest text for prompts (`scan_repository`).

### 3.6 Logging and Telemetry
- **Module**: `gpt_review.logger` with helper functions `get_logger` and environment toggles (`GPT_REVIEW_LOG_DIR`, `GPT_REVIEW_LOG_LVL`, `GPT_REVIEW_LOG_ROT`, `GPT_REVIEW_LOG_BACK`, `GPT_REVIEW_LOG_UTC`, `GPT_REVIEW_LOG_JSON`).
- **Behaviour**: Daily rotating file handler by default, optional JSON console emission, consistent formatting across modules.
- **Wrapper Logging**: `software_review.sh` writes combined stdout/stderr to log files when enabled (default `logs/wrapper-YYYYMMDD-HHMMSS.log`).

## 4. Data Design
### 4.1 Runtime State
- `.gpt-review-state.json`: Tracks session progress for crash-safe resume.
- `.gpt-review/initial_plan.json`, `INITIAL_REVIEW_PLAN.md`: Persist plan-first results.
- `.gpt-review/review_plan.json`, `REVIEW_GUIDE.md`: Final iteration plan outputs.
- `.gpt-review/blueprints/*.md`: Canonical blueprint documents accessible to the model.

### 4.2 Logs and Artifacts
- `logs/`: Rotating application logs (configurable directory).
- Wrapper log file (if logging enabled) capturing CLI output, command results, environment dump.
- Optional PR metadata when `GPT_REVIEW_CREATE_PR` triggers GitHub CLI commands.

### 4.3 Optional External Storage
- Conceptual SQL schema defined in `db/schema.sql` for a central orchestration service (sessions, patches, command runs, logs). Not required for CLI operation but available for future managed service deployment.

## 5. External Interfaces
### 5.1 CLI Commands
- `gpt-review iterate <instructions> <repo>`
- `gpt-review api <instructions> <repo>`
- `gpt-review scan <repo> [--max-lines N]`
- `gpt-review validate --payload JSON | --file PATH`
- `gpt-review schema`
- `software_review.sh ...` with wrapper options, `.env` loading, `--api` shorthand, `--env-dump`.

### 5.2 API Contract
- Logical API described in `openapi/openapi.yaml` with endpoints to create sessions, submit patches, run commands, and retrieve logs for managed-service scenarios.

### 5.3 Filesystem Contracts
- expects writable repo directory, read access to instructions, and optional `.env` file.
- Browser mode requires Chrome profile dir (default `~/.cache/gpt-review/chrome`) unless overridden.

## 6. Security and Compliance
- **Data Handling**: Avoid logging sensitive env variables; wrapper prints placeholders for API keys.
- **Path Restrictions**: `apply_patch.py` and orchestrator enforce repo-relative POSIX paths, reject `.git` writes, and check for locally modified files before destructive operations.
- **Credentials**: API key provided via `GPT_CODEX_API_KEY` (legacy `OPENAI_API_KEY`); login cookies stored in user-controlled directories; root sessions auto-add `--no-sandbox` for Chrome.
- **Least Privilege**: Wrapper validates git availability, instructions readability; fails fast when prerequisites missing.
- **Auditability**: Commit-by-commit history with logs and plan artifacts allows human review before merges.

## 7. Performance Considerations
- Manage prompt size with `GPT_REVIEW_MAX_PROMPT_BYTES` and `GPT_REVIEW_HEAD_TAIL_BYTES` (head/tail truncation).
- Limit log payload sizes with `GPT_REVIEW_LOG_TAIL_CHARS` (API) and `GPT_REVIEW_CHUNK_SIZE` (browser).
- Control runtime budget via `GPT_REVIEW_ITERATIONS`, `GPT_REVIEW_MAX_ERROR_ROUNDS`, and per-command `--timeout`/`GPT_REVIEW_COMMAND_TIMEOUT`.
- Browser mode uses Selenium waits (`GPT_REVIEW_WAIT_UI`, `GPT_REVIEW_STREAM_IDLE_SECS`, `GPT_REVIEW_RETRIES`) to balance speed and stability.

## 8. Observability
- Logs include timestamps, levels, and module identifiers for filtering.
- Wrapper environment dump shows effective mode, model, command timeout, and key env toggles.
- API driver logs run command status, exit codes, and truncated output.
- Dev commands (`make smoke`, `make test`, `make e2e`) provide quick health checks.

## 9. Deployment and Operations
- **Local**: Use `make install` to create venv, `software_review.sh` or `gpt-review` for runs.
- **Docker**: `docker build` using provided Dockerfile; defaults to headless browser mode.
- **Debian Install**: `install.sh` script installs dependencies, clones repo to `/opt/gpt-review`, sets up launchers (`gpt-review`, `software_review.sh`, `cookie_login.sh`, `gpt-review-update`).
- **CI**: `.github/workflows/ci.yml` runs lint/tests across Python 3.10-3.12; `.github/workflows/e2e.yml` executes Selenium smoke tests.
- **Updates**: `update.sh` refreshes local install while preserving venv and launchers.
- **Recovery**: Delete `.gpt-review-state.json` for stuck sessions; rerun `cookie_login.sh` for expired cookies.

## 10. Testing Strategy
- Unit tests covering patch validation, logger import behaviour, scoped staging, API driver offline scenarios.
- CLI smoke tests verifying `python -m gpt_review --version` and `gpt-review --help` paths.
- Integration tests via `make e2e` (headless Selenium) and `make smoke` (CLI entrypoints).
- Future enhancements include API-mode smoke tests using mocked backend (tracked in README and backlog).

## 11. Risks and Mitigations
| Risk | Area | Mitigation |
|------|------|------------|
| API rate limiting or downtime | External dependency | Retries, configurable timeouts, ability to switch to browser mode temporarily. |
| Repo contains large binaries | Performance | Use `body_b64` with caution; consider ignoring large files via repo scanner or instructions. |
| Browser environment constraints (snap confinement, root) | Operations | Wrapper auto-detects profiles, provides guidance in README, and sets `--no-sandbox` when required. |
| Misconfigured commands causing hangs | Runtime | `--timeout`/`GPT_REVIEW_COMMAND_TIMEOUT` enforce kill switches; logs capture timeouts clearly. |
| Concurrent sessions overwriting state | Multi-user | Repos should be run in isolated worktrees; PR creation disabled by default. |

## 12. Future Work
- Expose structured telemetry (JSON logs, metrics endpoints) for observability platforms.
- Support alternative browsers (Firefox) or headless driver options.
- Add API-mode smoke test workflow and mocks for deterministic regression coverage.
- Explore fine-grained plan diffing and blueprint version management to reduce token usage.

## 13. References
- `README.md` for quick starts, environment variables, troubleshooting.
- `docs/PDR.md` for product vision and roadmap.
- `docs/Jira Backlog.md` for sprint planning.
- `openapi/openapi.yaml` for conceptual managed service API.
- `db/schema.sql` for optional orchestration database schema.
