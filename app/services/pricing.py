"""
GGH-201 — Dynamic Budget & Timeline Estimation Logic Engine.

Deterministic rules engine: project type + feature toggles + design tier
-> (price_min, price_max, weeks_min, weeks_max). No AI/LLM call is needed
for this — the acceptance criteria describe fixed lookup math, not
open-ended reasoning, so this stays fast, free, and fully testable.

Numbers below are placeholders — swap in GG HighTech's real pricing once
the business side signs off; the shape (base + additive deltas) is the
part meant to be durable.
"""

import math
from dataclasses import dataclass


def _round_to_nearest_hundred(value: float) -> float:
    """Standard round-half-up to the nearest $100 (avoids the surprises of
    Python's banker's-rounding `round(x, -2)` for currency display)."""
    return math.floor(value / 100 + 0.5) * 100


@dataclass(frozen=True)
class EstimateResult:
    price_min: float
    price_max: float
    weeks_min: int
    weeks_max: int


# Base project types set the floor for price and timeline.
PROJECT_TYPES: dict[str, EstimateResult] = {
    "LANDING_PAGE": EstimateResult(3_000, 6_000, 2, 3),
    "WEB_APPLICATION": EstimateResult(10_000, 18_000, 6, 10),
    "MOBILE_APP": EstimateResult(15_000, 25_000, 8, 12),
    "CROSS_PLATFORM_ECOSYSTEM": EstimateResult(14_000, 22_000, 7, 10),
}

# Each feature toggle adds a delta on top of the base.
FEATURE_DELTAS: dict[str, EstimateResult] = {
    "AI_INTEGRATION": EstimateResult(2_500, 4_000, 1, 2),
    "AUTH": EstimateResult(1_000, 1_500, 0, 1),
    "PAYMENTS": EstimateResult(1_500, 2_500, 1, 1),
    "REALTIME_SYNC": EstimateResult(2_000, 3_000, 1, 1),
}

# Design tier is a multiplier on price only (timeline is additive, flat).
DESIGN_TIER_PRICE_MULTIPLIER: dict[str, float] = {
    "STANDARD": 1.0,
    "PREMIUM": 1.15,
    "MOTION_3D": 1.3,
}
DESIGN_TIER_WEEKS_DELTA: dict[str, int] = {
    "STANDARD": 0,
    "PREMIUM": 1,
    "MOTION_3D": 2,
}


class InvalidScopeError(ValueError):
    """Raised when the estimator receives an unrecognized option key."""


def calculate_estimate(
    project_type: str,
    features: list[str],
    design_tier: str = "STANDARD",
) -> EstimateResult:
    if project_type not in PROJECT_TYPES:
        raise InvalidScopeError(f"Unknown project_type: {project_type!r}")
    if design_tier not in DESIGN_TIER_PRICE_MULTIPLIER:
        raise InvalidScopeError(f"Unknown design_tier: {design_tier!r}")

    base = PROJECT_TYPES[project_type]
    price_min, price_max = base.price_min, base.price_max
    weeks_min, weeks_max = base.weeks_min, base.weeks_max

    for feature in features:
        if feature not in FEATURE_DELTAS:
            raise InvalidScopeError(f"Unknown feature: {feature!r}")
        delta = FEATURE_DELTAS[feature]
        price_min += delta.price_min
        price_max += delta.price_max
        weeks_min += delta.weeks_min
        weeks_max += delta.weeks_max

    price_multiplier = DESIGN_TIER_PRICE_MULTIPLIER[design_tier]
    weeks_delta = DESIGN_TIER_WEEKS_DELTA[design_tier]

    return EstimateResult(
        price_min=_round_to_nearest_hundred(price_min * price_multiplier),
        price_max=_round_to_nearest_hundred(price_max * price_multiplier),
        weeks_min=weeks_min + weeks_delta,
        weeks_max=weeks_max + weeks_delta,
    )
