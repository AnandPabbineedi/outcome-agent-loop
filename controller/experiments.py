"""
Experiment Manager — decides what to change and when.

For each workflow, maintains the current experiment state:
- What variants are being tested in each cohort+dim
- When we have a winner, proposes config change
- Handles hallucination incidents (suggest fix → validate → propose)
- Handles caller-ID rotation
- Manages call-time schedule optimisation
"""
from __future__ import annotations

import copy
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .analysis import AnalysisEngine
from .audit import AuditStore
from .client import SimClient
from .cohorts import CohortRegistry
from .governance import GovernanceClient
from .stats import StatTracker


def _sim_hour_from_ts(sim_ts: str) -> int:
    try:
        return datetime.fromisoformat(sim_ts).hour
    except Exception:
        return 12  # Default to midday

log = logging.getLogger(__name__)

# Budget guards
HALLUC_VALIDATION_N = 150   # Calls to validate a proposed prompt fix
MAX_CREDIT_SPEND_PER_INCIDENT = 20  # credits (3 for suggest + ceil(150/100)*1 for validate)


@dataclass
class WorkflowState:
    workflow_id: str
    workflow_meta: dict          # from GET /workflows/{id}
    governance: dict
    options: dict
    primary_kpi: str
    guardrail: dict
    # Experiment tracking: (cohort_id, dim) -> currently testing variants
    active_experiments: dict[tuple[str, str], list[str]] = field(default_factory=dict)
    # Hallucination incident tracking
    halluc_fix_attempted: bool = False
    halluc_fix_in_validation: str | None = None  # prompt version being validated
    validated_prompt_fix: tuple[str, str, float] | None = None  # (candidate, run_id, pass_rate)
    # Whether we've built cohorts for this workflow
    cohorts_built: bool = False


class ExperimentManager:
    def __init__(self, client: SimClient, audit: AuditStore,
                 stats: StatTracker, analysis: AnalysisEngine,
                 cohorts: CohortRegistry, governance: GovernanceClient):
        self._client = client
        self._audit = audit
        self._stats = stats
        self._analysis = analysis
        self._cohorts = cohorts
        self._gov = governance
        self._wf_states: dict[str, WorkflowState] = {}

    def load_workflows(self) -> None:
        """Fetch all workflows from sim and initialise state."""
        workflows = self._client.workflows()
        for wf in workflows:
            wid = wf["workflow_id"]
            gov = wf.get("governance", {})
            obj = wf.get("objective", {})
            primary_kpi = obj.get("primary", "engagement_rate")
            guardrail = {k: v for k, v in obj.items() if k != "primary"}
            self._wf_states[wid] = WorkflowState(
                workflow_id=wid,
                workflow_meta=wf,
                governance=gov,
                options=wf.get("options", {}),
                primary_kpi=primary_kpi,
                guardrail=guardrail,
            )
            # Build initial cohorts from current config
            cur = self._client.get_config(wid)
            self._cohorts.load_from_config(wid, cur["config"])
            log.info("Loaded workflow %s (primary_kpi=%s)", wid, primary_kpi)

    def on_tick(self, sim_ts: str, credits_remaining: int) -> None:
        """Main decision loop — called after each tick's events are ingested."""
        for wid, wf_state in self._wf_states.items():
            if self._gov.has_pending(wid):
                continue  # Wait for pending approval before proposing more
            try:
                self._run_workflow(wid, wf_state, sim_ts, credits_remaining)
            except Exception as e:
                log.exception("Error running workflow %s: %s", wid, e)

    def _run_workflow(self, wid: str, state: WorkflowState, sim_ts: str, credits: int) -> None:
        cur = self._client.get_config(wid)
        cohort_ids = self._cohorts.all_cohort_ids(wid)
        signals = self._analysis.run_checks(
            wid, cohort_ids, state.primary_kpi, state.options
        )

        # 1. Hallucination incident — highest priority
        if (signals["hallucination_alert"] or state.validated_prompt_fix) and not state.halluc_fix_attempted:
            self._handle_hallucination(wid, state, cur, sim_ts, credits)
            return

        # 2. Apply caller-ID rotation (auto-apply — no approval needed if policy allows)
        if signals["caller_id_alerts"]:
            self._handle_caller_id_rotation(wid, state, cur, signals["caller_id_alerts"], sim_ts)

        # 3. Promote winners to config
        if signals["new_winners"]:
            self._handle_winners(wid, state, cur, signals["new_winners"], sim_ts)

        # 4. Rollback decayed winners
        if signals["decay_alerts"]:
            self._handle_decay(wid, state, cur, signals["decay_alerts"], sim_ts)

        # 5. Ensure experiments are running (if we have no data for a dim, start exploring)
        self._ensure_experiments_running(wid, state, cur, sim_ts)

    # ------------------------------------------------------- hallucination
    def _handle_hallucination(self, wid: str, state: WorkflowState,
                              cur: dict, sim_ts: str, credits: int) -> None:
        # If we already have a validated fix waiting to be submitted, try proposing it
        if state.validated_prompt_fix:
            candidate, val_id, pass_rate = state.validated_prompt_fix
            min_pass = state.governance.get("min_validation_pass_rate", 0.97)
            new_config = copy.deepcopy(cur["config"])
            new_config["prompt_version"] = candidate
            evidence = {
                "type": "hallucination_fix",
                "validation_run_id": val_id,
                "current_halluc_rate": self._stats.halluc_rate_recent(wid),
                "candidate_prompt": candidate,
                "validation_pass_rate": pass_rate,
            }
            proposed = self._gov.propose_change(
                wid, cur, new_config,
                summary=f"Switch prompt to {candidate} to reduce hallucinations",
                rationale=f"Validation pass_rate={pass_rate:.3f} >= {min_pass}. "
                          f"Current halluc rate exceeds 3% threshold.",
                evidence=evidence,
                governance=state.governance,
                sim_ts=sim_ts,
                impact_score=0.8,
                affected_leads=state.workflow_meta.get("daily_batch", {}).get("size", 1000),
            )
            if proposed:
                state.halluc_fix_attempted = True
                state.validated_prompt_fix = None
            return

        if credits < MAX_CREDIT_SPEND_PER_INCIDENT:
            log.warning("Low credits (%d) — skipping hallucination fix for %s", credits, wid)
            return

        log.info("Handling hallucination incident for %s", wid)

        try:
            fixes = self._client.suggest_fixes(wid, "high hallucination rate detected")
        except Exception as e:
            log.exception("suggest_fixes failed for %s: %s", wid, e)
            return

        suggestions = fixes.get("suggestions", [])
        if not suggestions:
            log.warning("No suggestions returned for %s", wid)
            return

        self._audit.log_decision(sim_ts, wid, "halluc_suggest_fixes",
                                  {"suggestions": suggestions})

        # Validate top suggestion (avoid P4 — high halluc risk)
        current_prompt = cur["config"].get("prompt_version", "P1")
        for sug in suggestions:
            candidate = sug.get("prompt_version")
            if not candidate or candidate == current_prompt:
                continue
            # Skip P4 (persuasive) — 2.5% halluc rate is worse than helpful
            if candidate == "P4":
                log.info("Skipping P4 (high halluc risk) for %s", wid)
                continue

            try:
                validation = self._client.run_validation(wid, candidate, n=HALLUC_VALIDATION_N)
            except Exception as e:
                log.exception("Validation failed for %s/%s: %s", wid, candidate, e)
                continue

            pass_rate = validation.get("pass_rate", 0.0)
            min_pass = state.governance.get("min_validation_pass_rate", 0.97)
            val_id = validation.get("validation_run_id")
            self._audit.log_decision(sim_ts, wid, "halluc_validation",
                                      {"prompt": candidate, "pass_rate": pass_rate,
                                       "min_pass": min_pass})

            if pass_rate >= min_pass:
                state.validated_prompt_fix = (candidate, val_id, pass_rate)
                # Try proposing now
                new_config = copy.deepcopy(cur["config"])
                new_config["prompt_version"] = candidate
                evidence = {
                    "type": "hallucination_fix",
                    "validation_run_id": val_id,
                    "current_halluc_rate": self._stats.halluc_rate_recent(wid),
                    "candidate_prompt": candidate,
                    "validation_pass_rate": pass_rate,
                }
                proposed = self._gov.propose_change(
                    wid, cur, new_config,
                    summary=f"Switch prompt to {candidate} to reduce hallucinations",
                    rationale=f"Validation pass_rate={pass_rate:.3f} >= {min_pass}. "
                              f"Current halluc rate exceeds 3% threshold.",
                    evidence=evidence,
                    governance=state.governance,
                    sim_ts=sim_ts,
                    impact_score=0.8,
                    affected_leads=state.workflow_meta.get("daily_batch", {}).get("size", 1000),
                )
                if proposed:
                    state.halluc_fix_attempted = True
                    state.validated_prompt_fix = None
                return

        log.warning("No valid prompt fix found for %s", wid)

    # ----------------------------------------------------- caller-ID rotation
    def _handle_caller_id_rotation(self, wid: str, state: WorkflowState,
                                   cur: dict, alerts: list, sim_ts: str) -> None:
        degraded_dids = {a.did for a in alerts}
        good_dids = [d for d in state.options.get("caller_id", []) if d not in degraded_dids]
        if not good_dids:
            log.warning("No good DIDs available for %s!", wid)
            return

        new_config = copy.deepcopy(cur["config"])
        # Redistribute allocation evenly among good DIDs
        new_alloc = {d: 1 for d in good_dids}
        for cohort in new_config.get("allocations", {}):
            new_config["allocations"][cohort]["caller_id"] = new_alloc

        # Try auto-apply first (caller_id is often in allowed_dims)
        applied = self._gov.try_auto_apply(
            wid, cur, new_config,
            summary=f"Rotate away from degraded DIDs: {degraded_dids}",
            rationale=f"DIDs {degraded_dids} have pickup rate < 75% of global average",
            governance=state.governance,
            sim_ts=sim_ts,
        )
        if not applied:
            # Fall back to governance proposal
            self._gov.propose_change(
                wid, cur, new_config,
                summary=f"Rotate caller-IDs away from degraded: {sorted(degraded_dids)}",
                rationale=f"DIDs {sorted(degraded_dids)} showing degraded pickup vs global baseline",
                evidence={"degraded_dids": list(degraded_dids),
                          "alerts": [{"did": a.did, "rate": a.pickup_rate, "global": a.global_rate}
                                     for a in alerts]},
                governance=state.governance,
                sim_ts=sim_ts,
                impact_score=0.6,
            )

    # -------------------------------------------------------- winner promotion
    def _handle_winners(self, wid: str, state: WorkflowState,
                        cur: dict, new_winners: list, sim_ts: str) -> None:
        """Build a consolidated config change for all new winners and propose it."""
        new_config = copy.deepcopy(cur["config"])
        changes = []

        for w in new_winners:
            cohort_id = w["cohort_id"]
            dim = w["dim"]
            variant = w["variant"]
            rate = w["rate"]

            # Set this cohort's dim allocation to 100% winner
            if "allocations" not in new_config:
                new_config["allocations"] = {}
            if cohort_id not in new_config["allocations"]:
                new_config["allocations"][cohort_id] = {}
            new_config["allocations"][cohort_id][dim] = {variant: 1}
            changes.append(f"{cohort_id}/{dim}→{variant}({rate:.3f})")

        if not changes:
            return

        summary = f"Promote winners: {', '.join(changes)}"
        evidence = {
            "type": "winner_promotion",
            "winners": new_winners,
            "primary_kpi": state.primary_kpi,
            "min_arm_calls": 150,
            "p_threshold": 0.05,
        }

        # Try auto-apply if all changed dims are allowed
        applied = self._gov.try_auto_apply(
            wid, cur, new_config, summary,
            rationale="Statistically significant winner (p<0.05, n>=150, lift>=2%)",
            governance=state.governance, sim_ts=sim_ts,
        )
        if not applied:
            self._gov.propose_change(
                wid, cur, new_config, summary,
                rationale="Statistically significant winner (chi-sq p<0.05, min 150 calls/arm, >=2% lift)",
                evidence=evidence,
                governance=state.governance,
                sim_ts=sim_ts,
                impact_score=min(0.9, 0.5 + rate * 0.3),
            )

    # ------------------------------------------------------- decay / rollback
    def _handle_decay(self, wid: str, state: WorkflowState,
                      cur: dict, decay_alerts: list, sim_ts: str) -> None:
        new_config = copy.deepcopy(cur["config"])
        changes = []

        for alert in decay_alerts:
            cohort_id = alert["cohort_id"]
            dim = alert["dim"]
            # Rollback: re-open the allocation to explore all variants equally
            all_variants = state.options.get(dim, [])
            if not all_variants:
                continue
            if cohort_id not in new_config.get("allocations", {}):
                new_config.setdefault("allocations", {})[cohort_id] = {}
            new_config["allocations"][cohort_id][dim] = {v: 1 for v in all_variants}
            changes.append(f"{cohort_id}/{dim} → explore {all_variants}")

        if not changes:
            return

        summary = f"Rollback decayed winners: {', '.join(changes)}"
        self._gov.try_auto_apply(
            wid, cur, new_config, summary,
            rationale="Winner variant performance degraded >5% relative — reopening experiment",
            governance=state.governance, sim_ts=sim_ts,
        ) or self._gov.propose_change(
            wid, cur, new_config, summary,
            rationale="Winner performance decayed > 5% relative. Reopening to explore alternatives.",
            evidence={"type": "decay_rollback", "alerts": decay_alerts},
            governance=state.governance, sim_ts=sim_ts,
            impact_score=0.5,
        )

    # ------------------------------------------------- ensure experiments run
    def _ensure_experiments_running(self, wid: str, state: WorkflowState,
                                    cur: dict, sim_ts: str) -> None:
        """
        If a dimension in a cohort is 100% on one variant (not testing),
        and we haven't found a winner yet, open it up to explore.
        Uses the approval path (since full uniform allocation usually exceeds auto-apply max_shift).
        Only submits one proposal per workflow at a time.
        """
        sim_hour = _sim_hour_from_ts(sim_ts)
        approver_hours = state.governance.get("approver_hours", [10, 19])
        if not (approver_hours[0] <= sim_hour < approver_hours[1]):
            return  # Outside hours — wait for approver window

        if self._gov.has_pending(wid):
            return  # Already have a pending approval

        config = cur["config"]
        alloc = config.get("allocations", {})

        # Self-healing: if an experiment was marked active but config only has 1 variant
        # and no approval is pending, the proposal was declined/expired/lapsed — allow retrying
        for exp_k in list(state.active_experiments.keys()):
            coh, d = exp_k
            cur_dim = alloc.get(coh, {}).get(d, {})
            if len(cur_dim) <= 1:
                del state.active_experiments[exp_k]

        new_config = copy.deepcopy(config)
        changed = False
        changes = []
        chosen_exp_key = None
        chosen_options = None

        for cohort_id in ["default"]:  # Start with default only to keep proposals focused
            cohort_alloc = alloc.get(cohort_id, {})
            for dim in ("voice", "welcome_message"):
                all_options = state.options.get(dim, [])
                if len(all_options) < 2:
                    continue
                current_dim_alloc = cohort_alloc.get(dim, {})
                # Already exploring if >1 variant in allocation
                if len(current_dim_alloc) > 1:
                    exp_key = (cohort_id, dim)
                    state.active_experiments[exp_key] = list(current_dim_alloc.keys())
                    continue
                winner = self._analysis.get_winner(wid, cohort_id, dim)
                if winner:
                    continue  # Already have winner — leave it
                exp_key = (cohort_id, dim)
                if exp_key in state.active_experiments:
                    continue  # Already tracking
                # Start uniform exploration
                if "allocations" not in new_config:
                    new_config["allocations"] = {}
                if cohort_id not in new_config["allocations"]:
                    new_config["allocations"][cohort_id] = {}
                new_config["allocations"][cohort_id][dim] = {v: 1 for v in all_options}
                changes.append(f"{cohort_id}/{dim}")
                changed = True
                chosen_exp_key = exp_key
                chosen_options = all_options
                break
            if changed:
                break

        if not changed or not chosen_exp_key:
            return

        summary = f"Start experiment: {', '.join(changes)}"
        log.info("Starting experiments for %s: %s", wid, changes)

        # Try auto-apply first (only works if TVD <= max_shift and dim is allowed)
        applied = self._gov.try_auto_apply(
            wid, cur, new_config, summary,
            rationale="Uniform exploration of untested variants to find winners",
            governance=state.governance, sim_ts=sim_ts,
        )
        if applied:
            state.active_experiments[chosen_exp_key] = chosen_options
        else:
            proposed = self._gov.propose_change(
                wid, cur, new_config, summary,
                rationale="Uniform exploration: testing all options with equal allocation. "
                          "Winner promoted once p<0.05, n>=150 per arm, lift>=2%.",
                evidence={"type": "exploration", "dims": changes},
                governance=state.governance,
                sim_ts=sim_ts,
                impact_score=0.4,
                affected_leads=state.workflow_meta.get("daily_batch", {}).get("size", 1000),
            )
            if proposed:
                state.active_experiments[chosen_exp_key] = chosen_options

    # ------------------------------------------------------- event handlers
    def on_config_changed(self, event: dict, sim_ts: str) -> None:
        """Called when a config_changed event is detected."""
        wid = event.get("workflow_id")
        actor = event.get("actor", "")
        self._gov.notify_config_changed(wid, actor, sim_ts)
        # Reload cohort definitions
        try:
            cur = self._client.get_config(wid)
            self._cohorts.load_from_config(wid, cur["config"])
        except Exception as e:
            log.exception("Error reloading config for %s: %s", wid, e)
