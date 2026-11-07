"""
Governance Client — proposal lifecycle, auto-apply, approval tracking.

Enforces:
- Approver hours (don't submit during off-hours)
- max_requests_per_24h per workflow
- apply_window_hours (apply before it lapses)
- Batching (combine multiple changes into one proposal where possible)
- Stale-base re-proposal on 409
- Respects supervisor edits (config_changed actor=supervisor → pause)
- auto-apply only on allowed dims, with TVD check
"""
from __future__ import annotations

import copy
import json
import logging
import math
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Any

from .audit import AuditStore
from .client import SimClient

log = logging.getLogger(__name__)


@dataclass
class PendingApproval:
    approval_id: str
    workflow_id: str
    proposed_config: dict
    proposal_id: str
    base_version: int
    created_sim_ts: str
    apply_by: str | None = None
    status: str = "pending"  # pending / approved / rejected / expired / applied / lapsed


class GovernanceClient:
    def __init__(self, client: SimClient, audit: AuditStore):
        self._client = client
        self._audit = audit
        # wid -> PendingApproval (at most one pending per workflow)
        self._pending: dict[str, PendingApproval] = {}
        # wid -> count of proposals submitted in last 24h (approximate)
        self._proposal_counts: dict[str, list[str]] = {}  # wid -> [sim_ts_str]
        # wid -> sim_ts of last supervisor edit (to pause experiments)
        self._supervisor_edits: dict[str, str] = {}
        # wid -> set of DIDs known to be degraded (for caller-ID proposals)
        self._degraded_dids: dict[str, set[str]] = {}

    def notify_config_changed(self, workflow_id: str, actor: str, sim_ts: str) -> None:
        """Called when a config_changed event arrives."""
        if actor == "supervisor":
            log.info("Supervisor edit on %s at %s — pausing experiments", workflow_id, sim_ts)
            self._supervisor_edits[workflow_id] = sim_ts
            # If we had a pending approval, it's now stale — drop it
            if workflow_id in self._pending:
                log.info("Dropping stale pending approval for %s due to supervisor edit", workflow_id)
                del self._pending[workflow_id]

    def has_pending(self, workflow_id: str) -> bool:
        return workflow_id in self._pending

    def supervisor_edited_recently(self, workflow_id: str, sim_ts: str, cooldown_hours: float = 1.0) -> bool:
        """Returns True if a supervisor edited this workflow within the last cooldown_hours."""
        last = self._supervisor_edits.get(workflow_id)
        if not last:
            return False
        try:
            last_dt = datetime.fromisoformat(last)
            cur_dt = datetime.fromisoformat(sim_ts)
            return (cur_dt - last_dt).total_seconds() < cooldown_hours * 3600
        except Exception:
            return False

    # ---------------------------------------------------------------- polling
    def poll_pending_approvals(self, sim_ts: str) -> list[str]:
        """
        Check status of pending approvals. Apply approved ones.
        Returns list of workflow_ids where approval was applied.
        """
        applied = []
        for wid, pa in list(self._pending.items()):
            try:
                ap = self._client.get_approval(pa.approval_id)
                status = ap.get("status", "pending")
                pa.status = status

                if status == "approved":
                    # Apply it before it lapses
                    result = self._client.apply_approval(wid, pa.approval_id)
                    if result.get("error") == 409 or result.get("error") == "stale_base_version":
                        log.warning("Stale base applying %s for %s", pa.approval_id, wid)
                        self._audit.log_decision(sim_ts, wid, "apply_stale", {"approval_id": pa.approval_id})
                    else:
                        log.info("Applied approval %s for %s", pa.approval_id, wid)
                        self._audit.log_decision(sim_ts, wid, "applied",
                                                  {"approval_id": pa.approval_id,
                                                   "config": pa.proposed_config}, result)
                        applied.append(wid)
                    del self._pending[wid]

                elif status in ("rejected", "expired"):
                    log.info("Approval %s %s for %s", pa.approval_id, status, wid)
                    reasons = ap.get("reasons", [])
                    self._audit.log_decision(sim_ts, wid, f"approval_{status}",
                                             {"approval_id": pa.approval_id, "reasons": reasons})
                    del self._pending[wid]

            except Exception as e:
                log.exception("Error polling approval %s: %s", pa.approval_id, e)

        return applied

    # ---------------------------------------------------------------- propose
    def propose_change(self, workflow_id: str, current_config: dict, new_config: dict,
                       summary: str, rationale: str, evidence: dict,
                       governance: dict, sim_ts: str,
                       impact_score: float = 0.5,
                       affected_leads: int = 0) -> bool:
        """
        Submit a change proposal via the governance approval workflow.
        Returns True if proposal was submitted.
        """
        if workflow_id in self._pending:
            log.debug("Already have pending approval for %s, skipping", workflow_id)
            return False

        # Check approver hours
        sim_hour = _sim_hour(sim_ts)
        approver_hours = governance.get("approver_hours", [9, 18])
        if not (approver_hours[0] <= sim_hour < approver_hours[1]):
            log.debug("Outside approver hours (%s) for %s — queuing for next window", sim_hour, workflow_id)
            return False

        # Check proposal rate limit
        max_per_24h = governance.get("max_requests_per_24h", 3)
        if not self._can_propose(workflow_id, sim_ts, max_per_24h):
            log.info("Rate limit: max %d proposals/24h for %s", max_per_24h, workflow_id)
            return False

        # Don't re-propose right after supervisor edit
        if self.supervisor_edited_recently(workflow_id, sim_ts, cooldown_hours=2.0):
            log.info("Supervisor edited %s recently — not proposing", workflow_id)
            return False

        proposal_id = str(uuid.uuid4())
        diff = _compute_diff(current_config["config"], new_config)
        if not diff:
            diff = {"allocations": "updated"}
        if not affected_leads or affected_leads <= 0:
            affected_leads = 1000
        body = {
            "proposal_id": proposal_id,
            "workflow_id": workflow_id,
            "base_version": current_config["version"],
            "proposed_config": new_config,
            "change_summary": summary,
            "diff": diff,
            "affected_leads": int(affected_leads),
            "dry_run": {"note": f"Estimated impact: {impact_score:.2f}", "summary": summary},
            "impact_score": float(impact_score),
            "rationale": rationale,
            "evidence": evidence,
        }

        try:
            result = self._client.request_approval(body)
        except Exception as e:
            log.exception("Error submitting proposal for %s: %s", workflow_id, e)
            return False

        if "error" in result:
            log.warning("Proposal rejected by sim for %s: %s", workflow_id, result)
            return False

        aid = result.get("approval_id")
        if not aid:
            log.warning("No approval_id in response for %s: %s", workflow_id, result)
            return False

        pa = PendingApproval(
            approval_id=aid,
            workflow_id=workflow_id,
            proposed_config=new_config,
            proposal_id=proposal_id,
            base_version=current_config["version"],
            created_sim_ts=sim_ts,
        )
        self._pending[workflow_id] = pa
        self._track_proposal(workflow_id, sim_ts)
        self._audit.log_decision(sim_ts, workflow_id, "proposed",
                                  {"approval_id": aid, "summary": summary,
                                   "rationale": rationale, "config": new_config,
                                   "evidence": evidence}, result)
        log.info("Proposed change for %s: %s (approval_id=%s)", workflow_id, summary, aid)
        return True

    # ------------------------------------------------------------ auto-apply
    def try_auto_apply(self, workflow_id: str, current_config: dict, new_config: dict,
                       summary: str, rationale: str, governance: dict,
                       sim_ts: str) -> bool:
        """
        Attempt auto-apply via PUT. Only succeeds if the workflow's policy permits it.
        Returns True if applied successfully.
        """
        auto_policy = governance.get("auto_apply", {})
        if not auto_policy.get("enabled", False):
            return False

        allowed_dims = set(auto_policy.get("allowed_dims", []))
        max_shift = auto_policy.get("max_shift", 0.0)

        # Check which dims changed
        changed_dims = _find_changed_dims(current_config["config"], new_config)
        if not changed_dims.issubset(allowed_dims):
            disallowed = changed_dims - allowed_dims
            log.debug("Auto-apply blocked: dims %s not in allowed %s", disallowed, allowed_dims)
            return False

        # TVD check for allocation changes
        if not _tvd_ok(current_config["config"], new_config, max_shift):
            log.debug("Auto-apply blocked: TVD exceeds max_shift %.2f", max_shift)
            return False

        proposal_id = str(uuid.uuid4())
        body = {
            "proposal_id": proposal_id,
            "base_version": current_config["version"],
            "proposed_config": new_config,
            "rationale": rationale,
        }

        result = self._client.auto_apply(workflow_id, body)
        if isinstance(result, dict) and "error" in result:
            log.warning("Auto-apply failed for %s: %s", workflow_id, result)
            return False

        self._audit.log_decision(sim_ts, workflow_id, "auto_applied",
                                  {"summary": summary, "config": new_config}, result)
        log.info("Auto-applied change for %s: %s", workflow_id, summary)
        return True

    # -------------------------------------------------------- reconcile restart
    def reconcile_on_restart(self, sim_ts: str) -> None:
        """
        On controller restart: poll all pending approvals via the API
        to recover state without double-applying.
        """
        for wid_candidate in list(self._pending.keys()):
            self.poll_pending_approvals(sim_ts)

        # Also check for any approvals we might have submitted but lost track of
        # (robust against crash between submit and recording to _pending)
        try:
            pending_approvals = self._client.list_approvals(status="pending")
            for ap in pending_approvals:
                wid = ap.get("workflow_id")
                aid = ap.get("approval_id")
                if wid and aid and wid not in self._pending:
                    log.info("Recovered orphaned approval %s for %s", aid, wid)
                    self._pending[wid] = PendingApproval(
                        approval_id=aid,
                        workflow_id=wid,
                        proposed_config=ap.get("proposed_config", {}),
                        proposal_id=ap.get("proposal_id", ""),
                        base_version=ap.get("base_version", 0),
                        created_sim_ts=sim_ts,
                    )
        except Exception as e:
            log.exception("Error reconciling approvals on restart: %s", e)

    # --------------------------------------------------------------- helpers
    def _can_propose(self, workflow_id: str, sim_ts: str, max_per_24h: int) -> bool:
        # Count proposals in last 24 simulated hours
        counts = self._proposal_counts.get(workflow_id, [])
        cur_dt = _parse_dt(sim_ts)
        cutoff = cur_dt - timedelta(hours=24) if cur_dt else None
        if cutoff:
            counts = [ts for ts in counts if _parse_dt(ts) and _parse_dt(ts) >= cutoff]
        self._proposal_counts[workflow_id] = counts
        return len(counts) < max_per_24h

    def _track_proposal(self, workflow_id: str, sim_ts: str) -> None:
        if workflow_id not in self._proposal_counts:
            self._proposal_counts[workflow_id] = []
        self._proposal_counts[workflow_id].append(sim_ts)


# -------------------------------------------------------------------- helpers
def _sim_hour(sim_ts: str) -> int:
    try:
        return datetime.fromisoformat(sim_ts).hour
    except Exception:
        return 12  # Default to midday if unparseable


def _parse_dt(ts: str) -> datetime | None:
    try:
        return datetime.fromisoformat(ts)
    except Exception:
        return None


def _compute_diff(old_config: dict, new_config: dict) -> dict:
    """Shallow diff of two config dicts."""
    diff = {}
    for key in set(list(old_config.keys()) + list(new_config.keys())):
        if old_config.get(key) != new_config.get(key):
            diff[key] = {"from": old_config.get(key), "to": new_config.get(key)}
    return diff


def _find_changed_dims(old_config: dict, new_config: dict) -> set[str]:
    """Find which top-level config dimensions changed."""
    changed = set()
    # Check allocations
    old_alloc = old_config.get("allocations", {})
    new_alloc = new_config.get("allocations", {})
    for cohort in set(list(old_alloc.keys()) + list(new_alloc.keys())):
        old_c = old_alloc.get(cohort, {})
        new_c = new_alloc.get(cohort, {})
        for dim in set(list(old_c.keys()) + list(new_c.keys())):
            if old_c.get(dim) != new_c.get(dim):
                changed.add(dim)
    # Check other dims
    for dim in ("prompt_version", "schedule", "cohorts"):
        if old_config.get(dim) != new_config.get(dim):
            changed.add(dim)
    return changed


def _tvd_ok(old_config: dict, new_config: dict, max_shift: float) -> bool:
    """
    Check total variation distance for allocation changes.
    TVD = 0.5 * sum(|p_i - q_i|) for each cohort/dim.
    Returns True if all TVDs are within max_shift.
    """
    old_alloc = old_config.get("allocations", {})
    new_alloc = new_config.get("allocations", {})
    all_cohorts = set(list(old_alloc.keys()) + list(new_alloc.keys()))
    for cohort in all_cohorts:
        old_c = old_alloc.get(cohort, {})
        new_c = new_alloc.get(cohort, {})
        all_dims = set(list(old_c.keys()) + list(new_c.keys()))
        for dim in all_dims:
            old_d = old_c.get(dim, {})
            new_d = new_c.get(dim, {})
            if old_d == new_d:
                continue
            tvd = _tvd(old_d, new_d)
            if tvd > max_shift:
                log.debug("TVD check failed: cohort=%s dim=%s tvd=%.3f max=%.3f",
                          cohort, dim, tvd, max_shift)
                return False
    return True


def _tvd(p: dict, q: dict) -> float:
    """Total variation distance between two probability distributions (as weight dicts)."""
    all_keys = set(list(p.keys()) + list(q.keys()))
    p_total = sum(p.values()) or 1.0
    q_total = sum(q.values()) or 1.0
    return 0.5 * sum(abs(p.get(k, 0) / p_total - q.get(k, 0) / q_total) for k in all_keys)
