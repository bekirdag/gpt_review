<!--
===============================================================================
 🛠️  GPT‑Review ▸ Contribution Guidelines
===============================================================================
Thank‑you for considering contributing ♥

This document covers **everything you need to know** to file great issues,
submit high‑quality pull‑requests, and understand how releases are made.

> _New to open‑source?_  Don’t worry – we love first‑timers!  
> Visit <https://opensource.guide/how-to-contribute/> for a quick primer.
===============================================================================
-->

## Table of Contents
1. [Code of Conduct](#code-of-conduct)  
2. [How to Ask a Good Question](#how-to-ask-a-good-question)  
3. [Development Quick‑Start](#development-quick-start)  
4. [Coding Standards](#coding-standards)  
5. [Pre‑Commit Hooks](#pre-commit-hooks)  
6. [Writing Tests](#writing-tests)  
7. [Commit Message Style](#commit-message-style)  
8. [Branching & PR Process](#branching--pr-process)  
9. [Release Process](#release-process)  
10. [Security Policy](#security-policy)  
11. [Community Chat](#community-chat)  

---

## Code of Conduct
We follow the **[Contributor Covenant v2.1](https://www.contributor-covenant.org)**.  
Be respectful, inclusive & constructive.  Violations may result in a ban.

---

## How to Ask a Good Question
Before opening an Issue:

1. **Search** the tracker and README – maybe it’s answered.
2. Provide:
   * Your OS & Python versions
   * `pip show gpt-review` version
   * Exact **steps to reproduce** (commands, patches, logs)
3. Use *markdown fences* for logs:  
   <pre>```text  
   full stack trace  
   ```</pre>

---

## Development Quick‑Start
```bash
git clone https://github.com/your‑org/gpt-review.git
cd gpt-review
python -m venv venv && . venv/bin/activate
pip install -e .[dev]           # pytest, black, flake8, pre‑commit
pre-commit install              # auto‑format on commit
make test                       # run unit tests
```

Need Chromium? Ubuntu: `sudo apt install chromium-browser`.  
macOS: `brew install chromium`.

---

## Coding Standards
| Tool  | Purpose | Config |
|-------|---------|--------|
| **Black** | Auto‑formatter | implicit (line‑len 88) |
| **isort** | Import order  | `pyproject.toml` |
| **flake8** | Linting (PEP8 + bugbear/bandit) | `.flake8` |
| **pytest** | Unit tests | implicit |

Run `make lint` to format & lint everything.

---

## Pre‑Commit Hooks
We use **pre‑commit**.  
After `pre-commit install`, each git commit will:

1. Re‑order imports (`isort`)
2. Re‑format code (`black`)
3. Lint (`flake8`)
4. Strip trailing whitespace & add EOF newline
5. Spell‑check docs/comments (`codespell`)

Fixes happen automatically; otherwise the commit is rejected.

---

## Writing Tests
* Unit tests live under `tests/`.
* Use `tmp_path` fixture for filesystem operations.
* Keep each test < 50 ms; heavy browser tests belong to the E2E suite.
* Run `pytest -q` locally; CI shows coverage.

---

## **New Session Rule (v0.3.0)**
When adding or adjusting *instruction files* for example projects, remember:

> **ChatGPT must patch one file per reply and ask the user to _continue_ before
> sending the next patch.**

This rule keeps commit history granular and predictable.  
If you modify the driver prompt or schema, update associated unit tests
(`tests/test_session_rules.py`).

---

## Commit Message Style
```text
<type>(<scope>): <subject>   # <= 50 chars

<body>                       # wrapped 72 chars
<footer>                     # optional, e.g. "Fixes #123"
```
**Types:** `fix`, `feat`, `docs`, `test`, `build`, `chore`, `refactor`.

Example:
```text
feat(review): enforce chunk‑by‑chunk session rule
```

---

## Branching & PR Process
* **main** – always stable; CI must be green.
* **feature/<topic>** – new work.
* **fix/<bug‑id>** – bug fixes.

### Pull‑Requests
* Mark as **Draft** until all unit tests pass locally.
* Tick the PR checklist:

  - [ ] Code formatted (`pre-commit run --all-files`)
  - [ ] Tests added / updated
  - [ ] CI green
  - [ ] README / CHANGELOG updated

* At least one approving review required.

---

## Release Process
1. Maintainer bumps `version` in `pyproject.toml` & adds a CHANGELOG entry.
2. Tag commit: `git tag -s vX.Y.Z`.
3. GitHub Action builds wheel & uploads to PyPI.

---

## Security Policy
Found a vulnerability? **Do not open a public issue.**  
Email <security@gpt‑review.dev> with steps to reproduce.

---

## Community Chat
Questions? Ideas? Join the GitHub Discussions tab or ping  
@maintainer on Matrix `#gpt-review:matrix.org`.

Happy hacking 💙
