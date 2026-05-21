"""Learned calibration for the weather scanner's synthetic probability.

Problem: raw `synth_prob` is a horizon-weighted blend of historical base rate
+ ensemble forecast. Empirically, blends can be systematically too confident
(overshoot tails) or miscalibrated per location. Without correction we'll
keep placing bets where our "70%" really plays out 55%.

Solution: a histogram calibrator fit on resolved recs. For each 0.1 bin of
raw predictions, we learn the empirical hit rate, then map future raw preds
to the learned bin mean (isotonic-like but bucket-based so we don't need
sklearn).

- Fit requires MIN_SAMPLES (default 30) total resolved recs, else no-op.
- Calibrator fit coefficients live in the `config` table keyed by name, so
  the scanner can load them without re-fitting per request.
- Refits are triggered by `refit_calibrator()` — called after each resolve
  pass.

`calibrate(raw_prob)` is safe to call always; it returns `raw_prob` unchanged
when no calibrator has been fit yet.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone

from db import connect, get_config, set_config
from logging_setup import get_logger

log = get_logger(__name__)

_CONFIG_KEY = "calibrator.weather.v1"
_MIN_SAMPLES = 30  # below this, no calibration (too little data)
_N_BUCKETS = 10  # 0-10%, 10-20%, ..., 90-100%


@dataclass
class Calibrator:
    """Histogram-based learned map: raw_prob → calibrated_prob.

    `buckets[i]` is the empirical hit-rate for raw probs in [i/10, (i+1)/10).
    Buckets with too few samples fall back to the linear-interpolation
    neighbors so we don't get abrupt jumps.
    """

    buckets: list[float] = field(default_factory=lambda: [0.05 + 0.1 * i for i in range(_N_BUCKETS)])
    bucket_counts: list[int] = field(default_factory=lambda: [0] * _N_BUCKETS)
    total_samples: int = 0
    brier_before: float = 0.0
    brier_after: float = 0.0
    fit_at: str | None = None
    fit_note: str = "identity"  # 'identity' | 'fitted' | 'insufficient_data'

    def calibrate(self, raw_prob: float) -> float:
        raw_prob = max(0.0, min(1.0, raw_prob))
        if self.fit_note != "fitted":
            return raw_prob
        bucket = min(int(raw_prob * _N_BUCKETS), _N_BUCKETS - 1)
        # Linear interpolation within the bucket between neighbors — prevents
        # step-function artifacts at bucket boundaries.
        left_anchor = raw_prob * _N_BUCKETS - bucket  # 0..1 inside this bucket
        this_val = self.buckets[bucket]
        # Neighbor blend
        if left_anchor < 0.5 and bucket > 0:
            neighbor_val = self.buckets[bucket - 1]
            t = 0.5 + left_anchor  # 0.5..1.0
        elif left_anchor >= 0.5 and bucket < _N_BUCKETS - 1:
            neighbor_val = self.buckets[bucket + 1]
            t = 1.5 - left_anchor  # 1.0..0.5
        else:
            return this_val
        return t * this_val + (1 - t) * neighbor_val

    def to_json(self) -> str:
        return json.dumps(
            {
                "buckets": self.buckets,
                "bucket_counts": self.bucket_counts,
                "total_samples": self.total_samples,
                "brier_before": self.brier_before,
                "brier_after": self.brier_after,
                "fit_at": self.fit_at,
                "fit_note": self.fit_note,
            }
        )

    @classmethod
    def from_json(cls, raw: str) -> "Calibrator":
        d = json.loads(raw)
        return cls(
            buckets=d.get("buckets", [0.05 + 0.1 * i for i in range(_N_BUCKETS)]),
            bucket_counts=d.get("bucket_counts", [0] * _N_BUCKETS),
            total_samples=d.get("total_samples", 0),
            brier_before=d.get("brier_before", 0.0),
            brier_after=d.get("brier_after", 0.0),
            fit_at=d.get("fit_at"),
            fit_note=d.get("fit_note", "identity"),
        )


# Module-level cache so the scanner doesn't hit the DB per rec.
_cached: Calibrator | None = None


async def load_calibrator() -> Calibrator:
    global _cached
    if _cached is not None:
        return _cached
    raw = await get_config(_CONFIG_KEY)
    if not raw:
        _cached = Calibrator()
        return _cached
    try:
        _cached = Calibrator.from_json(raw)
    except Exception as e:  # noqa: BLE001
        log.warning("calibrator.load_failed", error=str(e))
        _cached = Calibrator()
    return _cached


def _invalidate_cache() -> None:
    global _cached
    _cached = None


async def calibrate(raw_prob: float) -> float:
    """Public entry — load calibrator (cached) and apply."""
    c = await load_calibrator()
    return c.calibrate(raw_prob)


# ----------------------------------------------------------------------
# Fitting
# ----------------------------------------------------------------------


async def refit_calibrator() -> Calibrator:
    """Re-fit on all resolved recs. Returns the new calibrator.

    Uses the rec's *side-specific* prob. For outcome_index==0 (YES) we bucket
    `synth_prob` directly; for index==1 (NO) we bucket `1 - synth_prob`. The
    target is the `hit` field (1 correct, 0 wrong) — so "correct" is the
    outcome for whichever side the rec was on.
    """
    async with connect() as db:
        async with db.execute(
            "SELECT synth_prob, outcome_index, hit "
            "FROM recommendations "
            "WHERE hit IS NOT NULL AND synth_prob IS NOT NULL "
            "AND source = 'weather'"
        ) as cur:
            rows = [dict(r) for r in await cur.fetchall()]

    total = len(rows)
    c = Calibrator()
    c.total_samples = total
    c.fit_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    if total < _MIN_SAMPLES:
        c.fit_note = "insufficient_data"
        await _persist(c)
        log.info(
            "calibrator.fit_skipped",
            reason="insufficient_data",
            have=total,
            need=_MIN_SAMPLES,
        )
        return c

    # Collect per-bucket (pred, outcome) pairs; target = hit for the rec's side
    bucket_preds: list[list[float]] = [[] for _ in range(_N_BUCKETS)]
    bucket_hits: list[list[int]] = [[] for _ in range(_N_BUCKETS)]
    # Track brier on raw preds for before/after comparison
    brier_before_sum = 0.0
    for r in rows:
        raw = r["synth_prob"] or 0.5
        prob_side = raw if r["outcome_index"] == 0 else 1.0 - raw
        prob_side = max(0.0, min(1.0, prob_side))
        bucket = min(int(prob_side * _N_BUCKETS), _N_BUCKETS - 1)
        bucket_preds[bucket].append(prob_side)
        bucket_hits[bucket].append(int(r["hit"]))
        brier_before_sum += (prob_side - float(r["hit"])) ** 2

    # Fit: bucket value = mean hit rate, but pull shrinkage toward the bucket
    # midpoint when sample size is low (Laplace-style prior).
    PRIOR_WEIGHT = 5.0  # equivalent sample size for the prior
    fitted_vals: list[float] = []
    counts: list[int] = []
    for i in range(_N_BUCKETS):
        n = len(bucket_preds[i])
        counts.append(n)
        prior_mean = (i + 0.5) / _N_BUCKETS
        if n == 0:
            fitted_vals.append(prior_mean)
            continue
        observed_hit_rate = sum(bucket_hits[i]) / n
        shrunk = (n * observed_hit_rate + PRIOR_WEIGHT * prior_mean) / (n + PRIOR_WEIGHT)
        fitted_vals.append(shrunk)
    c.buckets = fitted_vals
    c.bucket_counts = counts
    c.fit_note = "fitted"

    # Compute brier AFTER calibration for impact reporting
    brier_after_sum = 0.0
    for r in rows:
        raw = r["synth_prob"] or 0.5
        prob_side = raw if r["outcome_index"] == 0 else 1.0 - raw
        prob_side = max(0.0, min(1.0, prob_side))
        calibrated = c.calibrate(prob_side)
        brier_after_sum += (calibrated - float(r["hit"])) ** 2
    c.brier_before = brier_before_sum / total
    c.brier_after = brier_after_sum / total

    await _persist(c)
    log.info(
        "calibrator.fitted",
        samples=total,
        brier_before=round(c.brier_before, 4),
        brier_after=round(c.brier_after, 4),
        bucket_vals=[round(v, 3) for v in c.buckets],
    )
    return c


async def _persist(c: Calibrator) -> None:
    await set_config(_CONFIG_KEY, c.to_json())
    _invalidate_cache()
