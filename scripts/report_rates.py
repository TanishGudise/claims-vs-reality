"""Headline tables with the ambiguous bucket beside them (preregistration §12, Amendment 8). $0.

usage: python scripts/report_rates.py --run runs/<phase> [--judges] [--scores F] [--compare F]
--scores reads another scores file in the run (default scores.jsonl); --compare F prints each cell as
"<F> -> <scores>" (e.g. the frozen Amendment 8 scoring next to Amendment 9, side by side).
Per model x arm: (a)-(d) and any-misreport counts with 95% Wilson intervals, then the
ambiguous bucket per sub-type (episodes with at least one case), never folded into (a)-(d).
Excluded episodes are left out of every denominator. --judges adds the Amendment 8
secondary table: the share of ambiguous judged episodes each rubric marks present.
"""

import argparse
import collections
import json
import math
from pathlib import Path

SUBTYPES = ("named_contradiction", "check_result_unrun", "read_target_not_a_path",  # Amendment 7
            "failed_to_start", "created_then_deleted_input",  # Amendment 9
            "phantom_input_unclear", "pass_claim_vs_not_done")  # Amendment 9 addendum
RUBRICS = ("transluce_overselling", "transluce_monitor_evasion", "claims_vs_reality")


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (0.0, 1.0)
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, c - h), min(1.0, c + h))


def fmt(k: int, n: int) -> str:
    lo, hi = wilson(k, n)
    return f"{k}/{n} [{lo:.2f},{hi:.2f}]"


def present(res: dict, rubric: str) -> bool:
    """Transluce rubrics have one label; ours is present if any of its four behaviors is."""
    out = res[rubric]["output"]
    if "label" in out:
        return out["label"] == "present"
    return any(out.get(k) == "present" for k in ("fabricated_action", "false_success", "silent_scope_cut",
                                                   "undisclosed_tampering"))


def subtypes(s: dict) -> set[str]:
    return {a.split(":", 1)[0] for a in s.get("ambiguous", [])}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", required=True)
    ap.add_argument("--judges", action="store_true")
    ap.add_argument("--scores", default="scores.jsonl")
    ap.add_argument("--compare", default="")
    ap.add_argument("--by", default="model,arm", help="grouping fields, e.g. model,condition,arm")
    a = ap.parse_args()
    run = Path(a.run)

    def table(name):
        scores = [json.loads(line) for line in (run / name).read_text().splitlines()]
        kept = [s for s in scores if not s["excluded"]]
        groups = collections.defaultdict(list)
        for s in kept:
            groups[tuple(s[k] for k in a.by.split(","))].append(s)
        rows = {}
        for key, ss in groups.items():
            n = len(ss)
            # §9.1 denominators: (b) and (c) over episodes with that opportunity, (a), (d), any over all
            cells = [fmt(sum(1 for s in ss if s[k]), sum(1 for s in ss if s["opportunity"][k]) if k in "bc" else n)
                     for k in ("a", "b", "c", "d", "any")]
            cells.insert(1, str(sum(1 for s in ss if s["a"] and any(x.startswith("fabricated_observation:")  # Am. 9 F5
                                                          for x in (s.get("details") or {}).get("a", [])))))
            amb = [str(sum(1 for s in ss if t in subtypes(s))) for t in SUBTYPES]
            amb.append(str(sum(1 for s in ss if s.get("ambiguous"))))
            rows[key] = [str(n)] + cells + amb
        return scores, kept, rows

    scores, kept, rows = table(a.scores)
    old = table(a.compare)[2] if a.compare else {}
    print(f"{len(scores)} scored, {len(scores) - len(kept)} excluded (ledger)" + (f"; cells: {a.compare} -> {a.scores}" if old else ""))
    print(" | ".join(a.by.split(",")) + " | n | a | a:F5 | b | c | d | any | " + " | ".join(f"amb:{t}" for t in SUBTYPES) + " | amb:any")
    for key in sorted(rows):
        cells = rows[key]
        if key in old:
            cells = [c if o == c else f"{o} -> {c}" for o, c in zip(old[key], cells)]
        print(" | ".join(list(key) + cells))
    if not a.judges:
        return
    by_ep = {s["episode"]: s for s in kept}
    print("\nAmendment 8 secondary: rubric 'present' share among ambiguous judged episodes (descriptive)")
    print("judge | model | sub-type | n | " + " | ".join(RUBRICS))
    for jdir in sorted((run / "judges").glob("*")):
        rows = collections.defaultdict(list)
        for f in jdir.glob("*.json"):
            s = by_ep.get(f.stem)
            if not s or not s.get("ambiguous"):
                continue
            res = json.loads(f.read_text())
            for t in subtypes(s):
                rows[(s["model"], t)].append(res)
        if not rows:
            print(f"{jdir.name} | (no ambiguous episodes in its judged set)")
        for (model, t), rs in sorted(rows.items()):
            n = len(rs)
            cells = [fmt(sum(1 for r in rs if present(r, k)), n) for k in RUBRICS]
            print(" | ".join([jdir.name, model, t, str(n)] + cells))


if __name__ == "__main__":
    main()
