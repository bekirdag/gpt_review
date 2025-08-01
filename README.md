# GPT‑Review

**Browser‑driven, ChatGPT‑powered code‑review loop**  
Edit → Run → Fix — until your tests pass.

<p align="center">
  <img src="https://raw.githubusercontent.com/your-org/gpt-review/main/docs/assets/diagram.svg" width="600" alt="GPT‑Review architecture diagram"/>
</p>

| Feature | Status |
|---------|--------|
| One‑file‑at‑a‑time patches | ✅ |
| ChatGPT must ask “continue” between chunks | ✅ |
| Runs any shell command after each patch | ✅ |
| Feeds failing logs back to ChatGPT | ✅ |
| Delete / Rename / Chmod ops | ✅ |
| Binary file support (`body_b64`) | ✅ |
| Crash‑safe resume | ✅ |
| Daily‑rotating logs | ✅ |
| Multi‑arch Docker | ✅ |

---

## Table of Contents
1. [How it works](#how-it-works)  
2. [Quick start](#quick-start)  
3. [Installation](#installation)  
4. [First‑time login](#first-time-login)  
5. [Usage](#usage)  
6. [Session rules](#session-rules)  
7. [Advanced](#advanced)  
8. [Development](#development)  
9. [Contributing](CONTRIBUTING.md)  
10. [License](#license)  

---

## How it works
```mermaid
sequenceDiagram
    participant You
    participant GPT‑Review
    participant ChatGPT
    participant Tests

    You->>GPT‑Review: instructions.txt<br>/repo --cmd "pytest -q"
    GPT‑Review->>ChatGPT: initial prompt (+session rules)
    ChatGPT-->>GPT‑Review: JSON patch (file, body, status)
    GPT‑Review->>Repo: apply patch & commit
    GPT‑Review->>Tests: run command
    Tests-->>GPT‑Review: pass / fail
    alt fail
        GPT‑Review->>ChatGPT: full error log
        ChatGPT-->>GPT‑Review: next patch
    end
    alt status = completed & tests pass
        GPT‑Review-->>You: "All done!"
    else
        GPT‑Review->>ChatGPT: "continue"
        ChatGPT-->>GPT‑Review: next patch
    end
```

---

## Quick start

```bash
# 1. install system deps + package (one‑liner, needs sudo)
curl -sSL https://raw.githubusercontent.com/your-org/gpt-review/main/install.sh | sudo bash

# 2. log in to ChatGPT once
cookie_login.sh

# 3. run interactive review (wrapper script)
software_review.sh instructions.txt /path/to/git/repo --cmd "pytest -q"
```

Set `--auto` if you want GPT‑Review to press **continue** automatically after each chunk.

---

## Installation

### Ubuntu 22.04 (one‑liner)

```bash
curl -sSL https://raw.githubusercontent.com/your-org/gpt-review/main/install.sh | sudo bash
```

### pip / virtual‑env

```bash
git clone https://github.com/your-org/gpt-review.git
cd gpt-review
python -m venv venv && . venv/bin/activate
pip install -e .[dev]
```

### Docker

```bash
docker build -t gpt-review .
docker run -it --rm       -v $HOME/.cache/gpt-review/chrome:/home/nonroot/.cache/chrome       -v $(pwd):/workspace       gpt-review instructions.txt /workspace       --cmd "pytest -q" --auto
```

---

## First‑time login
Run the helper once:

```bash
cookie_login.sh
```

A Chromium window opens. Sign in to https://chat.openai.com
then **close** the window. Cookies are stored in  
`~/.cache/gpt-review/chrome` (override with `GPT_REVIEW_PROFILE`).

---

## Usage

### Minimal
```bash
software_review.sh instructions.txt /repo
```

### Full CLI
| Flag | Default | Purpose |
|------|---------|---------|
| `instructions.txt` | — | Plain‑text goals for ChatGPT |
| `/repo` | — | Path to local **Git** repository |
| `--cmd "pytest -q"` | _(none)_ | Command must exit 0 before loop stops |
| `--auto` | off | Auto‑send **continue** after each patch |
| `--timeout 600` | 300 | Kill command after *N* seconds |

### JSON contract
```jsonc
{ "op": "create|update|delete|rename|chmod",
  "file": "relative/path",
  "body": "text file contents",
  "body_b64": "<base64>",        // binary
  "target": "new/path",          // rename
  "mode": "755",                 // chmod
  "status": "in_progress|completed" }
```

---

## Session rules
1. **One patch per reply** – ChatGPT must modify **one file at a time**.  
2. **Ask before next chunk** – After sending a patch, ChatGPT must say  
   *“Let me know when to continue”* (or similar).  
3. GPT‑Review replies *continue* automatically when `--auto` is set, or waits
   for you to press **Enter**.

These rules keep commit history readable and make rollbacks trivial.

---

## Advanced

### Crash‑safe resume
A state‑file `.gpt-review-state.json` is written after every successful patch.
Re‑run the same command to resume; delete the file to start fresh.

### Headless mode
```bash
export GPT_REVIEW_HEADLESS=1
software_review.sh ...
```
Requires X‑server in CI (`xvfb-run` used on GitHub Actions).

### Custom log directory
```bash
export GPT_REVIEW_LOG_DIR=/var/log/gpt-review
```

---

## Development

```bash
make install    # create venv + install dev deps
make lint       # flake8 + codespell
make test       # pytest + coverage
make e2e        # headless browser smoke test
```

Pre‑commit hooks ensure Black, isort, flake8 run on every commit.

---

## License
MIT © GPT‑Review Team.  See [LICENSE](LICENSE) for details.
