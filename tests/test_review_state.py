"""
===============================================================================
Unit‑tests ▸ review.py empty‑repo (unborn HEAD) behavior
===============================================================================
Ensures the driver tolerates repositories with no commits yet and persists
a sensible state file.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from review import HEAD_UNBORN, _current_commit  # type: ignore[attr-defined]


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, text=True)


def _init_empty_repo(tmp: Path) -> Path:
    repo = tmp / "empty"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "Test")
    return repo


def test_unborn_head_reports_sentinel(tmp_path: Path) -> None:
    repo = _init_empty_repo(tmp_path)
    assert _current_commit(repo) == HEAD_UNBORN


def test_state_file_uses_unborn_head(tmp_path: Path) -> None:
    # Re‑implement tiny helper to avoid importing private state writers
    repo = _init_empty_repo(tmp_path)
    state_path = repo / ".gpt-review-state.json"
    data = {
        "conversation_url": "https://chatgpt.com/",
        "last_commit": _current_commit(repo),
        "timestamp": 0,
    }
    state_path.write_text(json.dumps(data), encoding="utf-8")

    loaded = json.loads(state_path.read_text(encoding="utf-8"))
    assert loaded["last_commit"] == HEAD_UNBORN
