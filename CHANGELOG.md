###############################################################################
# GPT‑Review ▸ Project Metadata & Build Configuration
###############################################################################
# This file powers **PEP 517/518** builds (`python -m build`) as well as
# editable installs (`pip install -e .`).  Comments are verbose by design so
# newcomers can understand *why* each section exists.
###############################################################################

# ─────────────────────────────────────────────────────────────────────────────
#  Build back‑end
# ─────────────────────────────────────────────────────────────────────────────
[build-system]
requires      = ["setuptools>=68", "wheel"]
build-backend = "setuptools.build_meta"

# ─────────────────────────────────────────────────────────────────────────────
#  Core package metadata
# ─────────────────────────────────────────────────────────────────────────────
[project]
name            = "gpt-review"
version         = "0.3.0"                        # ↞ bumped from 0.2.0
description     = "Browser‑driven, ChatGPT‑powered code‑review loop with auto‑test execution."
readme          = "README.md"
license         = { file = "LICENSE" }
requires-python = ">=3.9"

authors = [
  { name = "GPT‑Review Team", email = "opensource@gpt-review.dev" }
]

keywords = ["chatgpt", "selenium", "code‑review", "automation", "dev‑tool"]

classifiers = [
  "Programming Language :: Python :: 3",
  "Programming Language :: Python :: 3 :: Only",
  "License :: OSI Approved :: MIT License",
  "Intended Audience :: Developers",
  "Operating System :: OS Independent",
]

# ─────────────────────────────────────────────────────────────────────────────
#  Runtime dependencies (PyPI)
# ─────────────────────────────────────────────────────────────────────────────
dependencies = [
  # Browser automation
  "selenium>=4.21.0",
  "webdriver-manager>=4.0.1",

  # JSON‑Schema validation
  "jsonschema>=4.22.0",

  # Git plumbing
  "gitpython>=3.1.43",

  # Misc HTTP helpers
  "requests>=2.32.3"
]

# ─────────────────────────────────────────────────────────────────────────────
#  Optional extras – `pip install .[dev]`
# ─────────────────────────────────────────────────────────────────────────────
[project.optional-dependencies]
dev = [
  # Formatting & style
  "black==24.4.2",
  "isort==5.13.2",
  "flake8==7.0.0",
  "flake8-bugbear==24.4.26",
  "flake8-bandit==4.1.1",

  # Testing & coverage
  "pytest==8.2.1",
  "coverage==7.5.3",
  "pytest-cov==5.0.0",

  # Git hooks & misc
  "pre-commit==3.7.0",
  "codespell==2.4.0"
]

# ─────────────────────────────────────────────────────────────────────────────
#  Handy links on PyPI project page
# ─────────────────────────────────────────────────────────────────────────────
[project.urls]
Homepage = "https://github.com/your-org/gpt-review"
Documentation = "https://github.com/your-org/gpt-review#readme"
Issues = "https://github.com/your-org/gpt-review/issues"

# ─────────────────────────────────────────────────────────────────────────────
#  Console script entry‑point
# ─────────────────────────────────────────────────────────────────────────────
[project.scripts]
gpt-review = "review:main"

# ─────────────────────────────────────────────────────────────────────────────
#  setuptools‑specific tweaks
# ─────────────────────────────────────────────────────────────────────────────
[tool.setuptools]
# Top‑level modules (sibling .py files) that live outside the package dir
py-modules = ["review", "apply_patch", "patch_validator", "logger"]

[tool.setuptools.package-data]
# Ship JSON schema inside the wheel
"gpt_review" = ["schema.json"]

# ─────────────────────────────────────────────────────────────────────────────
#  Black (auto‑formatter)
# ─────────────────────────────────────────────────────────────────────────────
[tool.black]
line-length = 88
target-version = ["py39", "py310", "py311", "py312"]

# ─────────────────────────────────────────────────────────────────────────────
#  isort (import sorter) – configured to play nice with Black
# ─────────────────────────────────────────────────────────────────────────────
[tool.isort]
profile            = "black"
line_length        = 88
multi_line_output  = 3
include_trailing_comma = true
combine_as_imports     = true
force_grid_wrap        = 0

# ─────────────────────────────────────────────────────────────────────────────
#  flake8 (linter)
# ─────────────────────────────────────────────────────────────────────────────
[tool.flake8]
max-line-length = 88
extend-ignore   = "E203,W503,E501"
exclude         = [
  ".git",
  "venv",
  ".cache",
  "build",
  "dist",
  "logs",
  "__pycache__",
]
select          = "C,E,F,W,B,B9"
per-file-ignores = "tests/*:S101"

# ─────────────────────────────────────────────────────────────────────────────
#  coverage.py
# ─────────────────────────────────────────────────────────────────────────────
[tool.coverage.run]
branch = true
source = ["gpt_review", "review", "apply_patch", "patch_validator"]
