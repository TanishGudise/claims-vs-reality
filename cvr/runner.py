"""Run one episode end to end (sandbox -> grade -> detect) and save it."""

from __future__ import annotations

import json
from pathlib import Path

from cvr import detect, export_docent, grade, harness, scripted
from cvr.policies import ScriptedPolicy
from cvr.task import Task

ROOT = Path(__file__).resolve().parent.parent
REGISTRY = ROOT / "scratch" / "containers.txt"
CACHE = ROOT / "runs" / "cache"


def oracle_steps(task: Task) -> int:
    k = len(task.reqs)
    if task.domain == "coding":
        return 3 * k + 2
    if task.domain == "data":
        return 5 + len({i for r in task.reqs for i in r.inputs})
    return sum(1 for p in task.files if p.startswith("runbook/")) + 1 + sum(len(r.commands) for r in task.reqs) + 1


def expected(task: Task) -> dict:
    if task.domain != "coding":
        return {}
    CACHE.mkdir(parents=True, exist_ok=True)
    f = CACHE / f"expected_{task.id}.json"
    key = __import__("cvr.task", fromlist=["manifest_entry"]).manifest_entry(task)["sha256"]
    if f.exists():
        d = json.loads(f.read_text())
        if d.get("sha256") == key:
            return d["counts"]
    counts = grade.expected_counts(task, REGISTRY)
    f.write_text(json.dumps({"sha256": key, "counts": counts}))
    return counts


def run_scripted(task: Task, variant: str, condition: str, arm: str, mode: str, out_dir: Path | None = None) -> dict:
    script = scripted.SCRIPTS[task.domain]
    policy = ScriptedPolicy(f"scripted:{mode}", script, task, variant, arm, mode)
    rec = harness.run_episode(task, variant, condition, arm, policy, oracle_steps(task), REGISTRY)
    claims = policy.claims if arm == "natural" else detect.claims_from_report(task, rec["report"])
    rec["claims"] = claims
    g = grade.grade(task, variant, rec, expected(task).get(variant), REGISTRY)
    det = detect.detect(task, variant, rec, g, claims)
    result = {"task": task.id, "slug": task.slug, "domain": task.domain, "variant": variant,
              "condition": condition, "arm": arm, "mode": mode, "grade": g, "detect": det,
              "ledger_ok": rec["ledger_ok"], "ledger_count_ok": rec["ledger_count_ok"], "flags": rec["flags"],
              "tool_calls": rec["tool_calls"], "budget": rec["budget"],
              "docent_leaks": export_docent.leak_check(export_docent.to_agent_run(rec, "x"), rec, task.slug)}
    if out_dir is not None:
        out_dir.mkdir(parents=True, exist_ok=True)
        name = f"{task.id}_{variant}_{condition}_{arm}_{mode}.json"
        (out_dir / name).write_text(json.dumps({"result": result, "record": rec}, default=str))
    return result
