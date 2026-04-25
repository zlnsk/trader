"""Bootstrap resampling utilities.

Answers: "is the observed improvement driven by one or two outlier
trades, or robust to resampling?"

Two-sample bootstrap on the accepted-trade outcomes for each config.
Each resample draws WITH REPLACEMENT from the respective accepted
lists; compute the difference of resampled means. The 95% CI of those
differences tells us whether the observed delta-of-means is
distinguishable from zero.
"""
from __future__ import annotations

import random
from typing import Sequence


def two_sample_delta_ci(
    baseline: Sequence[float],
    candidate: Sequence[float],
    *,
    n_samples: int = 2000,
    ci: float = 0.95,
    rng_seed: int | None = None,
) -> tuple[float, float, float]:
    """Return (observed_delta, lower_ci, upper_ci) where delta is
    mean(candidate) - mean(baseline). Empty inputs treated as zero-mean
    samples of size 1."""
    if not baseline or not candidate:
        return (0.0, 0.0, 0.0)
    rng = random.Random(rng_seed)
    nb = len(baseline)
    nc = len(candidate)
    deltas = []
    for _ in range(n_samples):
        bs_mean = sum(baseline[rng.randrange(nb)] for _ in range(nb)) / nb
        cs_mean = sum(candidate[rng.randrange(nc)] for _ in range(nc)) / nc
        deltas.append(cs_mean - bs_mean)
    deltas.sort()
    lo = deltas[int((1 - ci) / 2 * n_samples)]
    hi = deltas[int((1 + ci) / 2 * n_samples) - 1]
    observed = (sum(candidate) / nc) - (sum(baseline) / nb)
    return (observed, lo, hi)


# Legacy paired API retained for callers that want per-trade deltas.
def paired_delta_ci(
    deltas: Sequence[float],
    *,
    n_samples: int = 2000,
    ci: float = 0.95,
    rng_seed: int | None = None,
) -> tuple[float, float, float]:
    if not deltas:
        return (0.0, 0.0, 0.0)
    rng = random.Random(rng_seed)
    n = len(deltas)
    means = []
    for _ in range(n_samples):
        sample = [deltas[rng.randrange(n)] for _ in range(n)]
        means.append(sum(sample) / n)
    means.sort()
    lo = means[int((1 - ci) / 2 * n_samples)]
    hi = means[int((1 + ci) / 2 * n_samples) - 1]
    mean = sum(deltas) / n
    return (mean, lo, hi)
