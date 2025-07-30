# GPT‑Review

**Automated, ChatGPT‑driven code‑review loop**  
✓ patches a live Git repository one file at a time  
✓ runs your test or build command after every patch  
✓ feeds failing logs back to ChatGPT until the command passes  
✓ survives crashes & resumes exactly where it left off  

---

## Table of contents
1. [Requirements](#requirements)  
2. [Installation](#installation)  
   * [Ubuntu 22.04+ script](#1-one‑line‑script)  
   * [Python‑only install](#2‑python‑only)  
   * [Docker](#3‑docker)  
3. [First‑time login](#first‑time-login)  
4. [Usage](#usage)  
   * [Basic](#basic)  
   * [Full CLI options](#full‑cli‑options)  
   * [JSON contract returned by ChatGPT](#json-contract)  
   * [Crash‑safe resume](#crash‑safe-resume)  
5. [Advanced](#advanced)  
   * [Headless mode](#headless-mode)  
   * [Custom Chrome profile](#custom-chrome-profile)  
6. [Development](#development)  
7. [License](#license)

---

## Requirements
| Component | Version / note |
|-----------|----------------|
| **OS**    | Linux, macOS or Windows‑WSL. Ubuntu 22.04 instructions below. |
| **Python**| 3.9 + |
| **Chromium** | Any recent version. The project uses *webdriver‑manager* to fetch the matching driver automatically. |
| **OpenAI account** | You must be able to log in at <https://chat.openai.com/> with your browser. |

---

## Installation

### 1. One‑line script (Ubuntu 22.04+)
```bash
curl -sSL https://raw.githubusercontent.com/your‑org/gpt-review/main/install.sh | sudo bash
```
Installs system packages, clones this repo to /opt/gpt-review, creates a
virtualenv, installs Python deps, and exposes the gpt-review command in
/usr/local/bin.

### 2. Python‑only
```bash
git clone https://github.com/your‑org/gpt-review.git
cd gpt-review
python -m venv venv && . venv/bin/activate
pip install -U pip
pip install -e .            # installs selenium + webdriver‑manager etc.
```
### 3. Docker
```bash
docker build -t gpt-review .
docker run -it --rm \
  -v $HOME/.cache/gpt-review/chrome:/home/nonroot/.cache/chrome \
  -v $(pwd):/workspace \
  gpt-review instructions.txt /workspace \
  --cmd "pytest -q" --auto
  ```

The container runs headless (GPT_REVIEW_HEADLESS=1) and works on
amd64 and arm64 thanks to webdriver‑manager.

## First‑time login
GPT‑Review drives your real browser session. Run once to store cookies:

```bash

./cookie_login.sh
```

A Chromium window opens – sign in to https://chat.openai.com, close the
window. Cookies are stored in
~/.cache/gpt-review/chrome (override with GPT_REVIEW_PROFILE).

## Usage
### Basic
```bash

# interactive session, ask <Enter> between patches
gpt-review instructions.txt /path/to/git/repo

# fully automatic, run pytest after each patch
gpt-review instructions.txt /path/to/git/repo \
            --cmd "pytest -q" --auto
````

instructions.txt is plain text that tells ChatGPT what you want
(e.g. “Upgrade codebase to Python 3.12 and reach 90 % coverage.”).

### Full CLI options
Flag	Default	Meaning
instructions.txt	—	Plain‑text instructions shown to ChatGPT once.
/path/to/repo	—	Path to a local Git repository (must have .git).
--cmd "<shell>"	(none)	Command to run after each patch. The loop stops only when this command exits 0 and JSON status = completed.
--auto	off	Send continue automatically; otherwise press <Enter> manually.
--timeout N	300 s	Kill --cmd if it runs longer than N seconds.

### JSON contract

Each ChatGPT reply must be exactly one JSON object (no extra text):

```jsonc

{
  "op": "create" | "update" | "delete" | "rename" | "chmod",
  "file": "relative/path",
  // create / update  →  body  *or*  body_b64
  "body": "full text",
  "body_b64": "<base64 bytes>",
  // rename → target   |  chmod → mode (644 / 755)
  "target": "new/path",
  "mode": "755",
  "status": "in_progress" | "completed"
}
````

* in_progress → driver sends continue
* completed → session ends

### Crash‑safe resume
After every successful patch, the driver writes
.gpt-review-state.json in the repo:

```json

{
  "conversation_url": "https://chat.openai.com/c/...",
  "last_commit": "3e2b1c4...",
  "timestamp": 1729973744
}
```

If the program, browser or VM crashes:

```bash

gpt-review instructions.txt /path/to/repo --cmd "pytest -q" --auto
```
The driver re‑opens the saved conversation, verifies the commit hash and
continues automatically.
Delete the state file to start fresh.

## Advanced
### Headless mode
```bash

export GPT_REVIEW_HEADLESS=1

gpt-review ...
```

Runs Chromium without a visible window (still needs a display server
inside Docker/CI – we use xvfb-run on GitHub Actions).

### Custom Chrome profile
```bash

export GPT_REVIEW_PROFILE=/tmp/my‑chat‑cookies
```

Use a different cookie / cache directory (path is created if missing).

### Development
```bash

make test            # unit tests
make lint            # flake8
pre-commit install   # optional git hooks
```
The full dev environment:

```bash

python -m venv venv && . venv/bin/activate
pip install -e .[dev]        # installs black, flake8, pytest, coverage, pre‑commit
Continuous integration runs unit tests, coverage, lint and a
headless browser smoke test (.github/workflows/e2e.yml).
```

### License
MIT – see LICENSE for the full text.