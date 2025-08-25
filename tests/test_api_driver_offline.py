#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Offline unit test for the API driver (`gpt_review.api_driver`).

Goals
-----
• Exercise the end-to-end control flow in API mode without network access.
• Inject a fake OpenAI client that always returns a single `submit_patch` tool call
  with `status="completed"` so the loop exits cleanly after one turn.
• Stub subprocess.run to:
    - emulate `apply_patch.py` success (returncode=0),
    - emulate `git rev-parse` (no commits yet, harmless),
    - pass-through everything else to the original runner (rare).

This test does *not* assert filesystem changes because the patch application is
stubbed at the process layer; it focuses on API-driver orchestration and error
handling boundaries.

Run with:
    pytest -q tests/test_api_driver_offline.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, List

import pytest


# ───────────────────────────── helper fakes ──────────────────────────────────
class _Obj:
    """Simple attribute container to mimic SDK objects (choices/message/tool_calls)."""

    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)


class _FakeCompletions:
    def __init__(self, responses: List[Dict[str, Any]]):
        """
        responses: a list of dicts describing the tool-call payloads to return.
        Each item should be a Python dict that will be JSON-encoded into the tool
        call's `.function.arguments`.
        """
        self._responses = list(responses)
        self.calls: List[Dict[str, Any]] = []

    def create(self, **kwargs):
        """
        Emulate `client.chat.completions.create(...)`.
        Returns an object with `.choices[0].message.tool_calls[...]`.
        """
        self.calls.append(kwargs)
        if not self._responses:
            # If more calls are made than responses provided, return a no-op
            # assistant turn without tool calls to surface a failure.
            msg = _Obj(role="assistant", content="(no tool_calls)", tool_calls=[])
            return _Obj(choices=[_Obj(message=msg)])

        payload = self._responses.pop(0)
        tc = _Obj(
            id="tool_1",
            function=_Obj(
                name="submit_patch",
                arguments=json.dumps(payload, ensure_ascii=False),
            ),
        )
        msg = _Obj(role="assistant", content="", tool_calls=[tc])
        return _Obj(choices=[_Obj(message=msg)])


class _FakeChat:
    def __init__(self, responses: List[Dict[str, Any]]):
        self.completions = _FakeCompletions(responses)


class FakeOpenAIClient:
    """
    Minimal stand-in for the modern OpenAI client:
        client.chat.completions.create(...)
    """

    def __init__(self, responses: List[Dict[str, Any]]):
        self.chat = _FakeChat(responses)


# ───────────────────────────── subprocess stub ───────────────────────────────
class _Proc:
    def __init__(self, rc=0, out="", err=""):
        self.returncode = rc
        self.stdout = out
        self.stderr = err


@pytest.fixture
def stub_subprocess_run(monkeypatch):
    """
    Stub subprocess.run to intercept:
      • apply_patch.py invocation → success (returncode=0)
      • git rev-parse → no commits yet (returncode=1, empty out)
    Pass-through any other calls to the original.
    """
    import subprocess as _sp

    orig_run = _sp.run

    def fake_run(args, **kwargs):
        # Normalize program vector for pattern checks
        vector = args if isinstance(args, (list, tuple)) else [args]

        # 1) apply_patch.py (Python -m invocation with script path at argv[1])
        if (
            isinstance(vector, (list, tuple))
            and len(vector) >= 2
            and isinstance(vector[0], str)
            and isinstance(vector[1], str)
            and vector[1].endswith("apply_patch.py")
        ):
            # Simulate a successful apply; stdout can contain any text.
            return _Proc(rc=0, out="applied\n", err="")

        # 2) git rev-parse (commit detection)
        if isinstance(vector, (list, tuple)) and vector and str(vector[0]).endswith("git"):
            # No commits yet is fine for the driver
            return _Proc(rc=1, out="", err="fatal: Needed a single revision\n")

        # 3) Anything else → pass through
        return orig_run(args, **kwargs)

    monkeypatch.setattr("subprocess.run", fake_run)
    return fake_run


# ───────────────────────────── tests ─────────────────────────────────────────
def test_api_driver_completes_with_single_completed_patch(tmp_path, stub_subprocess_run):
    """
    Happy path:
      • Model returns one tool call with a valid `submit_patch` payload.
      • `apply_patch.py` succeeds (stubbed).
      • No --cmd given, so loop exits immediately because status=completed.
    """
    # Arrange
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()  # minimal marker for a git repo

    instructions = tmp_path / "instr.txt"
    instructions.write_text("Modernise for Python 3.12 and bump coverage.", encoding="utf-8")

    # The patch payload must satisfy our JSON schema
    patch_payload = {
        "op": "create",
        "file": "README.md",
        "body": "# Title\n\nHello\n",
        "status": "completed",
    }

    fake_client = FakeOpenAIClient(responses=[patch_payload])

    # Act
    from gpt_review.api_driver import run as api_run

    api_run(
        instructions_path=instructions,
        repo=repo,
        cmd=None,           # no command to run
        auto=True,          # doesn't matter; we exit in one turn
        timeout=120,        # unused here
        model="test-model", # carried through to the fake client log only
        api_timeout=30,
        client=fake_client, # injected: no network calls
    )

    # Assert: one API call made, with our forced tool_choice & tools present.
    calls = fake_client.chat.completions.calls
    assert len(calls) == 1, "expected exactly one API round-trip"
    # Basic shape checks (not brittle to exact SDK internals)
    sent = calls[0]
    assert "messages" in sent and isinstance(sent["messages"], list)
    assert "tools" in sent and sent["tools"], "tools schema must be sent"
    assert "tool_choice" in sent and sent["tool_choice"], "tool_choice must be forced"


def test_api_driver_handles_validation_error_then_nudge(tmp_path, stub_subprocess_run):
    """
    Failure flow:
      • First tool call returns an invalid payload (missing required fields),
        which should be rejected and answered with a tool result.
      • Second tool call returns a valid patch and completes.
    We mainly verify that the driver can progress across a validation error.
    """
    repo = tmp_path / "repo2"
    repo.mkdir()
    (repo / ".git").mkdir()

    instructions = tmp_path / "instr2.txt"
    instructions.write_text("Do minimal changes, one file per patch.", encoding="utf-8")

    bad_payload = {"op": "create"}  # missing required "status" and file/body details
    good_payload = {
        "op": "create",
        "file": "CHANGELOG.md",
        "body": "## Unreleased\n- Added tests.\n",
        "status": "completed",
    }

    fake_client = FakeOpenAIClient(responses=[bad_payload, good_payload])

    from gpt_review.api_driver import run as api_run

    api_run(
        instructions_path=instructions,
        repo=repo,
        cmd=None,
        auto=True,
        timeout=60,
        model="test-model",
        api_timeout=20,
        client=fake_client,
    )

    calls = fake_client.chat.completions.calls
    assert len(calls) == 2, "expected two API calls (first invalid, second valid)"
