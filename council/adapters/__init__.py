"""Adapter registry: council.yaml `adapter:` names -> implementations."""

from __future__ import annotations

from .base import Adapter, AdapterError, Reply, run_process
from .claude_cli import ClaudeAdapter
from .codex_cli import CodexAdapter
from .mock import MockAdapter
from .opencode_cli import OpencodeAdapter

REGISTRY: dict[str, type[Adapter]] = {
    "codex_cli": CodexAdapter,
    "opencode_cli": OpencodeAdapter,
    "claude_cli": ClaudeAdapter,
    "mock": MockAdapter,
}

__all__ = [
    "Adapter",
    "AdapterError",
    "Reply",
    "run_process",
    "REGISTRY",
    "build_adapter",
]


def build_adapter(name: str, **kwargs) -> Adapter:
    try:
        cls = REGISTRY[name]
    except KeyError:
        raise AdapterError(
            f"unknown adapter '{name}'. Known: {', '.join(sorted(REGISTRY))}"
        ) from None
    return cls(**kwargs)
