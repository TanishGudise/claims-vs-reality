"""Run the Docent judges on the judged set (and the Opus subset). PAID unless --dry-run.

usage: python scripts/run_judges.py --run runs/<phase> --cap <usd> [--per-model N] [--opus-subset] [--dry-run]
--per-model: judged episodes per model (seeded 20261018); omit to judge every natural-arm episode.
--opus-subset: judge the Opus subset (needs runs/<phase>/scores.jsonl for pipeline labels).
Writes runs/<phase>/judges/<model>/<episode>.json; reruns resume.
"""

import argparse
import asyncio
import json
import random
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cvr.export_docent import opaque_id, to_agent_run  # noqa: E402
from cvr.judge import MAIN, SUBSET, judge_episode, metered_service, opus_subset  # noqa: E402
from cvr.meter import CapReached, Meter, prior_spend, require_frozen_manifest  # noqa: E402

JUDGED_SEED = 20261018


def judged_set(run: Path, per_model: int | None) -> list[Path]:
    by_model = defaultdict(list)
    for f in sorted((run / "episodes").glob("*.json")):
        d = json.loads(f.read_text())
        if d["result"]["arm"] == "natural" and d["record"].get("final_text"):
            by_model[d["result"].get("model", d["record"]["policy"])].append(f)
    rng = random.Random(JUDGED_SEED)
    out = []
    for m in sorted(by_model):
        fs = by_model[m]
        out += fs if per_model is None or len(fs) <= per_model else sorted(rng.sample(fs, per_model))
    return out


def _cap_reached(e: BaseException):
    if isinstance(e, CapReached):
        return e
    for sub in getattr(e, "exceptions", ()) or ():
        found = _cap_reached(sub)
        if found:
            return found
    return None


async def main_async(a):
    run = Path(a.run)
    salt = (run / "salt.txt").read_text().strip() if (run / "salt.txt").exists() else ""
    if not salt:
        import secrets

        salt = secrets.token_hex(16)
        (run / "salt.txt").write_text(salt)
    files = judged_set(run, a.per_model)
    model = MAIN
    if a.opus_subset:
        if not a.scores:  # never read scores.jsonl here: a concurrent re-score rewrites it (2026-10-19 race)
            sys.exit("--opus-subset needs --scores <immutable pre-adjudication scores file>")
        scores = [json.loads(l) for l in (run / a.scores).read_text().splitlines()]
        in_set = {f.stem for f in files}
        ids = opus_subset([{"id": s["episode"], "positive": bool(s["a"] or s["b"] or s["c"] or s["d"]
                                                                   or s.get("d_plus"))}
                           for s in scores if s["episode"] in in_set],
                          n_negatives=a.opus_negatives, max_positives=a.opus_max_positives)
        files = [f for f in files if f.stem in set(ids)]
        model = SUBSET
        if a.list_subset:
            print(json.dumps(sorted(ids)))
            return
    suffix = "-dry" if a.dry_run else ""  # dry runs never touch the real verdicts or spend log
    out = run / f"judges{suffix}" / model.model_name
    out.mkdir(parents=True, exist_ok=True)
    spend_log = run / f"judge_spend{suffix}.jsonl"
    meter = Meter(a.cap, spend_log, spent=prior_spend(spend_log))
    print(f"judge spend so far ${meter.spent:.4f} of cap ${a.cap:.2f}", flush=True)
    svc = metered_service(meter, fake=a.dry_run)
    done = 0
    stop = False
    sem = asyncio.Semaphore(max(1, a.concurrency))

    async def one(f):
        nonlocal done, stop
        async with sem:
            if stop or (out / f.name).exists():
                return
            rec = json.loads(f.read_text())["record"]
            try:
                res = await judge_episode(to_agent_run(rec, opaque_id(f.stem, salt)), model, svc)
            except Exception as e:  # the rubrics run in a task group, so CapReached may arrive wrapped
                cap = _cap_reached(e)
                if cap is None:
                    raise
                if not stop:
                    print("METER STOP:", cap, flush=True)
                stop = True
                return
            failed = [k for k, v in res.items() if "FAILURE" in v["type"]]
            if failed:  # never record a failed judgment as done; it is retried on resume and counted
                with open(run / f"judge_failures{suffix}.jsonl", "a") as fl:
                    fl.write(json.dumps({"episode": f.stem, "model": model.model_name, "rubrics": failed,
                                         "meta": {k: res[k]["meta"] for k in failed}}, default=str) + "\n")
                print(f"JUDGE FAILURE {f.stem}: {failed}", flush=True)
                return
            (out / f.name).write_text(json.dumps(res, default=str))
            done += 1
            print(f"{done}/{len(files)} ${meter.spent:.4f} {f.stem}", flush=True)

    await asyncio.gather(*(one(f) for f in files))
    print(json.dumps({"judged": done, "spent": round(meter.spent, 4), "model": model.model_name}))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", required=True)
    ap.add_argument("--cap", type=float, required=True)
    ap.add_argument("--per-model", type=int, default=None)
    ap.add_argument("--opus-subset", action="store_true")
    ap.add_argument("--opus-negatives", type=int, default=60)
    ap.add_argument("--opus-max-positives", type=int, default=120)
    ap.add_argument("--scores", default="", help="--opus-subset: pipeline (pre-adjudication) scores file in the run")
    ap.add_argument("--list-subset", action="store_true", help="print the subset ids and exit ($0)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--concurrency", type=int, default=1, help="episodes judged at once (keep modest)")
    ap.add_argument("--deadline", type=int, default=7200, help="seconds; the process exits itself after this")
    a = ap.parse_args()
    import os
    import threading

    def _expire():  # verdicts are saved per episode, so an exit loses at most the episode in flight
        print(f"DEADLINE {a.deadline}s reached; exiting (rerun resumes)", flush=True)
        os._exit(3)

    timer = threading.Timer(a.deadline, _expire)
    timer.daemon = True
    timer.start()
    if a.dry_run:
        from cvr import netguard

        netguard.install()
    else:
        require_frozen_manifest()
        netguard_install = __import__("cvr.netguard", fromlist=["install"]).install
        netguard_install(allow=("api.anthropic.com",))
        __import__('cvr.envfile', fromlist=['load_env']).load_env()
    asyncio.run(main_async(a))


if __name__ == "__main__":
    main()
