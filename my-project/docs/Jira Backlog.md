# Jira Backlog

## Sprint 1 – Bootstrap Automation
- **GPTREV-1** – Set up `software_review.sh` wrapper logging defaults and `.env` loading flow. _Acceptance_: wrapper can run instructions file with --env-dump, persists logs to `logs/`.
- **GPTREV-2** – Implement blueprint preflight in API driver and orchestrator, ensuring `.gpt-review/blueprints/*.md` exist. _Acceptance_: missing docs generated via patch pipeline.
- **GPTREV-3** – Harden `apply_patch.py` path validation and commit scoping. _Acceptance_: rename/delete/chmod guarded; .git traversal blocked.

## Sprint 2 – Resilient Iterations
- **GPTREV-4** – Add plan-first artifacts (`INITIAL_REVIEW_PLAN.md`, `.gpt-review/initial_plan.json`). _Acceptance_: files written before iteration 1 commit.
- **GPTREV-5** – Implement iteration branches and optional push/PR workflow. _Acceptance_: branches `iteration1-3` created; `--no-push` respected.
- **GPTREV-6** – Tail failing command output and resend to model. _Acceptance_: logs truncated to `GPT_REVIEW_LOG_TAIL_CHARS`, visible in transcripts.

## Sprint 3 – CLI & API Parity
- **GPTREV-7** – Provide `gpt-review scan`, `schema`, and `validate` subcommands. _Acceptance_: CLI help shows new subcommands; unit tests cover entry points.
- **GPTREV-8** – Support Git URLs by cloning into temp directories. _Acceptance_: CLI accepts https/git URLs; cleans temp dirs after run.
- **GPTREV-9** – Add JSON console logging (`GPT_REVIEW_LOG_JSON`) and UTC timestamps toggle.

## Sprint 4 – Quality & Observability
- **GPTREV-10** – Add Selenium retries and idle timeout tuning. _Acceptance_: configurable via `GPT_REVIEW_WAIT_UI`, `GPT_REVIEW_RETRIES`.
- **GPTREV-11** – Expand test suite (CLI smoke, patch normalization, scoped staging).
- **GPTREV-12** – Package Docker image with headless defaults and document usage.

