"""Extract claims from natural-arm final messages with the frozen extractor. PAID.

usage: python scripts/extract_claims.py --run runs/<phase> --cap <usd> [--poll 60] [--chunk N] [--deadline S]
--chunk submits at most N messages per invocation, so each batch's worst case is checked
against the cap after the earlier batches' real spend. --deadline stops polling after S
seconds and records the batch id in runs/<phase>/extract_pending.json; the next run collects
it before submitting anything new.
Submits one Message Batch for every natural-arm episode that has a final message and no
claims file yet, reserves the batch's worst case against the cap first, then writes
runs/<phase>/claims/<episode>.json. Re-running resumes.
"""

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cvr import extractor  # noqa: E402
from cvr.meter import PRICES, prior_spend, require_frozen_manifest  # noqa: E402
from cvr.task import load_tasks  # noqa: E402

BATCH_DISCOUNT = 0.5


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", required=True)
    ap.add_argument("--cap", type=float, required=True)
    ap.add_argument("--poll", type=int, default=60)
    ap.add_argument("--chunk", type=int, default=0)
    ap.add_argument("--deadline", type=int, default=7200)
    a = ap.parse_args()
    run = Path(a.run)
    out = run / "claims"
    out.mkdir(parents=True, exist_ok=True)
    tasks = {t.id: t for t in load_tasks()}
    pending = run / "extract_pending.json"
    if pending.exists():  # a batch from an earlier invocation that hit its deadline
        client = _client()
        _wait_and_collect(client, json.loads(pending.read_text())["batch"], run, out, a)
        pending.unlink()
        return
    items = []
    for f in sorted((run / "episodes").glob("*.json")):
        if (out / f.name).exists() and "output" in json.loads((out / f.name).read_text()):
            continue  # extractor errors are retried on resume
        d = json.loads(f.read_text())
        r, rec = d["result"], d["record"]
        if r["arm"] != "natural" or not rec.get("final_text"):
            continue
        t = tasks[r["task"]]
        # the base task text only: the extractor never sees the condition (§4.1)
        items.append((f.stem, extractor.request_params(t, rec["final_text"], t.prompt)))
    if not items:
        print("nothing to extract")
        return
    if a.chunk:
        items = items[: a.chunk]
    p = PRICES[extractor.MODEL]
    worst = sum(len(json.dumps(params)) * max(p["input"], p["cache_write"]) / 1e6
                + params["max_tokens"] * p["output"] / 1e6 for _, params in items) * BATCH_DISCOUNT
    already = prior_spend(run / "extract_cost.jsonl")
    print(f"{len(items)} messages; worst-case ${worst:.4f}; spent so far ${already:.4f}; cap ${a.cap:.2f}")
    if already + worst > a.cap:
        sys.exit("spent + worst case exceeds the cap; not submitting")
    client = _client()
    batch = extractor.submit(client, items)
    print("batch", batch.id, flush=True)
    pending.write_text(json.dumps({"batch": batch.id}))
    _wait_and_collect(client, batch.id, run, out, a)
    pending.unlink()


def _client():
    require_frozen_manifest()
    from cvr import netguard

    netguard.install(allow=("api.anthropic.com",))
    __import__('cvr.envfile', fromlist=['load_env']).load_env()
    import anthropic

    return anthropic.Anthropic()


def _wait_and_collect(client, batch_id: str, run: Path, out: Path, a):
    p = PRICES[extractor.MODEL]
    t0 = time.time()
    while client.messages.batches.retrieve(batch_id).processing_status != "ended":
        if time.time() - t0 > a.deadline:
            sys.exit(f"DEADLINE: batch {batch_id} still running; recorded in extract_pending.json, rerun to collect")
        time.sleep(a.poll)
    results = extractor.collect(client, batch_id)
    cost = 0.0
    with open(run / "extract_cost.jsonl", "a") as log:
        for cid, res in results.items():
            (out / f"{cid}.json").write_text(json.dumps(res))
            u = res.get("usage") or {}
            c = BATCH_DISCOUNT * (u.get("input_tokens", 0) * p["input"] + u.get("output_tokens", 0) * p["output"]) / 1e6
            cost += c
            log.write(json.dumps({"episode": cid, "cost": c}) + "\n")
    errors = sum(1 for r in results.values() if "error" in r)
    print(json.dumps({"extracted": len(results) - errors, "errors": errors, "cost": round(cost, 4)}))


if __name__ == "__main__":
    main()
