"""The efficiency coefficient and the points curve, frozen Oct 28 18:00 UTC.

K = (T_ref/T)^0.50 x min(M_ref/M, 50)^0.20 x min(S_ref/S, 1)^0.15 x (D_ref/D)^0.15

Wall time is uncapped: a faster program always scores more. Memory is capped
because a naive reference can be absurdly wasteful there. Source size and
dependency weight can only cost, never pay. The site's src/lib/coefficient.ts
implements the same constants; tests/test_scoring.py pins both to one set of
vectors.
"""
import math

EXPONENTS = {"T": 0.50, "M": 0.20, "S": 0.15, "D": 0.15}
CAPS = {"T": None, "M": 50.0, "S": 1.0, "D": 1.0}
MACHINE_WEIGHT = 0.65
JUDGE_WEIGHT = 0.35
OVERALL_TRACKS = 3


def capped_ratio(axis, ratio):
    cap = CAPS[axis]
    return ratio if cap is None else min(ratio, cap)


def k_from_ratios(ratios):
    """ratios maps T, M, S, D to reference / yours. Every axis is required."""
    k = 1.0
    for axis, exponent in EXPONENTS.items():
        ratio = ratios[axis]
        if not ratio > 0:
            raise ValueError(f"ratio for {axis} must be positive, got {ratio!r}")
        k *= capped_ratio(axis, ratio) ** exponent
    return k


def k_from_measurements(ref, team):
    """ref and team map T (s), M (bytes), S (code lines), D (1 + direct deps).

    S can be 0 when a track holds only published copies (which cost nothing); its factor
    is capped at 1.0, so zero lines simply sit at the cap.
    """
    ratios = {}
    for axis in EXPONENTS:
        if team[axis] > 0:
            ratios[axis] = ref[axis] / team[axis]
        elif axis == "S":
            ratios[axis] = CAPS["S"]
        else:
            raise ValueError(f"team {axis} must be positive, got {team[axis]!r}")
    return k_from_ratios(ratios)


def speed_points(k):
    """50 x log10(K), clamped to [0, 100]: K = 1 -> 0, K = 10 -> 50, K = 100 -> 100."""
    if not k > 0:
        return 0.0
    return min(100.0, max(0.0, 50.0 * math.log10(k)))


def track_total(points, judge_score):
    return MACHINE_WEIGHT * points + JUDGE_WEIGHT * judge_score


def overall(track_totals):
    """Geometric mean of the best three track totals; None below three tracks."""
    best = sorted(track_totals, reverse=True)[:OVERALL_TRACKS]
    if len(best) < OVERALL_TRACKS:
        return None
    if min(best) <= 0:
        return 0.0
    return math.exp(sum(math.log(t) for t in best) / OVERALL_TRACKS)


def dependency_weight(direct_dependencies):
    return 1 + direct_dependencies
