"""Guardrails: broker-agnostic risk checks that sit between a trading decision and the broker.

This package deliberately imports nothing else from taylortrader or any broker SDK, so it can be
used on its own (for example in front of an AI agent that places orders).
"""

from .engine import Decision, Guardrails, find_unmanaged_positions
from .limits import HARD_LIMITS, Limits, LimitsError
from .models import AccountSnapshot, OrderIntent, Position, Side

__all__ = [
    "HARD_LIMITS",
    "AccountSnapshot",
    "Decision",
    "Guardrails",
    "Limits",
    "LimitsError",
    "OrderIntent",
    "Position",
    "Side",
    "find_unmanaged_positions",
]
