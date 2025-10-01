#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
===============================================================================
GPT-Review ▸ GPT-Codex Client Adapter
===============================================================================

Provides a thin compatibility layer that exposes the subset of the OpenAI
Chat Completions interface relied upon by GPT-Review while delegating to the
installed **gpt-5-codex** client library. The adapter normalises a variety of
potential method layouts so the rest of the codebase can continue to call:

    client.chat.completions.create(...)

Environment variables
---------------------
GPT_CODEX_API_KEY        – preferred API key variable
GPT_CODEX_BASE_URL       – optional custom endpoint
GPT_CODEX_API_BASE       – alias for the base URL (mirrors OpenAI naming)
GPT_CODEX_ORG_ID         – optional organisation identifier

For backwards compatibility, the adapter also honours the legacy OpenAI env
names (`OPENAI_API_KEY`, `OPENAI_BASE_URL`, `OPENAI_API_BASE`,
`OPENAI_ORG_ID`, `OPENAI_ORGANIZATION`). This allows incremental upgrades
without breaking existing deployments.
"""
from __future__ import annotations

import os
from importlib import import_module
from types import SimpleNamespace
from typing import Any, Iterable, Sequence

from gpt_review import get_logger

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Environment helpers
# ---------------------------------------------------------------------------
_API_KEY_VARS: Sequence[str] = (
    "GPT_CODEX_API_KEY",
    "CODEX_API_KEY",
    "OPENAI_API_KEY",
)
_BASE_URL_VARS: Sequence[str] = (
    "GPT_CODEX_BASE_URL",
    "GPT_CODEX_API_BASE",
    "CODEX_BASE_URL",
    "OPENAI_BASE_URL",
    "OPENAI_API_BASE",
)
_ORG_ID_VARS: Sequence[str] = (
    "GPT_CODEX_ORG_ID",
    "GPT_CODEX_ORGANIZATION",
    "OPENAI_ORG_ID",
    "OPENAI_ORGANIZATION",
)


def _first_env(names: Iterable[str]) -> str | None:
    for name in names:
        val = os.getenv(name)
        if val:
            return val
    return None


def resolve_api_key() -> str | None:
    """Return the first configured API key, if any."""
    return _first_env(_API_KEY_VARS)


def resolve_base_url() -> str | None:
    """Return the configured base URL (optional)."""
    return _first_env(_BASE_URL_VARS)


def resolve_org_id() -> str | None:
    """Return the configured organisation identifier (optional)."""
    return _first_env(_ORG_ID_VARS)


# ---------------------------------------------------------------------------
# SDK bootstrap
# ---------------------------------------------------------------------------
_CLIENT_CANDIDATES: Sequence[tuple[str, str]] = (
    ("gpt_5_codex_client", "Client"),
    ("gpt_5_codex", "Client"),
    ("gpt5_codex", "Client"),
    ("gpt_codex", "Client"),
)


def _load_sdk_class() -> type[Any]:
    """Import and return the first available gpt-5-codex client class."""
    errors: list[str] = []
    for module_name, attr in _CLIENT_CANDIDATES:
        try:
            module = import_module(module_name)
            cls = getattr(module, attr)
            log.debug("Using gpt-5-codex client: %s.%s", module_name, attr)
            return cls  # type: ignore[return-value]
        except ModuleNotFoundError as exc:
            errors.append(f"{module_name}: {exc}")
        except AttributeError as exc:
            errors.append(f"{module_name}.{attr}: {exc}")
    raise RuntimeError(
        "Could not locate a gpt-5-codex client class. Install the Codex SDK "
        "(e.g. `pip install gpt-5-codex-client`) and ensure it exposes one of: %s. "
        "Details: %s" % (
            ", ".join(f"{m}.{a}" for m, a in _CLIENT_CANDIDATES),
            "; ".join(errors) or "<no import attempts>",
        )
    )


# ---------------------------------------------------------------------------
# Chat completions proxy
# ---------------------------------------------------------------------------
_CALL_PATTERNS: Sequence[tuple[str, ...]] = (
    ("chat", "completions", "create"),
    ("chat", "completions"),
    ("chat_completions",),
    ("create_chat_completion",),
    ("chat_completion",),
    ("completions", "create"),
    ("completions",),
)


def _resolve_callable(root: Any, chain: tuple[str, ...]) -> Any | None:
    target = root
    for name in chain:
        target = getattr(target, name, None)
        if target is None:
            return None
    if callable(target):
        return target
    create = getattr(target, "create", None)
    if callable(create):
        return create
    return None


class _ChatCompletionsProxy:
    """Provide `.create(...)` regardless of the underlying SDK layout."""

    def __init__(self, sdk: Any, default_timeout: int | None = None) -> None:
        self._sdk = sdk
        self._default_timeout = default_timeout

    def create(self, **kwargs: Any) -> Any:
        if self._default_timeout is not None and "timeout" not in kwargs:
            kwargs["timeout"] = self._default_timeout
        for chain in _CALL_PATTERNS:
            func = _resolve_callable(self._sdk, chain)
            if func is None:
                continue
            call_kwargs = dict(kwargs)
            try:
                return func(**call_kwargs)
            except TypeError as exc:
                # Retry without timeout if the SDK does not support it.
                if "timeout" in call_kwargs and "timeout" in str(exc).lower():
                    call_kwargs.pop("timeout", None)
                    try:
                        return func(**call_kwargs)
                    except TypeError as inner_exc:
                        exc = inner_exc
                log.debug(
                    "gpt-5-codex callable %s rejected kwargs %s: %s",
                    ".".join(chain),
                    sorted(call_kwargs.keys()),
                    exc,
                )
                continue
        raise RuntimeError(
            "The gpt-5-codex client does not expose a compatible chat completion "
            "interface. Expected one of the chains: %s" % ", ".join(
                "->".join(chain) for chain in _CALL_PATTERNS
            )
        )


class CodexClientAdapter:
    """Expose `.chat.completions.create` by wrapping the raw SDK client."""

    def __init__(self, sdk: Any, timeout: int | None = None) -> None:
        self._sdk = sdk
        self.chat = SimpleNamespace(completions=_ChatCompletionsProxy(sdk, timeout))

    def __getattr__(self, item: str) -> Any:  # pragma: no cover - passthrough
        return getattr(self._sdk, item)


# ---------------------------------------------------------------------------
# Public factory
# ---------------------------------------------------------------------------


def create_client(api_timeout: int) -> CodexClientAdapter:
    """Instantiate and wrap the gpt-5-codex SDK client."""
    api_key = resolve_api_key()
    if not api_key:
        raise RuntimeError(
            "GPT_CODEX_API_KEY is not set. Please export it before running GPT-Review."
        )

    cls = _load_sdk_class()
    init_kwargs: dict[str, Any] = {}

    base_url = resolve_base_url()
    if base_url:
        init_kwargs["base_url"] = base_url

    org_id = resolve_org_id()
    if org_id:
        # Different SDKs use different parameter names; try a few common ones.
        for key in ("organization", "org_id", "tenant"):
            init_kwargs.setdefault(key, org_id)

    # Always prefer explicit api_key but fall back to attribute assignment
    # if the class does not accept it in the constructor.
    try:
        sdk = cls(api_key=api_key, **init_kwargs)
    except TypeError:
        try:
            sdk = cls(**init_kwargs)
        except Exception as exc:
            raise RuntimeError("Failed to initialise gpt-5-codex client") from exc
        if hasattr(sdk, "api_key"):
            setattr(sdk, "api_key", api_key)
        elif hasattr(sdk, "set_api_key"):
            sdk.set_api_key(api_key)  # type: ignore[call-arg]
        else:
            raise RuntimeError(
                "gpt-5-codex client does not accept api_key argument or attribute"
            )
    except Exception as exc:  # pragma: no cover - defensive
        raise RuntimeError("Failed to initialise gpt-5-codex client") from exc

    log.info(
        "gpt-5-codex client initialised | base=%s",
        base_url or "<default>",
    )
    return CodexClientAdapter(sdk, api_timeout)


__all__ = [
    "create_client",
    "resolve_api_key",
    "resolve_base_url",
    "resolve_org_id",
    "CodexClientAdapter",
]
