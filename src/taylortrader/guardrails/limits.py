from __future__ import annotations

import math
from dataclasses import asdict, dataclass, fields
from typing import Any


class LimitsError(ValueError):
    """Raised when configuration tries to loosen a hard limit."""


@dataclass(frozen=True)
class Limits:
    max_position_pct: float = 25.0        # one position, % of equity
    max_total_exposure_pct: float = 90.0  # all positions, % of equity
    max_daily_loss_pct: float = 3.0       # stop opening positions after this drawdown today
    max_trades_per_day: int = 10
    max_stop_loss_pct: float = 15.0       # every entry needs a stop no wider than this

    @classmethod
    def from_config(cls, cfg: dict[str, Any] | None) -> Limits:
        """Build limits from config. Config may tighten the hard limits but never loosen them."""
        cfg = dict(cfg or {})
        known = {f.name for f in fields(cls)}
        unknown = set(cfg) - known
        if unknown:
            raise LimitsError(f"Unknown guardrail setting(s): {sorted(unknown)}")
        limits = cls(**cfg)
        limits.validate()
        return limits

    def validate(self) -> None:
        problems = []
        for name, value in asdict(self).items():
            hard = getattr(HARD_LIMITS, name)
            # NaN compares false with everything, so it would slip past "> hard" and loosen a limit.
            if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
                problems.append(f"{name}={value!r} must be a number")
            elif value <= 0:
                problems.append(f"{name}={value} must be positive")
            elif value > hard:
                problems.append(f"{name}={value} exceeds the hard limit of {hard}")
        if problems:
            raise LimitsError("; ".join(problems))


# The ceiling nothing can exceed. Changing these is a code change, reviewed on purpose.
HARD_LIMITS = Limits(
    max_position_pct=100.0,
    max_total_exposure_pct=100.0,
    max_daily_loss_pct=10.0,
    max_trades_per_day=50,
    max_stop_loss_pct=25.0,
)
