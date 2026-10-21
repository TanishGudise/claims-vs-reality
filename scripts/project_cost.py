"""Project main-run cost per ladder rung from measured pilot costs only (§8.3). $0.

usage: python scripts/project_cost.py --pilot runs/pilot --cap 80 [--open-weight-cost-per-episode X]
Uses mean measured cost per episode by (model, arm), mean extractor cost per message, and mean
judge cost per (judge model, episode, three rubrics). Never reads pilot outcomes.
"""

import argparse
import json
import statistics
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cvr import design  # noqa: E402


def means(pilot: Path) -> dict:
    ep = defaultdict(list)
    for f in (pilot / "episodes").glob("*.json"):
        r = json.loads(f.read_text())["result"]
        ep[(r["model"], r["arm"])].append(r["cost"])
    out = {"episode": {f"{m}|{a}": statistics.mean(v) for (m, a), v in ep.items()},
           "episode_max": {f"{m}|{a}": max(v) for (m, a), v in ep.items()}}
    ex = [json.loads(l)["cost"] for l in (pilot / "extract_cost.jsonl").read_text().splitlines()] \
        if (pilot / "extract_cost.jsonl").exists() else []
    out["extract_per_message"] = statistics.mean(ex) if ex else None
    jc = defaultdict(list)
    if (pilot / "judge_spend.jsonl").exists():
        for line in (pilot / "judge_spend.jsonl").read_text().splitlines():
            d = json.loads(line)
            jc[d["model"]].append(d["cost"])
    out["judge_per_call"] = {m: statistics.mean(v) for m, v in jc.items()}
    return out


def project(m: dict, rung: int, ow_cost: float | None) -> dict:
    jobs = design.main_jobs(rung, "open-weight" if ow_cost is not None else None)
    agent = 0.0
    natural = defaultdict(int)
    for model, _, _, _, arm in jobs:
        if model == "open-weight":
            agent += ow_cost
        else:
            agent += m["episode"][f"{model}|{arm}"]
        if arm == "natural":
            natural[model] += 1
    n_nat = sum(natural.values())
    extract = n_nat * (m["extract_per_message"] or 0)
    per_model = design.judged_per_model(rung)
    judged = sum(n if per_model is None else min(n, per_model) for n in natural.values())
    sonnet_call = m["judge_per_call"].get("claude-sonnet-5-5", 0)
    opus_call = m["judge_per_call"].get("claude-opus-5-5", 0)
    judge = judged * 3 * sonnet_call + (120 + 60) * 3 * opus_call  # Opus subset at its maximum size
    return {"rung": rung, "episodes": len(jobs), "judged": judged, "agent": round(agent, 2),
            "extract": round(extract, 2), "judge": round(judge, 2), "total": round(agent + extract + judge, 2)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pilot", required=True)
    ap.add_argument("--cap", type=float, required=True)
    ap.add_argument("--open-weight-cost-per-episode", type=float, default=None)
    a = ap.parse_args()
    m = means(Path(a.pilot))
    rows = [project(m, r, a.open_weight_cost_per_episode) for r in range(0, 7)]
    fit = next((r for r in rows if r["total"] <= a.cap), None)
    print(json.dumps({"measured": m, "projection": rows, "chosen_rung": fit["rung"] if fit else None}, indent=1))


if __name__ == "__main__":
    main()
