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


@dataclass(frozen=True)
class InfrastructureCost:
    name: str
    monthly_min: float
    monthly_max: float
    annual_min: float = 0
    annual_max: float = 0
    note: str = ""


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


def apply_scope_adjustment(result: EstimateResult, adjustment_percent: int) -> EstimateResult:
    """Apply an AI/heuristic complexity adjustment within a safe 0–35% band."""
    percent = max(0, min(35, adjustment_percent))
    multiplier = 1 + percent / 100
    extra_weeks = math.ceil(result.weeks_max * percent / 100)
    return EstimateResult(
        price_min=_round_to_nearest_hundred(result.price_min * multiplier),
        price_max=_round_to_nearest_hundred(result.price_max * multiplier),
        weeks_min=result.weeks_min + (extra_weeks // 2),
        weeks_max=result.weeks_max + extra_weeks,
    )


def calculate_infrastructure(project_type: str, features: list[str], description: str = "") -> list[InfrastructureCost]:
    """Transparent first-year operating-cost assumptions, separate from build cost."""
    text = description.lower()
    items: list[InfrastructureCost] = []

    if project_type in ("LANDING_PAGE", "WEB_APPLICATION", "CROSS_PLATFORM_ECOSYSTEM"):
        items.append(InfrastructureCost("Domain registration", 0, 0, 15, 40, "Typical .com-style domain"))

    hosting = {
        "LANDING_PAGE": (10, 35),
        "WEB_APPLICATION": (60, 250),
        "MOBILE_APP": (75, 300),
        "CROSS_PLATFORM_ECOSYSTEM": (125, 500),
    }[project_type]
    items.append(InfrastructureCost("Hosting & compute", *hosting, note="Scales with traffic and background jobs"))

    if project_type != "LANDING_PAGE" or "database" in text or "portal" in text:
        items.append(InfrastructureCost("Database & storage", 25, 175, note="Managed database, files, and backups"))
        items.append(InfrastructureCost("Monitoring & backups", 10, 80, note="Error tracking, uptime checks, and retention"))
    if "AUTH" in features or any(word in text for word in ("email", "notification", "invite", "reset password")):
        items.append(InfrastructureCost("Transactional email", 10, 100, note="Volume-based email delivery"))
    if "AI_INTEGRATION" in features or any(word in text for word in (" ai ", "artificial intelligence", "chatbot", "llm")):
        items.append(InfrastructureCost("AI model usage", 50, 600, note="Varies with users, tokens, and model"))
    if project_type in ("MOBILE_APP", "CROSS_PLATFORM_ECOSYSTEM"):
        items.append(InfrastructureCost("Apple Developer Program", 0, 0, 99, 99, "Annual publisher membership"))
        items.append(InfrastructureCost("Google Play registration", 0, 0, 25, 25, "One-time registration shown in year one"))
    if "PAYMENTS" in features or any(word in text for word in ("payment", "checkout", "subscription")):
        items.append(InfrastructureCost("Payment processing", 0, 0, note="Transaction percentage and fixed fees vary by provider"))
    return items


def infrastructure_totals(items: list[InfrastructureCost]) -> tuple[float, float, float, float]:
    monthly_min = sum(item.monthly_min for item in items)
    monthly_max = sum(item.monthly_max for item in items)
    annual_min = sum(item.annual_min for item in items)
    annual_max = sum(item.annual_max for item in items)
    return monthly_min, monthly_max, annual_min + monthly_min * 12, annual_max + monthly_max * 12
