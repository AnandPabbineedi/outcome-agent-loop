"""
Shared Cohort Object — the single definition used by detection, experiments,
and the supervisor view. Prevents three different subtly-wrong definitions.

A cohort is: { id, where: {feature: [values]}, workflow_id }
A lead matches the FIRST cohort whose where-clause is satisfied (AND across features, OR within).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)


@dataclass
class Cohort:
    id: str
    where: dict[str, list[str]]  # feature -> list of accepted values (OR within)
    workflow_id: str

    def matches(self, features: dict[str, str]) -> bool:
        """AND across keys, OR within each key's value list."""
        for feat, accepted in self.where.items():
            if features.get(feat) not in accepted:
                return False
        return True


class CohortRegistry:
    """
    Manages cohort definitions per workflow.
    Built from the workflow's current config + observed feature space.
    """
    def __init__(self):
        # wid -> ordered list of Cohort (first match wins)
        self._cohorts: dict[str, list[Cohort]] = {}

    def load_from_config(self, workflow_id: str, config: dict) -> None:
        """Read cohort definitions from a workflow config dict."""
        raw = config.get("cohorts", [])
        cohorts = []
        for c in raw:
            cohorts.append(Cohort(
                id=c["id"],
                where=c["where"],
                workflow_id=workflow_id,
            ))
        self._cohorts[workflow_id] = cohorts
        log.debug("Loaded %d cohorts for %s", len(cohorts), workflow_id)

    def assign(self, workflow_id: str, features: dict[str, str]) -> str:
        """Return cohort ID for a lead's features. 'default' if no match."""
        for cohort in self._cohorts.get(workflow_id, []):
            if cohort.matches(features):
                return cohort.id
        return "default"

    def get_cohorts(self, workflow_id: str) -> list[Cohort]:
        return self._cohorts.get(workflow_id, [])

    def all_cohort_ids(self, workflow_id: str) -> list[str]:
        ids = [c.id for c in self._cohorts.get(workflow_id, [])]
        ids.append("default")
        return ids
