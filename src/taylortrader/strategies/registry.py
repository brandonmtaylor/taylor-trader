from __future__ import annotations

from dataclasses import fields
from typing import Any

from taylortrader.strategies.base import Strategy

_REGISTRY: dict[str, type[Strategy]] = {}


def register(cls: type[Strategy]) -> type[Strategy]:
    if cls.name in _REGISTRY:
        raise ValueError(f"strategy {cls.name!r} registered twice")
    _REGISTRY[cls.name] = cls
    return cls


def get_strategy(name: str, params: dict[str, Any] | None = None) -> Strategy:
    if name not in _REGISTRY:
        raise ValueError(f"unknown strategy {name!r}; available: {', '.join(sorted(_REGISTRY))}")
    return _REGISTRY[name](params)


def list_strategies(include_research: bool = False) -> list[dict[str, Any]]:
    """Strategies people can trade with; research-only ones (like ml) only when asked for."""
    return [
        {
            "name": cls.name,
            "description": cls.description,
            "backtestable": cls.backtestable,
            "research_only": cls.research_only,
            "params": {f.name: f.default for f in fields(cls.Params)},
        }
        for cls in sorted(_REGISTRY.values(), key=lambda c: c.name)
        if include_research or not cls.research_only
    ]
