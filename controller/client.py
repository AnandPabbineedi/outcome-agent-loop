"""
Thin HTTP client wrapper around the simulator API.
All calls go through here so we can log, retry on 5xx, and handle credits gracefully.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any, Iterator

import httpx

log = logging.getLogger(__name__)


class SimClient:
    def __init__(self, base_url: str, timeout: float = 120.0):
        self._c = httpx.Client(base_url=base_url, timeout=timeout)
        self.base_url = base_url

    # ------------------------------------------------------------------ basics
    def health(self) -> dict:
        return self._get("/health")

    def state(self) -> dict:
        return self._get("/sim/state")

    def advance(self, minutes: int) -> dict:
        return self._post("/sim/advance", {"minutes": minutes})

    # ---------------------------------------------------------------- workflows
    def workflows(self) -> list[dict]:
        return self._get("/workflows")

    def workflow(self, wid: str) -> dict:
        return self._get(f"/workflows/{wid}")

    def get_config(self, wid: str) -> dict:
        return self._get(f"/workflows/{wid}/config")

    def config_history(self, wid: str) -> list[dict]:
        return self._get(f"/workflows/{wid}/config/history")

    # ------------------------------------------------------------------ events
    def list_chunks(self, kind: str = "events", after: int = -1) -> list[dict]:
        return self._get("/events/chunks", params={"kind": kind, "after": after})

    def list_history_chunks(self) -> list[dict]:
        return self._get("/events/chunks", params={"kind": "history"})

    def get_chunk_lines(self, chunk_n: int) -> Iterator[dict]:
        """Stream NDJSON lines from a chunk."""
        url = f"{self.base_url}/events/chunks/{chunk_n}"
        with self._c.stream("GET", f"/events/chunks/{chunk_n}") as r:
            r.raise_for_status()
            for line in r.iter_lines():
                line = line.strip()
                if line:
                    yield json.loads(line)

    # --------------------------------------------------------------- approvals
    def request_approval(self, body: dict) -> dict:
        return self._post("/approvals", body)

    def get_approval(self, aid: str) -> dict:
        return self._get(f"/approvals/{aid}")

    def list_approvals(self, workflow_id: str | None = None, status: str | None = None) -> list[dict]:
        params: dict[str, str] = {}
        if workflow_id:
            params["workflow_id"] = workflow_id
        if status:
            params["status"] = status
        return self._get("/approvals", params=params)

    def apply_approval(self, wid: str, approval_id: str) -> dict:
        return self._post(f"/workflows/{wid}/apply", {"approval_id": approval_id})

    def auto_apply(self, wid: str, body: dict) -> dict:
        """PUT /workflows/{wid}/config — only allowed dims, respects max_shift."""
        r = self._c.put(f"/workflows/{wid}/config", json=body)
        if r.status_code == 200:
            return r.json()
        log.warning("auto_apply %s → %d %s", wid, r.status_code, r.text[:200])
        return {"error": r.status_code, "detail": r.text}

    # --------------------------------------------------------------- LLM/valid
    def suggest_fixes(self, workflow_id: str, problem: str) -> dict:
        return self._post("/llm/suggest-fixes", {"workflow_id": workflow_id, "problem": problem})

    def run_validation(self, workflow_id: str, prompt_version: str, n: int = 100) -> dict:
        return self._post("/validation/runs", {"workflow_id": workflow_id, "prompt_version": prompt_version, "n": n})

    # ---------------------------------------------------------------- internals
    def _get(self, path: str, params: dict | None = None) -> Any:
        for attempt in range(3):
            try:
                r = self._c.get(path, params=params)
                r.raise_for_status()
                return r.json()
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 402:
                    log.error("OUT OF CREDITS — stopping credit-spending calls")
                    raise
                if e.response.status_code >= 500 and attempt < 2:
                    time.sleep(1.0 * (attempt + 1))
                    continue
                raise
        raise RuntimeError(f"GET {path} failed after retries")

    def _post(self, path: str, body: dict) -> Any:
        for attempt in range(3):
            try:
                r = self._c.post(path, json=body)
                r.raise_for_status()
                return r.json()
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 402:
                    log.error("OUT OF CREDITS")
                    raise
                if e.response.status_code == 409:
                    # finished / stale_base_version — caller must handle
                    return e.response.json()
                if e.response.status_code >= 500 and attempt < 2:
                    time.sleep(1.0 * (attempt + 1))
                    continue
                raise
        raise RuntimeError(f"POST {path} failed after retries")
