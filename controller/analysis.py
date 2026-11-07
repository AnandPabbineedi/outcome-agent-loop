"""
Analysis Engine — detection of drift, incidents, hallucinations, DID degradation.

This module reads from StatTracker and produces signals that the
GovernanceClient acts on.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from .stats import StatTracker

log = logging.getLogger(__name__)

# Thresholds
HALLUC_THRESHOLD = 0.03         # > 3% flag rate triggers incident
HALLUC_MIN_CALLS = 100          # Minimum calls before triggering
DID_DEGRADED_RATIO = 0.75       # DID pickup < 75% of global → degraded
DID_MIN_DIALS = 30              # Minimum dials to assess a DID

# How many consecutive calls to look at for drift detection
DRIFT_WINDOW = 200
DRIFT_DROP_THRESHOLD = 0.03     # Absolute drop in KPI rate triggers alert


@dataclass
class CallerIDAlert:
    workflow_id: str
    did: str
    pickup_rate: float
    global_rate: float


@dataclass
class HallucinationAlert:
    workflow_id: str
    halluc_rate: float
    n_calls: int


@dataclass
class EngagementDriftAlert:
    workflow_id: str
    cohort_id: str
    dim: str
    winner: str
    all_time_rate: float
    recent_rate: float


class AnalysisEngine:
    def __init__(self, stat_tracker: StatTracker):
        self._stats = stat_tracker
        # Track known winners per (wid, cohort, dim) → (variant, rate)
        self._winners: dict[tuple[str, str, str], tuple[str, float]] = {}
        # Track active hallucination incidents
        self._halluc_incidents: dict[str, bool] = {}  # wid -> active?
        # Track degraded DIDs
        self._degraded_dids: dict[str, set[str]] = {}  # wid -> {did}
        # Track decayed variants so they are not re-promoted by stale all-time data
        self._decayed_variants: set[tuple[str, str, str, str]] = set()

    # ---------------------------------------------------------------- per tick
    def run_checks(self, workflow_id: str, cohort_ids: list[str],
                   primary_kpi: str, options: dict,
                   current_hour: int | None = None) -> dict:
        """
        Run all checks for a workflow. Returns a dict of signals.
        Called once per tick per workflow.
        """
        signals: dict = {
            "caller_id_alerts": [],
            "hallucination_alert": None,
            "decay_alerts": [],
            "new_winners": [],
        }

        # 1. Caller-ID health check
        for did in options.get("caller_id", []):
            alert = self._check_did(workflow_id, did)
            if alert:
                signals["caller_id_alerts"].append(alert)

        # 2. Hallucination check
        h_alert = self._check_hallucination(workflow_id)
        if h_alert:
            signals["hallucination_alert"] = h_alert

        # 3. Winner detection + decay detection per (cohort, dim)
        for cohort_id in cohort_ids:
            for dim in ("voice", "welcome_message"):
                key = (workflow_id, cohort_id, dim)

                # Check for decay on current winner FIRST
                if key in self._winners:
                    w_variant, w_rate = self._winners[key]
                    if self._stats.detect_decay(workflow_id, cohort_id, dim, w_variant, primary_kpi, w_rate):
                        log.warning("Decay detected: wf=%s cohort=%s dim=%s winner=%s",
                                    workflow_id, cohort_id, dim, w_variant)
                        signals["decay_alerts"].append({
                            "cohort_id": cohort_id,
                            "dim": dim,
                            "winner": w_variant,
                            "all_time_rate": w_rate,
                        })
                        # Mark as decayed so we don't immediately re-promote it
                        self._decayed_variants.add((workflow_id, cohort_id, dim, w_variant))
                        del self._winners[key]

                # Check for new winner (skipping any variant that has already decayed)
                result = self._stats.find_winner(
                    workflow_id, cohort_id, dim, primary_kpi
                )
                if result:
                    variant, rate = result
                    if (workflow_id, cohort_id, dim, variant) not in self._decayed_variants:
                        old = self._winners.get(key)
                        if not old or old[0] != variant:
                            log.info("New winner: wf=%s cohort=%s dim=%s variant=%s rate=%.3f",
                                     workflow_id, cohort_id, dim, variant, rate)
                            self._winners[key] = (variant, rate)
                            signals["new_winners"].append({
                                "cohort_id": cohort_id,
                                "dim": dim,
                                "variant": variant,
                                "rate": rate,
                            })

        return signals

    def get_winner(self, workflow_id: str, cohort_id: str, dim: str) -> tuple[str, float] | None:
        return self._winners.get((workflow_id, cohort_id, dim))

    def clear_winner(self, workflow_id: str, cohort_id: str, dim: str) -> None:
        self._winners.pop((workflow_id, cohort_id, dim), None)

    def mark_halluc_resolved(self, workflow_id: str) -> None:
        self._halluc_incidents[workflow_id] = False

    def is_halluc_active(self, workflow_id: str) -> bool:
        return self._halluc_incidents.get(workflow_id, False)

    def get_degraded_dids(self, workflow_id: str) -> set[str]:
        return self._degraded_dids.get(workflow_id, set())

    # ------------------------------------------------------------- internals
    def _check_did(self, workflow_id: str, did: str) -> CallerIDAlert | None:
        rate = self._stats.did_pickup_rate(workflow_id, did)
        if rate is None:
            return None
        global_rate = self._stats.global_pickup_rate(workflow_id)
        if global_rate is None:
            return None
        if rate < global_rate * DID_DEGRADED_RATIO:
            # Mark as degraded
            if workflow_id not in self._degraded_dids:
                self._degraded_dids[workflow_id] = set()
            self._degraded_dids[workflow_id].add(did)
            return CallerIDAlert(
                workflow_id=workflow_id,
                did=did,
                pickup_rate=rate,
                global_rate=global_rate,
            )
        else:
            # Recover if was degraded
            if workflow_id in self._degraded_dids:
                self._degraded_dids[workflow_id].discard(did)
        return None

    def _check_hallucination(self, workflow_id: str) -> HallucinationAlert | None:
        entries = self._stats._halluc_window.get(workflow_id, [])
        n = len(entries)
        if n < HALLUC_MIN_CALLS:
            return None
        rate = sum(1 for _, f in entries if f) / n
        if rate > HALLUC_THRESHOLD:
            self._halluc_incidents[workflow_id] = True
            return HallucinationAlert(
                workflow_id=workflow_id,
                halluc_rate=rate,
                n_calls=n,
            )
        # If rate dropped back below threshold, resolve
        if self._halluc_incidents.get(workflow_id):
            self.mark_halluc_resolved(workflow_id)
        return None
