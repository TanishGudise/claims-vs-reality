"""Run model episodes for the pilot or the main run under a hard spend cap. PAID unless --dry-run.

usage:
  python scripts/run_models.py --phase pilot --cap 8 [--workers 3]
  python scripts/run_models.py --phase main --cap <X> --rung <R> [--open-weight <model>] [--models m1,m2]
  python scripts/run_models.py --phase pilot --dry-run      # fake client, loopback-only, $0

Episodes are saved under runs/<phase>/episodes/ and skipped on rerun (resume). An episode
that the meter refuses mid-way is recorded in runs/<phase>/meter_stops.jsonl, not scored.
"""

import argparse
import concurrent.futures as cf
import json
import sys
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cvr import design, detect, export_docent, grade, harness  # noqa: E402
from cvr.meter import CapReached, Meter, require_frozen_manifest  # noqa: E402
from cvr.policies import AnthropicPolicy  # noqa: E402
from cvr.runner import REGISTRY, expected, oracle_steps  # noqa: E402
from cvr.task import load_tasks  # noqa: E402


def key(job) -> str:
    model, tid, v, c, arm = job
    return f"{model}__{tid}_{v}_{c}_{arm}"


class InfraFailure(RuntimeError):
    pass


def grade_and_save(job, rec: dict, out: Path) -> str:
    model, tid, v, c, arm = job
    task = TASKS[tid]
    if not (rec["ledger_ok"] and rec["ledger_count_ok"]):
        raise InfraFailure(f"ledger check failed (ok={rec['ledger_ok']}, count_ok={rec['ledger_count_ok']})")
    g = grade.grade(task, v, rec, expected(task).get(v), REGISTRY)
    claims = detect.claims_from_report(task, rec["report"]) if arm == "structured" else None
    res = {"task": tid, "slug": task.slug, "domain": task.domain, "model": model, "variant": v, "condition": c,
           "arm": arm, "grade": g, "ledger_ok": rec["ledger_ok"], "ledger_count_ok": rec["ledger_count_ok"],
           "flags": rec["flags"], "tool_calls": rec["tool_calls"], "budget": rec["budget"],
           "cost": round(sum(u.get("cost", 0) for u in rec["usage"]), 6),
           "docent_leaks": export_docent.leak_check(export_docent.to_agent_run(rec, "x"), rec, task.slug)}
    if claims is not None:
        res["detect"] = detect.detect(task, v, rec, g, claims)
    rec["claims"] = claims
    (out / f"{key(job)}.json").write_text(json.dumps({"result": res, "record": rec}, default=str))
    return f"{key(job)} calls={rec['tool_calls']} cost=${res['cost']:.4f} flags={rec['flags']}"


def run_job(job, meter, out: Path, client) -> str:
    """Raw record is saved before grading, so a grading crash never discards a paid episode."""
    model, tid, v, c, arm = job
    raw = out.parent / "raw" / f"{key(job)}.json"
    if raw.exists():  # paid already; only grading is left
        try:
            return grade_and_save(job, json.loads(raw.read_text()), out) + " (re-graded)"
        except Exception:
            with open(out.parent / "grading_failures.jsonl", "a") as f:  # reported separately from infra
                f.write(json.dumps({"job": key(job), "error": traceback.format_exc()[-600:]}) + "\n")
            raise
    task = TASKS[tid]
    policy = AnthropicPolicy(model, client=client, meter=meter)
    rec = harness.run_episode(task, v, c, arm, policy, oracle_steps(task), REGISTRY)
    raw.parent.mkdir(parents=True, exist_ok=True)
    if rec["ledger_ok"] and rec["ledger_count_ok"]:
        raw.write_text(json.dumps(rec, default=str))
    return grade_and_save(job, rec, out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--phase", choices=["pilot", "main"], required=True)
    ap.add_argument("--cap", type=float, default=0.0)
    ap.add_argument("--rung", type=int, default=0)
    ap.add_argument("--open-weight", default=None)
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--limit", type=int, default=0, help="run at most N jobs (probes)")
    ap.add_argument("--models", default="", help="comma-separated: run only these models' jobs")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--deadline", type=int, default=7200, help="seconds; the process exits itself after this")
    a = ap.parse_args()
    import os
    import subprocess
    import threading

    def _expire():
        print(f"DEADLINE {a.deadline}s reached; removing this checkout's containers and exiting", flush=True)
        mine = set(REGISTRY.read_text().split()) if REGISTRY.exists() else set()
        running = subprocess.run(["docker", "ps", "-q", "--no-trunc", "--filter", "label=cvr"],
                                 capture_output=True, text=True).stdout.split()
        for cid in running:
            if cid in mine:
                subprocess.run(["docker", "rm", "-f", cid], capture_output=True)
        os._exit(3)

    timer = threading.Timer(a.deadline, _expire)
    timer.daemon = True
    timer.start()
    out_root = Path("runs") / (a.phase + ("-dry" if a.dry_run else ""))
    out = out_root / "episodes"
    out.mkdir(parents=True, exist_ok=True)
    client = None
    if a.dry_run:
        from cvr import netguard
        from tests.fake_anthropic import FakeClient

        netguard.install()
        client = FakeClient()
        a.cap = a.cap or 1.0
    elif a.cap <= 0:
        sys.exit("refusing to run paid episodes without --cap")
    else:
        require_frozen_manifest()
        from cvr import netguard

        netguard.install(allow=("api.anthropic.com",))
        __import__('cvr.envfile', fromlist=['load_env']).load_env()
    jobs = design.pilot_jobs() if a.phase == "pilot" else design.main_jobs(a.rung, a.open_weight)
    if a.models:
        jobs = [j for j in jobs if j[0] in a.models.split(",")]
    fail_log = out_root / "failures.jsonl"
    attempts: dict[str, int] = {}
    if fail_log.exists():
        for line in fail_log.read_text().splitlines():
            k = json.loads(line)["job"]
            attempts[k] = attempts.get(k, 0) + 1
    # §13: an infrastructure failure is rerun once; after two failures the episode is excluded and counted
    todo = [j for j in jobs if not (out / f"{key(j)}.json").exists() and attempts.get(key(j), 0) < 2]
    if a.limit:
        todo = todo[: a.limit]
    spent_before = 0.0
    spend_log = out_root / "spend.jsonl"
    if spend_log.exists():
        lines = spend_log.read_text().splitlines()
        spent_before = json.loads(lines[-1])["spent"] if lines else 0.0
    meter = Meter(a.cap, spend_log, spent=spent_before)
    print(f"{a.phase}: {len(jobs)} jobs, {len(todo)} to run, cap ${a.cap:.2f}, spent so far ${spent_before:.4f}",
          flush=True)
    stopped = False

    def one(job):
        nonlocal stopped
        tries = attempts.get(key(job), 0)
        while tries < 2:  # §13: one rerun on an infrastructure failure, in the same invocation
            if stopped:
                return job, "skipped (cap)"
            try:
                return job, run_job(job, meter, out, client)
            except CapReached as e:
                stopped = True
                with open(out_root / "meter_stops.jsonl", "a") as f:
                    f.write(json.dumps({"job": key(job), "reason": str(e)}) + "\n")
                return job, f"METER STOP: {e}"
            except Exception:
                tries += 1
                err = traceback.format_exc()[-600:]
                with open(fail_log, "a") as f:
                    f.write(json.dumps({"job": key(job), "attempt": tries, "error": err}) + "\n")
        return job, "EXCLUDED after 2 infrastructure failures: " + err.strip().splitlines()[-1]

    t0 = time.time()
    with cf.ThreadPoolExecutor(a.workers) as ex:
        for job, msg in ex.map(one, todo):
            print(f"[{time.time() - t0:6.0f}s ${meter.spent:.4f}] {msg}", flush=True)
    print(json.dumps({"spent": round(meter.spent, 4), "cap": a.cap, "stopped_by_meter": stopped}))


TASKS = {t.id: t for t in load_tasks()}

if __name__ == "__main__":
    main()
