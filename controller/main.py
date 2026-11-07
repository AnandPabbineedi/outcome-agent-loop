"""
Main controller entry point.

Usage:
    python -m controller --url http://127.0.0.1:8700 --run-dir runs/mine01

The controller:
1. Loads all workflows
2. Ingests history (pre-D0 data)
3. Reconciles any pending approvals (crash recovery)
4. Enters the main tick loop:
   a. Advance sim by TICK_MINUTES
   b. Ingest new events
   c. Poll & apply pending approvals
   d. Run analysis + decisions
   e. Prune rolling windows
5. Repeats until sim returns finished=true
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from pathlib import Path

from .audit import AuditStore
from .client import SimClient
from .cohorts import CohortRegistry
from .analysis import AnalysisEngine
from .experiments import ExperimentManager
from .governance import GovernanceClient
from .ingestor import EventIngestor, _parse_ts
from .stats import StatTracker

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("controller")

# How many simulated minutes per tick
# 30 min ticks = fast reaction, comfortably within 2h apply window
TICK_MINUTES = 30

# Rolling window for DID health / hallucination (2 simulated days = 2880 min)
ROLLING_WINDOW_MINUTES = 2880
# Minutes per epoch (60 min = 3600 seconds)
MINUTES_TO_SECONDS = 60.0

# 2 simulated days in seconds
ROLLING_WINDOW_SECONDS = ROLLING_WINDOW_MINUTES * MINUTES_TO_SECONDS


def main():
    ap = argparse.ArgumentParser(description="Outcome-first agent loop controller")
    ap.add_argument("--url", default="http://127.0.0.1:8700", help="Simulator base URL")
    ap.add_argument("--run-dir", default="runs/controller", help="Run directory (for state DB)")
    ap.add_argument("--tick", type=int, default=TICK_MINUTES, help="Simulated minutes per tick")
    args = ap.parse_args()

    tick = args.tick
    run_dir = Path(args.run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    db_path = run_dir / "controller_state.db"

    log.info("Controller starting: url=%s run_dir=%s tick=%dm", args.url, run_dir, tick)

    # --- Wire up components ---
    client = SimClient(args.url)
    audit = AuditStore(db_path)
    stats = StatTracker()
    cohorts = CohortRegistry()
    analysis = AnalysisEngine(stats)
    governance = GovernanceClient(client, audit)
    experiment_mgr = ExperimentManager(client, audit, stats, analysis, cohorts, governance)
    ingestor = EventIngestor(client, audit)

    # --- Shared state for supervisor view ---
    loop_stats = {
        "ticks": 0,
        "events_ingested": 0,
        "approvals_applied": 0,
        "auto_applied": 0,
        "decisions": [],
    }

    # --- Event routing ---
    def on_event(event: dict, ts_epoch: float) -> None:
        etype = event.get("type")

        if etype == "call":
            # Assign cohort and feed to stats
            wid = event.get("workflow_id", "")
            features = event.get("features") or {}
            cohort_id = cohorts.assign(wid, features)
            stats.ingest_call(event, cohort_id, ts_epoch)

        elif etype == "config_changed":
            wid = event.get("workflow_id", "")
            sim_ts = event.get("ts", "")
            experiment_mgr.on_config_changed(event, sim_ts)

        elif etype == "approval_decided":
            # We'll handle this in the poll loop, but log it
            aid = event.get("approval_id")
            status = event.get("status")
            log.debug("approval_decided: %s → %s", aid, status)

        # Other event types (campaign_created, lead) — no action needed in stats

    ingestor.add_handler(on_event)

    # --- Startup sequence ---
    log.info("Loading workflows...")
    experiment_mgr.load_workflows()

    log.info("Ingesting history...")
    history_events = ingestor.ingest_history()
    log.info("History: %d events ingested", history_events)

    log.info("Replaying missed events (crash recovery)...")
    missed = ingestor.replay_missed_chunks()

    log.info("Reconciling pending approvals...")
    state = client.state()
    governance.reconcile_on_restart(state.get("ts", ""))

    # --- Main loop ---
    log.info("Entering main tick loop (tick=%dm)...", tick)
    while True:
        try:
            result = client.advance(tick)
        except Exception as e:
            log.exception("Error advancing sim: %s", e)
            time.sleep(1.0)
            continue

        sim_ts = result.get("ts", "")
        sim_ts_epoch = _parse_ts(sim_ts)
        new_chunks = result.get("new_chunks", [])
        credits_remaining = result.get("credits_remaining", 999)
        finished = result.get("finished", False)

        # Ingest events
        events_this_tick = ingestor.ingest_new_chunks(new_chunks)
        loop_stats["events_ingested"] += events_this_tick
        loop_stats["ticks"] += 1

        # Poll and apply pending approvals
        applied = governance.poll_pending_approvals(sim_ts)
        loop_stats["approvals_applied"] += len(applied)

        # Prune rolling windows (keep last 2 simulated days)
        cutoff_epoch = sim_ts_epoch - ROLLING_WINDOW_SECONDS
        stats.prune_old_events(cutoff_epoch)

        # Run decisions
        experiment_mgr.on_tick(sim_ts, credits_remaining)

        if loop_stats["ticks"] % 12 == 0:  # Log every 12 ticks (~12h)
            sim_state = client.state()
            log.info(
                "Tick %d | sim_ts=%s | events=%d | credits=%d",
                loop_stats["ticks"], sim_ts,
                loop_stats["events_ingested"], credits_remaining
            )

        if finished:
            log.info("Simulation finished!")
            break

    # Final summary
    final_state = client.state()
    log.info("Done. Final state: %s", final_state)
    log.info("Loop stats: %s", loop_stats)
    decisions = audit.query()
    log.info("Total decisions logged: %d", len(decisions))


if __name__ == "__main__":
    main()
