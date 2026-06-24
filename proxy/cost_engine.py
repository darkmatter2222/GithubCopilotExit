"""
Cost analysis engine for LLM proxy.

Provides:
- Cloud model pricing reference data (per 1M tokens) across high/medium/low tiers
- Cost calculations for any token count against any referenced cloud model
- Local GPU cost estimation (electricity + amortized hardware)
- Savings comparison between local and cloud inference
"""

import os
import time
from datetime import datetime, timezone, timedelta

# ── Cloud model pricing reference ────────────────────────────────────────
# Prices per 1M tokens, standard processing tier.
# Structure: { model_id: { display_name, input_price, output_price, tier } }
# Tiers: "high" = flagship, "medium" = capable mid-range, "low" = economical

MODEL_PRICING = {
    # ── High-end (flagship) models ──
    "gpt-5.5": {
        "display_name": "GPT-5.5",
        "input_price": 5.00,
        "output_price": 30.00,
        "tier": "high",
    },
    "claude-opus-4": {
        "display_name": "Claude Opus 4",
        "input_price": 15.00,
        "output_price": 75.00,
        "tier": "high",
    },
    "gpt-5.4-pro": {
        "display_name": "GPT-5.4 Pro / o3",
        "input_price": 30.00,
        "output_price": 180.00,
        "tier": "high",
    },
    # ── Medium-end models ──
    "gpt-5.4-mini": {
        "display_name": "GPT-5.4 Mini / o4-mini",
        "input_price": 0.75,
        "output_price": 4.50,
        "tier": "medium",
    },
    "claude-sonnet-4": {
        "display_name": "Claude Sonnet 4",
        "input_price": 3.00,
        "output_price": 15.00,
        "tier": "medium",
    },
    "gemini-2.5-pro": {
        "display_name": "Gemini 2.5 Pro",
        "input_price": 1.25,
        "output_price": 12.50,
        "tier": "medium",
    },
    # ── Low-end (economical) models ──
    "gpt-5.4-nano": {
        "display_name": "GPT-5.4 Nano / o3-mini",
        "input_price": 0.20,
        "output_price": 1.25,
        "tier": "low",
    },
    "claude-haiku": {
        "display_name": "Claude Haiku",
        "input_price": 0.25,
        "output_price": 1.25,
        "tier": "low",
    },
    "gemini-2.5-flash": {
        "display_name": "Gemini 2.5 Flash",
        "input_price": 0.10,
        "output_price": 0.40,
        "tier": "low",
    },
}

# Tier groupings for quick aggregation
TIER_MODELS = {
    "high": ["gpt-5.5", "claude-opus-4", "gpt-5.4-pro"],
    "medium": ["gpt-5.4-mini", "claude-sonnet-4", "gemini-2.5-pro"],
    "low": ["gpt-5.4-nano", "claude-haiku", "gemini-2.5-flash"],
}

# ── Local GPU cost model ────────────────────────────────────────────────

# RTX 5090: ~575W TDP under load, we estimate actual draw during inference
GPU_POWER_WATTS = float(os.environ.get("GPU_POWER_WATTS", "400"))
# Atlanta GA average electricity rate (MERIT system / Georgia Power)
# ~$0.128/kWh as of 2025 for level-plan residential
ELECTRICITY_RATE_USD_PER_KWH = float(os.environ.get("ELECTRICITY_RATE", "0.128"))

# Amortized GPU cost (optional, defaults to $1800 / 36 months)
GPU_HARDWARE_COST_USD = float(os.environ.get("GPU_HARDWARE_COST", "1800"))
GPU_LIFESPAN_MONTHS = int(os.environ.get("GPU_LIFESPAN_MONTHS", "36"))


def dollar_fmt(n: float) -> str:
    """Format a dollar amount with smart precision."""
    if n == 0:
        return "$0.00"
    abs_n = abs(n)
    if abs_n >= 100:
        return f"${n:,.2f}"
    if abs_n >= 1:
        return f"${n:.2f}"
    if abs_n >= 0.01:
        return f"${n:.3f}"
    return f"${n:.4f}"


def get_gpu_hourly_cost() -> float:
    """USD per hour of GPU compute (electricity + amortized hardware)."""
    elec_per_hour = (GPU_POWER_WATTS / 1000) * ELECTRICITY_RATE_USD_PER_KWH
    # Hardware: cost_spread / month / hours_in_month
    hw_per_hour = GPU_HARDWARE_COST_USD / GPU_LIFESPAN_MONTHS / 730
    return elec_per_hour + hw_per_hour


def get_gpu_cost_for_duration(duration_secs: float) -> float:
    """USD cost of running the GPU for a given duration."""
    hours = duration_secs / 3600
    return hours * get_gpu_hourly_cost()


def calculate_cloud_cost(prompt_tokens: int, completion_tokens: int, model_id: str) -> float:
    """Calculate cloud API cost for a single request against a specific model."""
    pricing = MODEL_PRICING.get(model_id)
    if not pricing:
        return 0.0
    cost = (prompt_tokens / 1_000_000) * pricing["input_price"]
    cost += (completion_tokens / 1_000_000) * pricing["output_price"]
    return cost


def calculate_cloud_cost_by_tier(prompt_tokens: int, completion_tokens: int, tier: str) -> float:
    """Average cloud cost across all models in a tier."""
    model_ids = TIER_MODELS.get(tier, [])
    if not model_ids:
        return 0.0
    total = sum(
        calculate_cloud_cost(prompt_tokens, completion_tokens, mid)
        for mid in model_ids
    )
    return total / len(model_ids)


def get_all_model_costs(prompt_tokens: int, completion_tokens: int) -> dict:
    """Return cost for every tracked cloud model."""
    costs = {}
    for model_id in MODEL_PRICING:
        costs[model_id] = calculate_cloud_cost(prompt_tokens, completion_tokens, model_id)
    return costs


def get_tier_summary(prompt_tokens: int, completion_tokens: int) -> dict:
    """Return average cost per tier."""
    return {
        tier: calculate_cloud_cost_by_tier(prompt_tokens, completion_tokens, tier)
        for tier in ("high", "medium", "low")
    }


def format_pricing_table() -> list:
    """Return rows for a pricing reference table."""
    rows = []
    for model_id, info in MODEL_PRICING.items():
        rows.append({
            "model_id": model_id,
            "display_name": info["display_name"],
            "tier": info["tier"],
            "input_per_1m": info["input_price"],
            "output_per_1m": info["output_price"],
        })
    return rows
