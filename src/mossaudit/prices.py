"""Documented prices, used to estimate costs before a campaign and to audit billing after it.

Source: https://docs.moss.dev/docs/inference/models.md ("Prices in USD per 1M tokens, as of
2026-11-05") and https://docs.moss.dev/docs/inference/pricing.md, both fetched 2026-11-25.
"""

from __future__ import annotations

from dataclasses import dataclass

RETRIEVAL_USD = 0.50 / 1000  # per retrieval
TASK_MODEL_MARKUP = 1.20  # browser tasks bill model tokens at cost + 20 %
TASK_BROWSER_USD_PER_HOUR = 0.02  # as documented; verified against usage.cost in TSK-14
TASK_TRAFFIC_USD_PER_GB = 5.0


@dataclass(frozen=True)
class Price:
    """Documented per-model prices, in USD per 1M tokens."""

    input: float  # USD per 1M tokens
    cached_input: float
    output: float


PRICES: dict[str, Price] = {
    "moss/fastlane-1": Price(0.25, 0.03, 1.75),
    "openai/gpt-6-luna": Price(0.10, 0.01, 0.50),
    "anthropic/claude-haiku-5.5": Price(0.10, 0.01, 0.50),
    "google/gemini-3.5-flash-lite": Price(0.30, 0.03, 2.50),
    "google/gemini-3.8-flash": Price(0.75, 0.075, 3.75),
    "deepseek/deepseek-v4.1-flash": Price(0.054, 0.0072, 0.60),
    "minimax/minimax-m3.1-flash": Price(0.09, 0.009, 0.36),
    "qwen/qwen3.8-flash": Price(0.18, 0.019, 0.56),
    "z-ai/glm-5.3-flash": Price(0.18, 0.036, 0.60),
}


def token_cost(model: str, input_tokens: int, output_tokens: int, cached_tokens: int = 0) -> float:
    """Cost in USD of one call from its usage counts.

    Vendor cache-write surcharges are not modelled (no blanket figure is documented); campaigns that
    may write vendor caches are reconciled against the portal Usage page instead."""
    p = PRICES.get(model)
    if p is None:
        raise KeyError(f"no documented price for {model!r}; add it to PRICES before spending")
    uncached = max(input_tokens - cached_tokens, 0)
    return (uncached * p.input + cached_tokens * p.cached_input + output_tokens * p.output) / 1e6


def usage_tokens(u: object) -> tuple[int, int, int] | None:
    """(input, output, cached) from a Responses or Chat usage object, or None when the counts are
    missing or implausible (bool, negative, cached > input). Such usage must not settle a charge."""
    if not isinstance(u, dict):
        return None

    def count(v: object) -> int | None:
        return v if isinstance(v, int) and not isinstance(v, bool) and v >= 0 else None

    inp = count(u.get("input_tokens", u.get("prompt_tokens")))
    out = count(u.get("output_tokens", u.get("completion_tokens")))
    if inp is None or out is None:
        return None
    details = u.get("input_tokens_details") or u.get("prompt_tokens_details") or {}
    cached = count(details.get("cached_tokens", 0)) if isinstance(details, dict) else 0
    if cached is None or cached > inp:
        return None
    return inp, out, cached


# Statuses that mean "refused before anything ran" when they carry an error body. 408 (timeout) and
# 424 (failed after a tool call) are NOT in the list: work may have been done and billed.
PRE_EXECUTION_REFUSALS = {400, 401, 403, 404, 413, 422}


def definitive_refusal(status: int | None, error: str | None, n_text_deltas: int,
                       body: object = None) -> bool:
    """True only for a known pre-execution refusal: listed 4xx, error body, no transport error,
    no text generated."""
    has_error_body = isinstance(body, dict) and isinstance(body.get("error"), dict)
    return (error is None and status in PRE_EXECUTION_REFUSALS and n_text_deltas == 0
            and has_error_body)
