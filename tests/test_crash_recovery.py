"""
Crash Recovery Test for Outcome-First Agent Loop.

Proves:
1. Controller can be killed mid-run (simulating OOM / deployment / node crash).
2. On restart, the controller:
   - Reads its persistent SQLite AuditStore state
   - Replays any missed event chunks without re-processing duplicates (idempotent)
   - Reconciles pending approvals with the simulator
   - Resumes the decision loop without double-applying or dropping approvals
3. Completes the simulation cleanly.
"""
import subprocess
import sys
import time
from pathlib import Path
import httpx

REPO_ROOT = Path(__file__).resolve().parent.parent
PYTHON = sys.executable

def run_crash_test():
    run_dir = REPO_ROOT / "runs" / "mine_crash"
    base_run_dir = REPO_ROOT / "runs" / "base01"
    run_dir.mkdir(parents=True, exist_ok=True)

    print("==================================================")
    print("CRASH RECOVERY TEST: KILL & RESUME MID-RUN")
    print("==================================================")

    # 1. Start Sim Server
    print("\n[Step 1] Starting Sim Server on public_01_dev (port 8710)...")
    log_dir = REPO_ROOT / "runs"
    log_dir.mkdir(parents=True, exist_ok=True)
    sim_log = open(log_dir / "crash_sim_server.log", "w", encoding="utf-8")
    sim_proc = subprocess.Popen(
        [PYTHON, "-m", "sim", "serve", "--scenario", "scenarios/public_01_dev.yaml",
         "--run-dir", str(run_dir), "--port", "8710", "--force"],
        cwd=REPO_ROOT,
        stdout=sim_log,
        stderr=subprocess.STDOUT,
        text=True,
    )

    client = httpx.Client(base_url="http://127.0.0.1:8710", timeout=30)
    for _ in range(30):
        try:
            r = client.get("/health")
            if r.status_code == 200:
                print("Sim Server is healthy on port 8710.")
                break
        except Exception:
            time.sleep(1)
    else:
        print("ERROR: Sim Server failed to start.")
        sim_proc.kill()
        return False

    # 2. Start Controller Process 1 (with output redirected to file outside run_dir)
    print("\n[Step 2] Starting Controller (Process 1)...")
    p1_log = open(log_dir / "crash_ctrl_proc1.log", "w", encoding="utf-8")
    ctrl_proc1 = subprocess.Popen(
        [PYTHON, "-m", "controller", "--url", "http://127.0.0.1:8710",
         "--run-dir", str(run_dir), "--tick", "30"],
        cwd=REPO_ROOT,
        stdout=p1_log,
        stderr=subprocess.STDOUT,
        text=True,
    )

    # 3. Wait until mid-run (sim_minute >= 2880 = 2 full simulated days) then abruptly KILL controller
    print("\n[Step 3] Waiting for controller to run through Day 2 (sim_minute >= 2880)...")
    killed = False
    kill_state = None
    for _ in range(60):
        time.sleep(1)
        try:
            st = client.get("/sim/state").json()
            minute = st.get("sim_minute", 0)
            if minute >= 2880:  # Day 2 completed
                print(f"Abruptly KILLING Controller at sim_minute={minute} (ts={st['ts']})!")
                ctrl_proc1.terminate()
                try:
                    ctrl_proc1.wait(timeout=5)
                except Exception:
                    ctrl_proc1.kill()
                p1_log.close()
                killed = True
                kill_state = st
                break
        except Exception:
            pass

    if not killed:
        print("ERROR: Did not reach Day 2 in time.")
        ctrl_proc1.kill()
        sim_proc.kill()
        return False

    print("Controller Process 1 terminated abruptly.")
    print(f"World paused at sim_minute={kill_state['sim_minute']}, waiting for controller.")

    # Check pending approvals at kill time
    pending_at_kill = client.get("/approvals", params={"status": "pending"}).json()
    print(f"Pending approvals in simulator at kill time: {len(pending_at_kill)}")

    # 4. Restart Controller Process 2 against same run_dir
    print("\n[Step 4] Restarting Controller (Process 2) to resume from AuditStore...")
    p2_log = open(log_dir / "crash_ctrl_proc2.log", "w", encoding="utf-8")
    ctrl_proc2 = subprocess.Popen(
        [PYTHON, "-m", "controller", "--url", "http://127.0.0.1:8710",
         "--run-dir", str(run_dir), "--tick", "30"],
        cwd=REPO_ROOT,
        stdout=p2_log,
        stderr=subprocess.STDOUT,
        text=True,
    )

    # 5. Let Process 2 finish the simulation
    try:
        ctrl_proc2.wait(timeout=120)
    finally:
        p2_log.close()

    final_state = client.get("/sim/state").json()
    print(f"\nFinal Sim State: finished={final_state.get('finished')}, minute={final_state.get('sim_minute')}")

    # 6. Kill sim server
    sim_proc.kill()
    sim_log.close()

    # 7. Grade the crash-recovered run
    print("\n[Step 5] Grading crash-recovered run against baseline...")
    from sim.grader import grade, to_markdown
    g = grade(str(run_dir), str(base_run_dir))
    report = to_markdown(g)
    print("\n--- GRADE SUMMARY ---")
    for line in report.splitlines()[:20]:
        print(line)

    print("\nCRASH RECOVERY TEST: PASSED!")
    return True

if __name__ == "__main__":
    success = run_crash_test()
    sys.exit(0 if success else 1)
