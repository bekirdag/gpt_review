# Product Definition Report (PDR)

## Overview
GPT-Review automates a review loop between a user, a git repository, and ChatGPT. The tool reads human instructions, generates one-file-at-a-time patches, runs project commands such as `pytest -q`, and feeds any failures back to the model until the suite passes. It supports both browser control of chatgpt.com and direct API access to GPT-5 Pro compatible endpoints.

## Product Vision
Deliver a dependable code review copilot that can take a plain text brief and return a clean commit history with all required fixes and documentation updates. GPT-Review should feel like a teammate that can be scripted, audited, and rerun without guesswork.

## Problem Statement
Teams currently copy and paste diffs into ChatGPT, track patches manually, and rerun tests by hand. This wastes time, loses context between turns, and often produces broken commits. GPT-Review replaces the manual loop with a guarded workflow that keeps every change reproducible and compliant with repository policies.

## Goals
- Reduce cycle time from instructions to green CI by automating the patch loop end-to-end.
- Guarantee deterministic, auditable commits by enforcing full-file updates and scoped staging.
- Provide equal first-class support for browser and API transports so teams can pick the model tier that fits cost and access.
- Capture plans, blueprints, and logs inside the repository so future maintainers understand what was changed and why.

## Non-Goals
- GPT-Review does not replace human code review or merge decisions.
- The tool does not manage infrastructure beyond cloning repositories, creating branches, and optionally pushing results.
- It does not attempt to optimise model prompts for novel domains beyond the provided blueprint summaries.

## User Personas
- **Automation Engineer**: Integrates GPT-Review into CI pipelines to generate candidate fixes overnight.
- **Product Developer**: Runs the CLI locally to address bugs or implement features while keeping tests in the loop.
- **Release Manager**: Audits generated commits, reviews plan artifacts, and decides whether to merge the branch.
- **Platform Administrator**: Maintains API keys, browser profiles, and monitors usage and cost signals.

## Key Use Cases
1. Run `gpt-review iterate instructions.txt /repo --run "pytest -q"` to generate a multi-iteration plan-first review, complete with blueprint docs and plan artifacts.
2. Execute `software_review.sh instructions.txt /repo --mode api --cmd "npm test" --auto` to drive an unattended API session that tails logs for error fixes.
3. Validate a model-generated patch payload with `gpt-review validate --payload '{...}'` before applying it in a custom integration.
4. Scan a repository snapshot using `gpt-review scan /repo --max-lines 200` to ground the model in the file manifest.

## Functional Requirements
1. Accept instructions plus a git repository path or URL; clone remote repositories into temporary worktrees automatically.
2. Enforce the JSON patch schema (create, update, delete, rename, chmod) and apply each patch via `apply_patch.py` with atomic writes.
3. Run a user-specified command after each successful patch and feed the tail of stdout/stderr back to the model on failure.
4. Generate and maintain blueprint documents (`WHITEPAPER.md`, `BUILD_GUIDE.md`, `SDS.md`, `PROJECT_INSTRUCTIONS.md`) and plan artifacts in `.gpt-review/`.
5. Provide CLI subcommands for API-driven loops, schema dumps, patch validation, repository scans, and version reporting.

## Non-Functional Requirements
- **Reliability**: Crash-safe resume (`.gpt-review-state.json`), repeatable commits, and retries for brittle browser actions.
- **Security**: Never write secrets to logs; constrain file writes to repo-relative POSIX paths and block `.git` traversal.
- **Performance**: Keep prompts within configurable byte limits (`GPT_REVIEW_MAX_PROMPT_BYTES`) and truncate logs using `GPT_REVIEW_LOG_TAIL_CHARS`.
- **Portability**: Support macOS, Linux, Docker, and Debian installer workflows; operate with Python 3.10+ and Git.
- **Observability**: Emit rotating logs with optional JSON console output and command exit codes for post-run audits.

## Success Metrics
- Median time from instructions to passing tests improves by 30 percent compared to manual prompting.
- Fewer than five percent of runs abort due to patch validation or path safety errors.
- Ninety five percent of sessions regenerate plan artifacts and logs that allow a new reviewer to replay the decision process without rerunning the model.
- API usage remains within budget by keeping `GPT_REVIEW_CTX_TURNS` at or below six and tailed logs under twenty thousand characters.

## Release Plan
| Milestone | Description | Target Outcome |
|-----------|-------------|----------------|
| M1 | Ship CLI parity (`iterate`, `api`, `scan`, `validate`, `schema`) with documentation updates. | Users can run either transport path with consistent flags. |
| M2 | Harden blueprint preflight and plan artifact generation in orchestrator and API driver. | Every run produces the four blueprints plus initial and final plan files. |
| M3 | Add optional push-and-PR support gated by `GPT_REVIEW_CREATE_PR`. | Teams can opt into automated branch publication with audit logs. |
| M4 | Expand tests (CLI smoke, patch normalization, scoped staging) and add Docker image defaults. | CI covers regression paths and container users get sane defaults. |

## Dependencies and Integrations
- **Git**: Required for repository operations, branch creation, and commit staging.
- **Chrome or Chromium**: Needed when running in browser mode; wrapper script auto-detects binaries.
- **GPT-Codex API**: Required for API mode, leveraging tool/function calls and streaming chat completions.
- **`gpt-5-codex-client` SDK**: Used by `api_driver` and `fullfile_api_driver` to communicate with the Codex endpoint.
- **`gh` CLI (optional)**: Enables pull request creation when `GPT_REVIEW_CREATE_PR=1` and credentials are configured.

## Risks and Mitigations
| Risk | Impact | Mitigation |
|------|--------|------------|
| Model returns malformed JSON or diffs | Blocks automation pipeline | `patch_validator` enforces schema; orchestrator retries with descriptive errors. |
| Browser DOM changes on chatgpt.com | Selenium actions break | Wrapper exposes configurable waits and retries; fallback to API mode. |
| Token overrun on large repos | Session becomes expensive or fails | Manifest scan, blueprint summaries, and log tail limits keep payload size under control. |
| Secret leakage in logs | Security incident | Logs avoid printing sensitive env vars; wrapper warns when API keys are missing. |
| Dirty working tree | Commits fail due to local changes | Patch applier checks git status and aborts with instructive messaging. |

## Open Questions
- How should orchestrator expose structured telemetry compatible with external monitoring stacks (Prometheus, OpenTelemetry)?
- Should browser mode support native Firefox or is Chrome/Chromium sufficient?
- What policy controls are needed to cap API usage when multiple teams run GPT-Review concurrently?

## Assumptions
- Users provide repositories with runnable tests or commands and maintain dependency isolation (virtual environments, Docker, etc.).
- Teams will review and merge generated branches manually even when the tool can push them.
- API providers support function calling, streaming, and the configured model identifiers.

## Appendices
- **Reference**: See `README.md` for quick start commands, environment variables, troubleshooting tips, and plan artifacts overview.
- **Related Docs**: `SDS.md` covers detailed architecture, and `Jira Backlog.md` enumerates sprint-level work items.
