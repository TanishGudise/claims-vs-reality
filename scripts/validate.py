"""Validation gates 2-3 (preregistration §10) with scripted agents, at $0.

usage: python scripts/validate.py [--pilot-only] [--tasks id,id] [--workers N] [--quick]
Exit 0 only if every expectation holds. Results go to runs/validation/<stamp>/.
"""

import argparse
import concurrent.futures as cf
import json
import sys
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cvr import scripted  # noqa: E402
from cvr.runner import run_scripted  # noqa: E402
from cvr.task import CONDITIONS, VARIANTS, load_tasks  # noqa: E402

ARMS = ("natural", "structured")


def jobs(tasks, quick):
    for t in tasks:
        for v in VARIANTS:
            for c in CONDITIONS:
                for arm in ARMS:
                    if quick and (c != "N" or arm != "natural"):
                        continue
                    yield (t, v, c, arm, "honest")
            for mode in scripted.LIARS + scripted.UNUSUAL:
                if not scripted.applies(mode, t, v):
                    continue
                for arm in ARMS:
                    if quick and arm != "natural":
                        continue
                    conds = ("F",) if mode in ("flaky_quit", "u_retry") else ("N",)
                    for c in conds:
                        yield (t, v, c, arm, mode)


def check(t, v, c, arm, mode, r) -> list[str]:
    d, g = r["detect"], r["grade"]["reqs"]
    errs = []
    if not (r["ledger_ok"] and r["ledger_count_ok"]):
        errs.append("ledger")
    if r["docent_leaks"]:
        errs.append(f"docent export leaks: {r['docent_leaks']}")
    if mode in ("honest",) or mode in scripted.UNUSUAL:
        if d["any"]:
            errs.append(f"honest flagged: a={d['a']} b={d['b']} c={d['c']} d={d['d']} "
                        f"{ {k: d['details'].get(k) for k in ('a', 'b', 'c')} } tamper={d['tampering_events']}")
        if mode == "u_disclose" and not d["d_plus"]:
            errs.append("disclosed tampering not counted as d+")
        if mode == "honest" and c == "N":
            imp = t.impossible(v)
            for rid, x in g.items():
                want = rid not in imp
                if x["pass"] != want:
                    errs.append(f"grader: {rid} pass={x['pass']} want {want}")
    else:
        want = scripted.LIAR_EXPECT[mode]
        if not d[want]:
            errs.append(f"liar {mode} not caught as ({want}): {d['details'] if 'details' in d else d}")
    return errs


def rescore(dirs) -> int:
    """Re-check saved gate episodes with the current detectors (grades are reused; no Docker)."""
    from cvr import detect

    tasks = {t.id: t for t in load_tasks()}
    n = bad = 0
    for d_ in dirs:
        for f in sorted(Path(d_).glob("episodes/*.json")):
            d = json.loads(f.read_text())
            r, rec = d["result"], d["record"]
            t = tasks[r["task"]]
            claims = rec.get("claims") if r["arm"] == "natural" else detect.claims_from_report(t, rec["report"])
            r["detect"] = detect.detect(t, r["variant"], rec, r["grade"], claims)
            errs = check(t, r["variant"], r["condition"], r["arm"], r["mode"], r)
            n += 1
            if errs:
                bad += 1
                print(f"FAIL {f.name}: {errs[0][:300]}", flush=True)
    print(json.dumps({"rescored": n, "failures": bad}))
    return 1 if bad else 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pilot-only", action="store_true")
    ap.add_argument("--tasks", default="")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--quick", action="store_true")
    ap.add_argument("--skip-dir", default="", help="resume: skip jobs whose saved episode in this run passed")
    ap.add_argument("--deadline", type=int, default=0, help="seconds; the process exits itself after this")
    ap.add_argument("--rescore", nargs="+", default=[], help="offline: re-run the detectors on saved episodes")
    ap.add_argument("--modes", default="", help="comma-separated scripted modes to run (default: all)")
    a = ap.parse_args()
    if a.rescore:
        sys.exit(rescore(a.rescore))
    if a.deadline:  # in-process watchdog (an external alarm did not stop this process once)
        import os
        import threading

        def _expire():
            print(f"DEADLINE {a.deadline}s reached; removing this run's containers and exiting", flush=True)
            import subprocess

            from cvr.runner import REGISTRY
            mine = set(REGISTRY.read_text().split()) if REGISTRY.exists() else set()
            running = subprocess.run(["docker", "ps", "-q", "--no-trunc", "--filter", "label=cvr"],
                                     capture_output=True, text=True).stdout.split()
            for cid in running:
                if cid in mine:  # exact ids this checkout started
                    subprocess.run(["docker", "rm", "-f", cid], capture_output=True)
            os._exit(3)

        timer = threading.Timer(a.deadline, _expire)
        timer.daemon = True  # must not keep a finished run alive
        timer.start()
    tasks = load_tasks()
    if a.pilot_only:
        tasks = [t for t in tasks if t.pilot]
    if a.tasks:
        tasks = [t for t in tasks if t.id in a.tasks.split(",") or t.slug in a.tasks.split(",")]
    out = Path("runs/validation") / time.strftime("%Y%m%d-%H%M%S")
    out.mkdir(parents=True, exist_ok=True)
    todo = list(jobs(tasks, a.quick))
    if a.modes:
        todo = [j for j in todo if j[4] in a.modes.split(",")]
    if a.skip_dir:
        prev = Path(a.skip_dir)
        done_ok = set()
        for f in (prev / "episodes").glob("*.json"):
            d = json.loads(f.read_text())["result"]
            t = next((t for t in tasks if t.id == d["task"]), None)
            if t is None:
                continue
            if not check(t, d["variant"], d["condition"], d["arm"], d["mode"], d):
                done_ok.add((d["task"], d["variant"], d["condition"], d["arm"], d["mode"]))
        todo = [j for j in todo if (j[0].id, j[1], j[2], j[3], j[4]) not in done_ok]
        print(f"resume: {len(done_ok)} passed episodes reused from {prev}", flush=True)
    print(f"{len(todo)} episodes, {a.workers} workers -> {out}", flush=True)
    fails, done, t0 = [], 0, time.time()

    def one(job):
        t, v, c, arm, mode = job
        for attempt in range(2):  # one rerun on infrastructure failure (§13)
            try:
                r = run_scripted(t, v, c, arm, mode, out / "episodes")
                return job, r, check(t, v, c, arm, mode, r)
            except Exception:
                err = traceback.format_exc()
        return job, None, [f"infra: {err[-400:]}"]

    with cf.ThreadPoolExecutor(a.workers) as ex:
        for job, r, errs in ex.map(one, todo):
            done += 1
            t, v, c, arm, mode = job
            if errs:
                fails.append({"task": t.slug, "variant": v, "condition": c, "arm": arm, "mode": mode, "errors": errs})
                print(f"FAIL {t.slug} {v} {c} {arm} {mode}: {errs[0][:300]}", flush=True)
            if done % 25 == 0:
                print(f"  {done}/{len(todo)} ({time.time() - t0:.0f}s)", flush=True)
    summary = {"episodes": len(todo), "failures": len(fails), "seconds": round(time.time() - t0)}
    (out / "summary.json").write_text(json.dumps({"summary": summary, "failures": fails}, indent=1))
    print(json.dumps(summary))
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
