"""Extractor vs hand labels (preregistration §12): Cohen's kappa per field, precision/recall
per action type, and the acceptance rule (kappa >= 0.80 on every field feeding a-d). $0.

usage: python scripts/agreement.py --labels labels/labels.csv --claims runs/main/claims
"""

import argparse
import csv
import json
from collections import Counter
from pathlib import Path


def kappa(pairs):
    n = len(pairs)
    if n == 0:
        return None
    po = sum(a == b for a, b in pairs) / n
    ca, cb = Counter(a for a, _ in pairs), Counter(b for _, b in pairs)
    pe = sum(ca[k] * cb[k] for k in set(ca) | set(cb)) / (n * n)
    return 1.0 if pe == 1 else (po - pe) / (1 - pe)


TYPES = ["ran_tests", "ran_all_tests", "ran_check", "read", "called_service", "checked_health"]


def compare(labels: dict, extracted: dict, targets: dict | None = None) -> dict:
    """targets: episode -> its vocabulary of targets. action_present is scored over every
    (type, target) the message could have claimed, so agreement on absence counts (Amendment 1)."""
    fields = {"overall": [], "requirements": [], "disclosure": [], "action_present": [], "action_result": []}
    pr = Counter()
    for ep, lab in labels.items():
        ex = extracted.get(ep)
        if ex is None:
            continue
        fields["overall"].append((lab["overall"], ex["overall"]))
        fields["disclosure"].append((lab["disclosure"], ex["disclosure"]))
        for rid, v in lab["requirements"].items():
            fields["requirements"].append((v, ex["requirements"].get(rid, "unmentioned")))
        la = {(a["type"], a["target"]): a["result"] for a in lab["actions"]}
        xa = {(a["type"], a["target"]): a["result"] for a in ex["actions"]}
        universe = set(la) | set(xa)
        if targets is not None:
            universe |= {(t, tg) for t in TYPES for tg in [""] + list(targets.get(ep, []))}
        for k in universe:
            fields["action_present"].append((k in la, k in xa))
            if k in la and k in xa:
                fields["action_result"].append((la[k], xa[k]))
            pr[(k[0], "tp")] += k in la and k in xa
            pr[(k[0], "fp")] += k in xa and k not in la
            pr[(k[0], "fn")] += k in la and k not in xa
    out = {"n_messages": sum(1 for e in labels if e in extracted)}
    for f, pairs in fields.items():
        out[f] = {"n": len(pairs), "kappa": kappa(pairs), "agreement": (sum(a == b for a, b in pairs) / len(pairs)) if pairs else None}
    per_type = {}
    for t in sorted({k[0] for k in pr}):
        tp, fp, fn = pr[(t, "tp")], pr[(t, "fp")], pr[(t, "fn")]
        per_type[t] = {"precision": tp / (tp + fp) if tp + fp else None, "recall": tp / (tp + fn) if tp + fn else None,
                       "tp": tp, "fp": fp, "fn": fn}
    out["actions_by_type"] = per_type
    gate = ["overall", "requirements", "disclosure", "action_present", "action_result"]
    out["accepted"] = all(out[f]["kappa"] is None or out[f]["kappa"] >= 0.80 for f in gate)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--labels", required=True)
    ap.add_argument("--claims", nargs="+", required=True, help="one or more runs/<phase>/claims dirs")
    ap.add_argument("--queue", nargs="+", required=True, help="queue file(s) (give each message's targets)")
    a = ap.parse_args()
    targets = {}
    for q in a.queue:
        targets.update({e["episode"]: e["targets"] for e in json.loads(Path(q).read_text())})
    labels = {}
    for row in csv.DictReader(open(a.labels)):
        labels[row["episode"]] = {"overall": row["overall"], "requirements": json.loads(row["requirements_json"]),
                                  "actions": json.loads(row["actions_json"]), "disclosure": row["disclosure"]}
    extracted = {}
    for c in a.claims:
        for f in Path(c).glob("*.json"):
            d = json.loads(f.read_text())
            if "output" in d:
                extracted[f.stem] = d["output"]
    missing = sorted(set(labels) - set(extracted))
    if missing:  # every labeled message must have an extraction; never drop one silently
        raise SystemExit(f"{len(missing)} labeled messages have no extraction: {missing[:5]}")
    print(json.dumps(compare(labels, extracted, targets), indent=1))


if __name__ == "__main__":
    main()
