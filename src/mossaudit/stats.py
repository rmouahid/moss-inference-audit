"""Small, dependency-light statistics used in the reports."""

from __future__ import annotations

import math
import random
from collections.abc import Sequence


def percentile(xs: Sequence[float], q: float) -> float:
    """Linear-interpolated percentile (same as numpy's default), q in [0, 100]."""
    if not xs:
        return math.nan
    s = sorted(xs)
    k = (len(s) - 1) * q / 100
    lo, hi = math.floor(k), math.ceil(k)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def bootstrap_ci(
    xs: Sequence[float], q: float, n_boot: int = 2000, alpha: float = 0.05, seed: int = 0
) -> tuple[float, float]:
    """Percentile-bootstrap confidence interval of the q-th percentile."""
    if len(xs) < 2:
        return (math.nan, math.nan)
    rng = random.Random(seed)
    n = len(xs)
    est = sorted(percentile([xs[rng.randrange(n)] for _ in range(n)], q) for _ in range(n_boot))
    return (est[int(n_boot * alpha / 2)], est[int(n_boot * (1 - alpha / 2)) - 1])


def wilson(successes: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson score interval for a proportion."""
    if n == 0:
        return (math.nan, math.nan)
    p = successes / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))


def summary(xs: Sequence[float]) -> dict[str, float]:
    xs = [x for x in xs if x is not None and not math.isnan(x)]
    if not xs:
        return {"n": 0}
    mean = sum(xs) / len(xs)
    sd = math.sqrt(sum((x - mean) ** 2 for x in xs) / (len(xs) - 1)) if len(xs) > 1 else 0.0
    return {
        "n": len(xs),
        "mean": round(mean, 2),
        "sd": round(sd, 2),
        "min": round(min(xs), 2),
        "p50": round(percentile(xs, 50), 2),
        "p90": round(percentile(xs, 90), 2),
        "p95": round(percentile(xs, 95), 2),
        "max": round(max(xs), 2),
    }
