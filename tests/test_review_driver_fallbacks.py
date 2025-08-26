#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
===============================================================================
Unit tests ▸ review.py driver fallbacks & apply-failure reporting
===============================================================================

Scope (high-impact only):
1) Driver provisioning order in _chrome_driver():
   • CHROMEDRIVER env path takes precedence (passes a Service).
   • If webdriver-manager fails, fall back to Selenium Manager
     (i.e., webdriver.Chrome(options=...) without Service).

2) Apply-failure reporting helper _send_apply_error():
   • Includes the attempted patch JSON and tool output.
   • Delegates to _send_error_chunks (spied).

These tests are **pure unit tests**:
- They DO NOT launch a real browser.
- selenium.webdriver.Chrome is monkeypatched with a FakeChrome stub.
- webdriver_manager install is forced to raise where needed.
"""
from __future__ import annotations

from pathlib import Path

import pytest

import review


# -----------------------------------------------------------------------------
# Test doubles
# -----------------------------------------------------------------------------
class FakeChrome:
    """
    Minimal stub of selenium.webdriver.Chrome that records constructor calls
    and exposes a tiny surface used by review._log_driver_versions().
    """
    calls: list[dict] = []

    def __init__(self, *args, **kwargs):
        # Record how Chrome was constructed (service vs options-only)
        FakeChrome.calls.append({"args": args, "kwargs": kwargs})
        # Capabilities mimicking a real driver enough for logging
        self.capabilities = {
            "browserVersion": "123.0",
            "chrome": {"chromedriverVersion": "123.0.1"},
        }
        self.title = "fake"

    def get(self, url: str) -> None:  # pragma: no cover - not relied on here
        self.title = f"loaded:{url}"

    def quit(self) -> None:  # pragma: no cover - no-op
        pass


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------
def _boom(*_args, **_kwargs):
    """Raise to ensure a code-path is NOT taken when the test expects so."""
    raise AssertionError("webdriver-manager should not be invoked in this path")


# =============================================================================
# Tests
# =============================================================================
def test_chromedriver_env_precedence(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """
    With CHROMEDRIVER set to an executable file, _chrome_driver() must create
    the driver using a **Service** (i.e., pass `service=...`), and it must NOT
    call webdriver-manager at all.
    """
    # Arrange: executable chromedriver path
    drv_path = tmp_path / "chromedriver"
    drv_path.write_text("#!/bin/sh\necho chromedriver\n", encoding="utf-8")
    drv_path.chmod(0o755)

    # Env for headless & isolated profile
    monkeypatch.setenv("CHROMEDRIVER", str(drv_path))
    monkeypatch.setenv("GPT_REVIEW_HEADLESS", "1")
    monkeypatch.setenv("GPT_REVIEW_PROFILE", str(tmp_path / ".profile"))

    # Ensure webdriver-manager is NOT called if CHROMEDRIVER is honored
    monkeypatch.setattr(review, "ChromeDriverManager", type("WDM", (), {"install": _boom})(), raising=False)

    # Replace real Chrome with our stub
    FakeChrome.calls = []
    # Ensure a webdriver namespace exists even if review imported differently
    if not hasattr(review, "webdriver"):
        monkeypatch.setattr(review, "webdriver", type("WD", (), {})(), raising=False)
    monkeypatch.setattr(review.webdriver, "Chrome", FakeChrome, raising=False)

    # Optional: keep binary detection simple/fast
    monkeypatch.setattr(review, "_detect_browser_binary", lambda: "/usr/bin/chromium", raising=False)

    # Act
    drv = review._chrome_driver()  # should return a FakeChrome

    # Assert: one call with a Service kwarg, and _boom didn't trigger
    assert isinstance(drv, FakeChrome)
    assert FakeChrome.calls, "FakeChrome was not constructed"
    kwargs = FakeChrome.calls[-1]["kwargs"]
    assert "service" in kwargs, "CHROMEDRIVER precedence should use a Service"
    # Do not assert exact attribute names on Service (selenium version differences)


def test_fallback_to_selenium_manager_when_wdm_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """
    When webdriver-manager raises (e.g., offline), _chrome_driver() must fall
    back to Selenium Manager: create Chrome with **options only** (no Service).
    """
    # Arrange: no CHROMEDRIVER path
    monkeypatch.delenv("CHROMEDRIVER", raising=False)
    monkeypatch.setenv("GPT_REVIEW_HEADLESS", "1")
    monkeypatch.setenv("GPT_REVIEW_PROFILE", str(tmp_path / ".profile"))

    # Force webdriver-manager to raise
    def _raise(*_a, **_k):
        raise RuntimeError("offline")

    monkeypatch.setattr(
        review, "ChromeDriverManager", type("WDM", (), {"install": _raise})(), raising=False
    )

    # Replace real Chrome with our stub
    FakeChrome.calls = []
    if not hasattr(review, "webdriver"):
        monkeypatch.setattr(review, "webdriver", type("WD", (), {})(), raising=False)
    monkeypatch.setattr(review.webdriver, "Chrome", FakeChrome, raising=False)

    # Optional: stable chrome type (not required)
    monkeypatch.setattr(review, "_detect_browser_binary", lambda: "/usr/bin/chromium", raising=False)

    # Act
    drv = review._chrome_driver()

    # Assert: constructed without a Service kwarg (options-only)
    assert isinstance(drv, FakeChrome)
    assert FakeChrome.calls, "FakeChrome was not constructed"
    kwargs = FakeChrome.calls[-1]["kwargs"]
    assert "service" not in kwargs, "Fallback should not pass Service when wdm fails"


def test_send_apply_error_includes_patch_json_and_output(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """
    _send_apply_error should format a report that includes:
    - The raw patch JSON we attempted to apply
    - The stdout/stderr from the failed applier
    It must delegate to _send_error_chunks (which we spy).
    """
    captured: dict = {}

    def spy_send_error_chunks(*, session_id, repo, cmd, exit_code, output, **_):
        captured.update(
            {
                "session_id": session_id,
                "repo": Path(repo),
                "cmd": cmd,
                "exit_code": exit_code,
                "output": output,
            }
        )

    # Spy the chunk sender to avoid network/browser interactions
    monkeypatch.setattr(review, "_send_error_chunks", spy_send_error_chunks, raising=False)

    patch = {"op": "create", "file": "foo.txt", "body": "hi", "status": "in_progress"}

    # Act
    review._send_apply_error(
        drv=object(),
        session_id="abc123",
        repo=tmp_path,
        patch=patch,
        stderr="Traceback: boom",
        stdout="some stdout",
        exit_code=3,
    )

    # Assert: basic envelope and key contents present
    out = captured.get("output", "")
    assert captured.get("cmd") == "apply_patch"
    assert captured.get("exit_code") == 3
    assert "Patch JSON (as attempted):" in out
    assert '"file": "foo.txt"' in out or '"file":"foo.txt"' in out
    assert "apply_patch.py output:" in out
    assert "Traceback" in out
