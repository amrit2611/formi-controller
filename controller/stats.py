"""Deterministic statistics. No LLM ever decides a winner."""
import math
from statistics import NormalDist


def rate(successes, trials):
    return successes / trials if trials else 0.0


def z_score(s_a, n_a, s_b, n_b):
    """Two-proportion z-test. Positive z means B's rate is higher than A's."""
    if n_a == 0 or n_b == 0:
        return 0.0
    pooled = (s_a + s_b) / (n_a + n_b)
    se = math.sqrt(pooled * (1 - pooled) * (1 / n_a + 1 / n_b))
    if se == 0:
        return 0.0
    return (s_b / n_b - s_a / n_a) / se


def z_critical(alpha, comparisons):
    """Bonferroni: split alpha across comparisons, two-sided."""
    return NormalDist().inv_cdf(1 - alpha / (2 * comparisons))


def min_sample(p, d):
    """Rule of thumb for ~80% power at alpha 0.05: n = 16 p(1-p) / d^2 per arm."""
    return math.ceil(16 * p * (1 - p) / (d * d))
