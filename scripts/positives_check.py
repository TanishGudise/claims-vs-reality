"""Positives check (Amendment 11b): human claims vs extractor on 9 judged exact positives + 9 negatives. $0.

usage: python scripts/positives_check.py --labels labels/human_labels_positives_n18.csv [--key labels/queue_pos_key.json]
Run only after the labeler has finished all 18 (n = 18).
"""

import argparse
import csv
import json
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from human_agreement import TYPES, kappa, wilson  # noqa: E402

from cvr import detect, extractor  # noqa: E402
from cvr.task import load_tasks  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--labels", required=True)
    ap.add_argument("--key", default="labels/queue_pos_key.json")
    a = ap.parse_args()
    key = json.loads(Path(a.key).read_text())
    queue = {it["episode"]: it for it in json.loads(Path("labels/queue_pos.json").read_text())}
    human = {r["episode"]: r for r in csv.DictReader(open(a.labels))}
    if set(human) != set(key):
        sys.exit(f"labels cover {len(human)} of {len(key)} items; run only when all 18 are labeled")
    mech = json.loads(Path("labels/mechanisms.json").read_text())
    tasks = {t.id: t for t in load_tasks()}
    fields = defaultdict(list)
    print(f"Positives check, n = {len(human)} (Amendment 11b)\n")
    print("item | role | mechanism | exact (extractor claims) | human claims -> labels | reproduced")
    for oid in sorted(key):
        ep, role = key[oid]["episode"], key[oid]["role"]
        r = human[oid]
        d = json.loads(Path(f"runs/main/episodes/{ep}.json").read_text())
        res, rec = d["result"], d["record"]
        t = tasks[res["task"]]
        ex = json.loads(Path(f"runs/main/claims/{ep}.json").read_text())["output"]
        h_req = json.loads(r["requirements_json"] or "{}")
        h_act = json.loads(r["actions_json"] or "[]")
        hc = {"overall": r["overall"] or "unclear", "requirements": h_req, "actions": h_act, "disclosure": r["disclosure"]}
        dh = detect.detect(t, res["variant"], rec, res["grade"], hc)
        de = detect.detect(t, res["variant"], rec, res["grade"], extractor.to_claims(ex))
        lh = [k for k in "abcd" if dh[k]] + [x.split(":")[0] for x in dh["details"].get("a", [])]
        le = [k for k in "abcd" if de[k]] + [x.split(":")[0] for x in de["details"].get("a", [])]
        m = mech.get(ep, "adjudicated_honest" if ep.endswith("f9dc8093_I_F_natural") else "-")
        claim_free = all(x.startswith("fabricated_observation") for x in de["details"].get("a", [])) and de["a"]
        rep = (dh["any"] == de["any"]) if role == "positive" else (not dh["any"])
        print(f"{oid} | {role} | {m}{' (claim-independent F5)' if claim_free else ''} | {le} | {lh} | {'yes' if rep else 'NO'}")
        it = queue[oid]
        if r["overall"]:
            fields["overall"].append((oid, r["overall"], ex["overall"]))
        for rid in sorted(set(h_req) | set(ex["requirements"])):
            fields["item_status"].append((f"{oid}:{rid}", h_req.get(rid, "unmentioned"), ex["requirements"].get(rid, "unmentioned")))
        fields["disclosure"].append((oid, r["disclosure"], ex["disclosure"]))
        hs = {(x["type"], x["target"]): x["result"] for x in h_act}
        es = {(x["type"], x["target"]): x["result"] for x in ex["actions"]}
        for ty in TYPES:
            for tgt in [""] + list(it["targets"]):
                fields["action_present"].append((f"{oid}:{ty}:{tgt}", (ty, tgt) in hs, (ty, tgt) in es))
                if (ty, tgt) in hs and (ty, tgt) in es:
                    fields["action_result"].append((f"{oid}:{ty}:{tgt}", hs[(ty, tgt)], es[(ty, tgt)]))
    print("\nfield | units | agree | % [95% CI] | kappa  (n = 18)")
    for f in ("overall", "item_status", "action_present", "action_result", "disclosure"):
        rows = fields[f]
        k = sum(h == e for _, h, e in rows)
        lo, hi = wilson(k, len(rows))
        kp = kappa([(h, e) for _, h, e in rows])
        print(f"{f} | {len(rows)} | {k} | {k / max(1, len(rows)):.3f} [{lo:.3f},{hi:.3f}] | {'-' if kp is None else f'{kp:.2f}'}")
        for uid, h, e in rows:
            if h != e:
                print(f"   DISAGREE {f} {uid}: human={h} extractor={e}")


if __name__ == "__main__":
    main()
