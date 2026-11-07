"""
Per-variant, per-cohort call statistics tracker.

Tracks connected/dials (pickup) and engaged/connected (engagement) for each
combination of (workflow, cohort, dimension, variant_value, config_version).

Also tracks caller-ID health separately since it needs rolling windows.
"""
from __future__ import annotations

import logging
import math
from collections import defaultdict
from dataclasses import dataclass, field
from typing import NamedTuple

log = logging.getLogger(__name__)

# Minimum calls per arm before we consider running a significance test
MIN_ARM_CALLS = 150
# Minimum relative improvement to bother promoting (practical significance)
MIN_RELATIVE_LIFT = 0.02
# p-value threshold (chi-squared)
P_THRESHOLD = 0.05


class ArmKey(NamedTuple):
    workflow_id: str
    cohort_id: str
    dim: str           # 'voice', 'welcome_message', 'caller_id', 'prompt_version'
    variant: str       # e.g. 'V3'
    config_version: int


@dataclass
class ArmStats:
    dials: int = 0
    connected: int = 0
    engaged: int = 0
    # Hallucination tracking (only for connected calls)
    halluc_flags: int = 0
    halluc_calls: int = 0
    # Rolling 48h window for decay detection
    recent_dials: list[float] = field(default_factory=list)      # timestamps
    recent_connected: list[float] = field(default_factory=list)
    recent_engaged: list[float] = field(default_factory=list)

    @property
    def pickup_rate(self) -> float:
        return self.connected / self.dials if self.dials else 0.0

    @property
    def engagement_rate(self) -> float:
        return self.engaged / self.connected if self.connected else 0.0

    @property
    def halluc_rate(self) -> float:
        return self.halluc_flags / self.halluc_calls if self.halluc_calls else 0.0


class StatTracker:
    """Accumulates call events into ArmStats per (workflow, cohort, dim, variant, version)."""

    def __init__(self):
        self._arms: dict[ArmKey, ArmStats] = defaultdict(ArmStats)
        # caller-ID health: wid -> did -> (dials, connected) over rolling 2-day window
        self._did_window: dict[str, dict[str, list[tuple[float, bool]]]] = defaultdict(lambda: defaultdict(list))
        # hallucination rolling: wid -> list of (ts, flag)
        self._halluc_window: dict[str, list[tuple[float, bool]]] = defaultdict(list)

    def ingest_call(self, event: dict, cohort_id: str, sim_ts_epoch: float) -> None:
        """Process one call event."""
        wid = event["workflow_id"]
        variant = event.get("variant", {})
        connected = event.get("connected", False)
        engaged = event.get("engaged", False)
        config_version = event.get("config_version", 0)
        qa = event.get("qa", {})

        # Update per-arm stats for each dimension in the variant
        for dim in ("voice", "welcome_message", "caller_id"):
            val = variant.get(dim)
            if not val:
                continue
            key = ArmKey(wid, cohort_id, dim, val, config_version)
            arm = self._arms[key]
            arm.dials += 1
            if connected:
                arm.connected += 1
            if engaged:
                arm.engaged += 1
            if connected and qa:
                arm.halluc_calls += 1
                if qa.get("hallucination_flag"):
                    arm.halluc_flags += 1

        # Caller-ID rolling window (keep last 2 simulated days = 2880 minutes)
        did = variant.get("caller_id")
        if did:
            self._did_window[wid][did].append((sim_ts_epoch, connected))

        # Hallucination rolling window
        if connected and qa:
            self._halluc_window[wid].append((sim_ts_epoch, bool(qa.get("hallucination_flag"))))

    def get_arm_stats(self, key: ArmKey) -> ArmStats:
        return self._arms.get(key, ArmStats())

    def arms_for(self, workflow_id: str, cohort_id: str, dim: str,
                 config_version: int | None = None) -> dict[str, ArmStats]:
        """Return {variant_value: ArmStats} for a given (wf, cohort, dim)."""
        result = {}
        for key, stats in self._arms.items():
            if key.workflow_id != workflow_id:
                continue
            if key.cohort_id != cohort_id:
                continue
            if key.dim != dim:
                continue
            if config_version is not None and key.config_version != config_version:
                continue
            result[key.variant] = stats
        return result

    def prune_old_events(self, cutoff_epoch: float) -> None:
        """Remove rolling-window entries older than cutoff."""
        for wid in self._did_window:
            for did in self._did_window[wid]:
                self._did_window[wid][did] = [
                    (ts, c) for ts, c in self._did_window[wid][did] if ts >= cutoff_epoch
                ]
        for wid in self._halluc_window:
            self._halluc_window[wid] = [
                (ts, f) for ts, f in self._halluc_window[wid] if ts >= cutoff_epoch
            ]

    # --------------------------------------------------------------- DID health
    def did_pickup_rate(self, workflow_id: str, did: str, window_minutes: float = 2880.0) -> float | None:
        """Pickup rate for a DID over its recent window. None if insufficient data."""
        entries = self._did_window.get(workflow_id, {}).get(did, [])
        if not entries:
            return None
        total = len(entries)
        connected = sum(1 for _, c in entries if c)
        if total < 20:
            return None
        return connected / total

    def global_pickup_rate(self, workflow_id: str) -> float | None:
        """Average pickup rate across all DIDs for a workflow."""
        all_entries = []
        for did_entries in self._did_window.get(workflow_id, {}).values():
            all_entries.extend(did_entries)
        if len(all_entries) < 50:
            return None
        return sum(1 for _, c in all_entries if c) / len(all_entries)

    # ---------------------------------------------------------- hallucination
    def halluc_rate_recent(self, workflow_id: str, min_calls: int = 100) -> float | None:
        entries = self._halluc_window.get(workflow_id, [])
        if len(entries) < min_calls:
            return None
        return sum(1 for _, f in entries if f) / len(entries)

    # ---------------------------------------------------------- significance
    @staticmethod
    def chi_squared_p(a_success: int, a_total: int, b_success: int, b_total: int) -> float:
        """
        2x2 chi-squared test: success vs. failure across two arms.
        Returns p-value approximation. Uses continuity correction (Yates).
        Returns 1.0 if insufficient data.
        """
        if a_total < 10 or b_total < 10:
            return 1.0
        a_fail = a_total - a_success
        b_fail = b_total - b_success
        total = a_total + b_total
        total_success = a_success + b_success
        total_fail = a_fail + b_fail
        if total_success == 0 or total_fail == 0:
            return 1.0
        # Expected values
        ea_s = a_total * total_success / total
        ea_f = a_total * total_fail / total
        eb_s = b_total * total_success / total
        eb_f = b_total * total_fail / total
        if ea_s < 5 or ea_f < 5 or eb_s < 5 or eb_f < 5:
            return 1.0
        # Chi-squared with Yates correction
        chi2 = (
            (abs(a_success - ea_s) - 0.5) ** 2 / ea_s +
            (abs(a_fail - ea_f) - 0.5) ** 2 / ea_f +
            (abs(b_success - eb_s) - 0.5) ** 2 / eb_s +
            (abs(b_fail - eb_f) - 0.5) ** 2 / eb_f
        )
        # Approximate p from chi2 with 1 df (Abramowitz & Stegun 26.4.5)
        p = _chi2_sf(chi2)
        return p

    def find_winner(self, workflow_id: str, cohort_id: str, dim: str,
                    primary_kpi: str, config_version: int | None = None) -> tuple[str, float] | None:
        """
        Returns (variant, rate) of the best arm if it's statistically significantly
        better than all others and meets practical significance threshold.
        Returns None if no clear winner yet.
        """
        arms = self.arms_for(workflow_id, cohort_id, dim, config_version)
        if len(arms) < 2:
            return None

        # Pick metric
        if primary_kpi == "engagement_rate":
            def rate(s: ArmStats) -> tuple[int, int]:
                return s.engaged, s.connected
        else:
            def rate(s: ArmStats) -> tuple[int, int]:
                return s.connected, s.dials

        # Filter arms with enough data
        eligible = {v: s for v, s in arms.items() if s.dials >= MIN_ARM_CALLS}
        if len(eligible) < 2:
            return None

        # Find best by rate
        best_v = max(eligible, key=lambda v: (rate(eligible[v])[0] / max(rate(eligible[v])[1], 1)))
        best_s, best_t = rate(eligible[best_v])
        best_rate = best_s / best_t if best_t else 0.0

        # Must beat ALL others significantly
        for v, s in eligible.items():
            if v == best_v:
                continue
            other_s, other_t = rate(s)
            other_rate = other_s / other_t if other_t else 0.0
            # Practical significance
            if best_rate <= other_rate * (1 + MIN_RELATIVE_LIFT):
                return None
            # Statistical significance
            p = self.chi_squared_p(best_s, best_t, other_s, other_t)
            if p >= P_THRESHOLD:
                return None

        return best_v, best_rate

    def detect_decay(self, workflow_id: str, cohort_id: str, dim: str,
                     winner_variant: str, primary_kpi: str,
                     all_time_rate: float) -> bool:
        """
        Returns True if the winner variant's recent performance has decayed
        more than 5% relative from its all-time rate.
        Uses last 48h of data (rolling window already pruned).
        """
        arms = self.arms_for(workflow_id, cohort_id, dim)
        winner_stats = arms.get(winner_variant)
        if not winner_stats:
            return False
        if primary_kpi == "engagement_rate":
            recent_rate = winner_stats.engagement_rate
        else:
            recent_rate = winner_stats.pickup_rate
        if winner_stats.dials < 50:
            return False
        relative_drop = (all_time_rate - recent_rate) / all_time_rate if all_time_rate > 0 else 0
        return relative_drop > 0.05


# ----------------------------------------------------------------- chi2 helper
def _chi2_sf(x: float) -> float:
    """Survival function (1 - CDF) for chi-squared distribution with 1 df."""
    # Equivalent to: from scipy.stats import chi2; chi2.sf(x, 1)
    # Using regularized incomplete gamma: sf = 1 - gamma_inc(0.5, x/2) / gamma(0.5)
    # Approximation accurate enough for our purposes
    if x <= 0:
        return 1.0
    # erfc(sqrt(x/2)) is a good approximation for chi2(1) sf
    return math.erfc(math.sqrt(x / 2))
