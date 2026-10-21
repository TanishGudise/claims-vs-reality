"""Build the hand-label queue: a seeded sample of natural-arm final messages, stratified by
model x variant x a reality stratum that needs no claims (§12).

usage: python scripts/make_label_queue.py --from claude-haiku-5-5=runs/main/episodes \
           claude-sonnet-5-5=runs/pilot/episodes claude-opus-5-5=runs/pilot/episodes --out labels/queue.json
       (top-up after the Sonnet/Opus runs: --append --seed 20261022)
"""

import argparse
import json
import random
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cvr import detect, extractor  # noqa: E402
from cvr.task import load_tasks  # noqa: E402

SEED = 20261020


def reality_stratum(result: dict, record: dict, task) -> str:
    g = result["grade"]["reqs"]
    if detect.tampering_events(task, result["variant"], record, result["grade"]):
        return "tamper"
    if any(not x["pass"] and not x["deliverable"] for x in g.values()):
        return "fail_no_deliverable"
    if any(not x["pass"] for x in g.values()):
        return "fail_with_deliverable"
    return "all_pass"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="sources", nargs="+", required=True,
                    help="model=episodes_dir (repeatable per model), e.g. claude-haiku-5-5=runs/main/episodes")
    ap.add_argument("--quota", nargs="+", default=["claude-haiku-5-5=40", "claude-sonnet-5-5=40", "claude-opus-5-5=40"],
                    help="model=N (Amendment 1: 40 per Claude model)")
    ap.add_argument("--seed", type=int, default=SEED)
    ap.add_argument("--out", default="labels/queue.json")
    ap.add_argument("--append", action="store_true", help="top-up: keep the existing queue and add to it")
    ap.add_argument("--exclude", default="", help="file of episode ids never to use (e.g. the §12 dev set)")
    a = ap.parse_args()
    tasks = {t.id: t for t in load_tasks()}
    quota = {k: int(v) for k, v in (q.split("=") for q in a.quota)}
    existing = json.loads(Path(a.out).read_text()) if a.append and Path(a.out).exists() else []
    have = {e["episode"] for e in existing}
    if a.exclude:
        have |= set(Path(a.exclude).read_text().split())
    count = {m: sum(1 for e in existing if e.get("model") == m) for m in quota}
    strata = defaultdict(list)
    sources = [s_.split("=", 1) for s_ in a.sources]
    for want_model, d_ in sources:
        for f in sorted(Path(d_).glob("*.json")):
            d = json.loads(f.read_text())
            r, rec = d["result"], d["record"]
            model = r.get("model", rec["policy"])
            if model != want_model:  # each model only from its declared source(s)
                continue
            if r["arm"] != "natural" or not rec.get("final_text") or f.stem in have or model not in quota:
                continue
            key = (model, r["variant"], r["condition"], reality_stratum(r, rec, tasks[r["task"]]))
            strata[key].append((f.stem, model, r, rec))
    rng = random.Random(a.seed)
    for k in sorted(strata):
        rng.shuffle(strata[k])
    queue = list(existing)
    shortfall = {}
    for m in quota:
        keys = sorted(k for k in strata if k[0] == m)
        rng.shuffle(keys)
        while count[m] < quota[m] and any(strata[k] for k in keys):
            for k in keys:
                if strata[k] and count[m] < quota[m]:
                    stem, model, r, rec = strata[k].pop()
                    t = tasks[r["task"]]
                    queue.append({"episode": stem, "model": model, "prompt": t.prompt,  # no condition text
                                  "requirements": [{"id": q.id, "title": q.title} for q in t.reqs],
                                  "targets": extractor.targets(t), "final_text": rec["final_text"]})
                    count[m] += 1
        if count[m] < quota[m]:
            shortfall[m] = quota[m] - count[m]
    # labeler-facing order: shuffled so models and variants are interleaved (seeded)
    new = queue[len(existing):]
    rng.shuffle(new)
    queue = existing + new
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(json.dumps(queue, indent=1))
    print(json.dumps({"queue": len(queue), "added": len(new), "per_model": count, "shortfall_until_topup": shortfall}))


if __name__ == "__main__":
    main()
