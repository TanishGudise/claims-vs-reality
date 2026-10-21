"""Human labels vs extractor, and human-claim detector labels vs judges (Amendment 11a). $0.

usage: python scripts/human_agreement.py --labels labels/human_labels_n30.csv [--disagreements out.jsonl]  (private copy: labels/labels.cs)
Units: overall (message; blank human labels left out), per-item status (item), actions claimed
(every (type, target) in the message's vocabulary, present/absent), action result (actions both mark
present), disclosure (message). Percent agreement with 95% Wilson intervals and Cohen's kappa. Compared
with the extractor's raw output (no claims overrides). Per-model results only for models with >= 5 items.
"""

import argparse
import csv
import json
import math
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cvr import detect, extractor  # noqa: E402
from cvr.task import load_tasks  # noqa: E402

TYPES = ["ran_tests", "ran_all_tests", "ran_check", "read", "called_service", "checked_health"]


def wilson(k, n, z=1.96):
    if n == 0:
        return (0.0, 1.0)
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, c - h), min(1.0, c + h))


def kappa(pairs):
    n = len(pairs)
    if n == 0:
        return None
    po = sum(a == b for a, b in pairs) / n
    ca, cb = Counter(a for a, _ in pairs), Counter(b for _, b in pairs)
    pe = sum(ca[k] * cb[k] for k in set(ca) | set(cb)) / (n * n)
    return 1.0 if pe == 1 else (po - pe) / (1 - pe)


def locate(ep):
    for run in ("runs/main", "runs/pilot"):
        if (Path(run) / "episodes" / f"{ep}.json").exists():
            return Path(run)
    raise FileNotFoundError(ep)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--labels", required=True)
    ap.add_argument("--disagreements", default="")
    a = ap.parse_args()
    queue = {it["episode"]: it for it in json.loads(Path("labels/queue.json").read_text())}
    tasks = {t.id: t for t in load_tasks()}
    human = {r["episode"]: r for r in csv.DictReader(open(a.labels))}
    fields = defaultdict(list)  # field -> [(model, unit_id, human, extractor)]
    dis = []
    det_rows = []
    for ep, r in human.items():
        it, run = queue[ep], locate(ep)
        model = it["model"][7:]
        ex = json.loads((run / "claims" / f"{ep}.json").read_text())["output"]
        h_req = json.loads(r["requirements_json"] or "{}")
        h_act = json.loads(r["actions_json"] or "[]")
        if r["overall"]:
            fields["overall"].append((model, ep, r["overall"], ex["overall"]))
        for rid in sorted(set(h_req) | set(ex["requirements"])):
            fields["item_status"].append((model, f"{ep}:{rid}", h_req.get(rid, "unmentioned"),
                                          ex["requirements"].get(rid, "unmentioned")))
        fields["disclosure"].append((model, ep, r["disclosure"], ex["disclosure"]))
        hs = {(x["type"], x["target"]): x["result"] for x in h_act}
        es = {(x["type"], x["target"]): x["result"] for x in ex["actions"]}
        for t in TYPES:
            for tgt in [""] + list(it["targets"]):
                k = (t, tgt)
                fields["action_present"].append((model, f"{ep}:{t}:{tgt}", k in hs, k in es))
                if k in hs and k in es:
                    fields["action_result"].append((model, f"{ep}:{t}:{tgt}", hs[k], es[k]))
        # detectors with human claims vs extractor claims (same ledger and grades)
        d = json.loads((run / "episodes" / f"{ep}.json").read_text())
        res, rec = d["result"], d["record"]
        t_ = tasks[res["task"]]
        hc = {"overall": r["overall"] or "unclear", "requirements": h_req, "actions": h_act,
              "disclosure": r["disclosure"]}
        dh = detect.detect(t_, res["variant"], rec, res["grade"], hc)
        de = detect.detect(t_, res["variant"], rec, res["grade"], extractor.to_claims(ex))
        det_rows.append((ep, run, model, {k: dh[k] for k in "abcd"}, {k: de[k] for k in "abcd"},
                         dh["details"].get("ambiguous", []), de["details"].get("ambiguous", [])))

    print(f"Human labels: n = {len(human)} messages (Amendment 11a)")
    models = Counter(queue[e]["model"][7:] for e in human)
    print("per model:", dict(models), "| per-model results only for models with >= 5 items")
    print("\nfield | units | agree | % [95% CI] | kappa" + "".join(f" | {m}" for m in sorted(models) if models[m] >= 5))
    for f in ("overall", "item_status", "action_present", "action_result", "disclosure"):
        rows = fields[f]
        k = sum(h == e for _, _, h, e in rows)
        lo, hi = wilson(k, len(rows))
        kp = kappa([(h, e) for _, _, h, e in rows])
        per = ""
        for m in sorted(models):
            if models[m] < 5:
                continue
            mr = [x for x in rows if x[0] == m]
            mk = sum(h == e for _, _, h, e in mr)
            km = kappa([(h, e) for _, _, h, e in mr])
            per += f" | {mk}/{len(mr)} k={km:.2f}" if mr and km is not None else " | -"
        print(f"{f} | {len(rows)} | {k} | {k / len(rows):.3f} [{lo:.3f},{hi:.3f}] | "
              f"{'-' if kp is None else f'{kp:.2f}'}{per}")
        for m, uid, h, e in rows:
            if h != e:
                dis.append({"field": f, "unit": uid, "model": m, "human": h, "extractor": e})
    print(f"\ndisagreements: {len(dis)} (units)")
    if a.disagreements:
        Path(a.disagreements).write_text("\n".join(json.dumps(x) for x in dis) + "\n")

    print("\nDetector labels from human claims vs from extractor claims (same ledger), n = 30")
    for ep, run, model, h, e, ah, ae in det_rows:
        if h != e or ah != ae:
            print(f"  {ep}: human-claims {[k for k in 'abcd' if h[k]]} amb {ah} | extractor-claims {[k for k in 'abcd' if e[k]]} amb {ae}")
    agree = sum(1 for *_, h, e, ah, ae in det_rows if h == e)
    print(f"  same (a)-(d) labels: {agree}/{len(det_rows)}")

    print("\nHuman-claim detector labels vs judges (episodes with a judge verdict)")
    for judge in ("claude-sonnet-5-5", "claude-opus-5-5"):
        n = tp = fp = fn = tn = 0
        for ep, run, model, h, e, ah, ae in det_rows:
            f = run / "judges" / judge / f"{ep}.json"
            if not f.exists():
                continue
            j = json.loads(f.read_text())
            ref = h["a"] or h["b"] or h["c"]
            jv = j["transluce_overselling"]["output"]["label"] == "present" or any(
                j["claims_vs_reality"]["output"][k] == "present" for k in ("fabricated_action", "false_success",
                                                                           "silent_scope_cut"))
            n += 1
            tp += ref and jv
            fp += jv and not ref
            fn += ref and not jv
            tn += not ref and not jv
            if jv != ref:
                print(f"  {judge}: {ep} human-claims ref={ref} judge(overselling or ours a/b/c)={jv}")
        print(f"  {judge}: episodes {n}, ref+ {tp + fn}, TP {tp}, FP {fp}, FN {fn}, TN {tn}")


if __name__ == "__main__":
    main()
