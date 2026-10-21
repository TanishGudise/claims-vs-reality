"""Docent reading pass (CP5, Amendment 11) through `behaviors`' local DocentReadingStep. PAID unless --dry-run.

Run with the behaviors venv: .venv-behaviors/bin/python -I scripts/run_readings.py --run runs/main --stage S --cap C
  --stage leak     $0: the judges' leak check on what the reader sees, for every episode in the reading set
  --stage read     Haiku reader on the reading set (612 natural-arm episodes + the structured-arm exact positives)
  --stage confirm  Sonnet checker: the 50-claim "supported" sample, then every unsupported/contradicted claim in
                   the 15 exact-positive episodes, then all other unsupported/contradicted claims (seeded order)
  --stage cluster  one Sonnet call grouping the confirmed unsupported/contradicted claims into mechanisms
--dry-run uses a fake model, writes to readings-dry/ and reading_spend-dry.jsonl, and allows no network.
Outputs: runs/<phase>/readings{-dry}/{haiku,check}/<episode>.json, clusters.json; spend in reading_spend{-dry}.jsonl.
"""

import argparse
import asyncio
import json
import os
import random
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cvr import reading  # noqa: E402
from cvr.meter import CapReached, Meter, prior_spend, require_frozen_manifest  # noqa: E402


def reading_set(run: Path) -> list[Path]:
    mech = json.loads(Path("labels/mechanisms.json").read_text())
    out = []
    for f in sorted((run / "episodes").glob("*.json")):
        if f.stem.endswith("_natural") or f.stem in mech:
            out.append(f)
    return out


def behaviors_run(f: Path, salt: str):
    from cvr.export_behaviors import to_behaviors_agent_run
    from cvr.export_docent import opaque_id

    return to_behaviors_agent_run(json.loads(f.read_text())["record"], opaque_id(f.stem, salt))


class Metered:
    """A behaviors ModelAPI that reserves each call's worst case on the meter and settles on usage."""

    def __init__(self, inner, model: str, meter: Meter, max_tokens: int):
        self.inner, self.model, self.meter, self.max_tokens = inner, model, meter, max_tokens

    async def generate(self, request):
        ticket = self.meter.reserve(self.model, "", [{"c": repr(request.context)}], [], self.max_tokens)
        try:
            resp = await self.inner.generate(request)
        except BaseException:
            self.meter.release(ticket)
            raise
        u = getattr(resp, "usage", None)
        if u is None:  # a transport failure returned as data: nothing billed that we can see
            self.meter.release(ticket)
            return resp
        self.meter.settle(ticket, self.model, {"input": u.input_tokens, "output": u.output_tokens,
                                               "cache_read": u.cache_read_input_tokens or 0,
                                               "cache_write": u.cache_write_input_tokens or 0})
        return resp


class Fake:
    """$0 stand-in that returns schema-valid JSON (dry runs)."""

    def __init__(self, kind: str):
        self.kind, self.n = kind, 0

    async def generate(self, request):
        from behaviors.types import ModelResponse, Usage
        from behaviors.types.content import ContentText
        from behaviors.types.message import AssistantMessage
        from behaviors.types.response import StopEndTurn

        self.n += 1
        if self.kind == "reader":
            body = {"claims": [{"claim": "ran the check", "status": "unsupported", "evidence": "none"},
                               {"claim": "file read", "status": "supported", "evidence": "a tool output"}],
                    "omissions": []}
        elif self.kind == "checker":
            text = repr(request.context)
            nums = sorted({int(x) for x in __import__("re").findall(r"\[claim (\d+)\]", text)}) or [1]
            body = {"verdicts": [{"number": k, "status": "unsupported", "evidence": "none"} for k in nums]}
        else:
            body = {"groups": [{"name": "fake", "description": "fake", "ids": []}]}
        return ModelResponse(id=f"fake-{self.n}", output=(AssistantMessage(content=(ContentText(text=json.dumps(body)),)),),
                             stop_reason=StopEndTurn(), usage=Usage(input_tokens=3000, cache_read_input_tokens=0,
                                                                     cache_write_input_tokens=0, output_tokens=400,
                                                                     reasoning_output_tokens=None))


def model_api(kind: str, model: str, meter: Meter, max_tokens: int, dry: bool):
    if dry:
        inner = Fake(kind)
    else:
        from behaviors.implementations.models.anthropic import AnthropicMessagesAPI

        inner = AnthropicMessagesAPI(model)
    return Metered(inner, model, meter, max_tokens)


def step(kind: str, api, extra_text=None):
    from behaviors.implementations.docent.reading_step import DocentReadingStep, DocentReadingStepConfig
    from behaviors.types import ReasoningParams, SamplingParams

    if kind == "reader":
        cfg = DocentReadingStepConfig(prompt_template_segments=tuple(reading.READER_PROMPT),
                                      output_schema=reading.READER_SCHEMA, input_param_name="run",
                                      max_output_attempts=reading.MAX_OUTPUT_ATTEMPTS)
        sampling = SamplingParams(max_tokens=reading.READER_MAX_TOKENS, reasoning=ReasoningParams(enabled=False))
    else:
        cfg = DocentReadingStepConfig(prompt_template_segments=tuple(reading.CHECKER_PROMPT),
                                      output_schema=reading.CHECKER_SCHEMA, input_param_name="run",
                                      extra_text_params=extra_text, max_output_attempts=reading.MAX_OUTPUT_ATTEMPTS)
        sampling = SamplingParams(max_tokens=reading.CHECKER_MAX_TOKENS,
                                  reasoning=ReasoningParams(enabled=True, effort="medium"))
    return DocentReadingStep(config=cfg, model_api=api, sampling=sampling)


async def run_pool(items, fn, concurrency):
    sem = asyncio.Semaphore(concurrency)
    state = {"stop": False}

    async def one(it):
        async with sem:
            if state["stop"]:
                return
            try:
                await fn(it)
            except Exception as e:  # CapReached may arrive wrapped
                cap = e if isinstance(e, CapReached) else next(
                    (x for x in getattr(e, "exceptions", ()) if isinstance(x, CapReached)), None)
                if cap is None:
                    raise
                if not state["stop"]:
                    print("METER STOP:", cap, flush=True)
                state["stop"] = True

    await asyncio.gather(*(one(i) for i in items))
    return state["stop"]


def claims_of(path: Path) -> list[dict]:
    d = json.loads(path.read_text())
    return (d.get("parsed") or {}).get("claims", []) if "parsed" in d else []


async def main_async(a):
    run = Path(a.run)
    salt = (run / "salt.txt").read_text().strip()
    sfx = "-dry" if a.dry_run else ""
    root = run / f"readings{sfx}"
    meter = Meter(a.cap, run / f"reading_spend{sfx}.jsonl", spent=prior_spend(run / f"reading_spend{sfx}.jsonl"))
    print(f"reading spend so far ${meter.spent:.4f} of cap ${a.cap:.2f}", flush=True)
    files = reading_set(run)

    if a.stage == "leak":
        from behaviors.implementations.docent.conversions import to_docent_agent_run

        from cvr.export_docent import leak_check

        bad = 0
        for f in files:
            d = json.loads(f.read_text())
            probs = leak_check(to_docent_agent_run(behaviors_run(f, salt)), d["record"], d["result"]["slug"])
            if probs:
                bad += 1
                print("LEAK?", f.stem, probs)
        print(json.dumps({"checked": len(files), "flagged": bad}))
        return

    if a.stage == "read":
        out = root / "haiku"
        out.mkdir(parents=True, exist_ok=True)
        api = model_api("reader", reading.READER_MODEL, meter, reading.READER_MAX_TOKENS, a.dry_run)
        st = step("reader", api)

        async def one(f):
            if (out / f.name).exists():
                return
            res = await st.read(behaviors_run(f, salt))
            if not hasattr(res, "parsed_response"):
                print("READ FAILURE", f.stem, res, flush=True)
                with open(run / f"reading_failures{sfx}.jsonl", "a") as fl:
                    fl.write(json.dumps({"episode": f.stem, "stage": "read", "failure": repr(res)}) + "\n")
                return
            (out / f.name).write_text(json.dumps({"parsed": res.parsed_response}, default=str))

        stopped = await run_pool(files, one, a.concurrency)
        print(json.dumps({"stage": "read", "done": len(list(out.glob("*.json"))), "of": len(files),
                          "spent": round(meter.spent, 4), "stopped_by_meter": stopped}))
        return

    if a.stage == "confirm":
        hdir, out = root / "haiku", root / "check"
        out.mkdir(parents=True, exist_ok=True)
        mech = json.loads(Path("labels/mechanisms.json").read_text())
        supported, flagged = [], {}
        for f in sorted(hdir.glob("*.json")):
            for i, c in enumerate(claims_of(f)):
                cid = f"{f.stem}#{i}"
                if c["status"] == "supported" and f.stem.endswith("_natural"):
                    supported.append(cid)
                elif c["status"] in ("unsupported", "contradicted"):
                    flagged.setdefault(f.stem, []).append(cid)
        sample = sorted(random.Random(reading.SAMPLE_SEED).sample(sorted(supported), min(reading.SUPPORTED_SAMPLE,
                                                                                       len(supported))))
        Path(root / "supported_sample.json").write_text(json.dumps(sample, indent=1))
        by_ep: dict[str, list[str]] = {}
        for cid in sample:
            by_ep.setdefault(cid.split("#")[0], []).append(cid)
        tier_a = sorted({cid.split("#")[0] for cid in sample})
        tier_b = sorted(e for e in flagged if e in mech)
        rest = sorted(e for e in flagged if e not in mech and e not in tier_a)
        random.Random(reading.ORDER_SEED).shuffle(rest)
        order = tier_b + [e for e in tier_a if e not in tier_b] + rest
        for e, cids in flagged.items():
            by_ep.setdefault(e, [])
            by_ep[e] = sorted(set(by_ep[e]) | set(cids), key=lambda c: int(c.split("#")[1]))
        api = model_api("checker", reading.CHECKER_MODEL, meter, reading.CHECKER_MAX_TOKENS, a.dry_run)
        files_by = {f.stem: f for f in files}

        async def one(e):
            if (out / f"{e}.json").exists():
                return
            cl = claims_of(hdir / f"{e}.json")
            ids = by_ep[e]
            text = "\n".join(f"[claim {k + 1}] {cl[int(c.split('#')[1])]['claim']}" for k, c in enumerate(ids))
            res = await step("checker", api, {"claims": text}).read(behaviors_run(files_by[e], salt))
            if not hasattr(res, "parsed_response"):
                print("CHECK FAILURE", e, res, flush=True)
                with open(run / f"reading_failures{sfx}.jsonl", "a") as fl:
                    fl.write(json.dumps({"episode": e, "stage": "confirm", "failure": repr(res)}) + "\n")
                return
            verdicts = {v["number"]: v for v in res.parsed_response.get("verdicts", [])}
            rows = [{"id": c, "claim": cl[int(c.split("#")[1])]["claim"], "haiku": cl[int(c.split("#")[1])]["status"],
                     "sonnet": verdicts.get(k + 1, {}).get("status"), "evidence": verdicts.get(k + 1, {}).get("evidence")}
                    for k, c in enumerate(ids)]
            (out / f"{e}.json").write_text(json.dumps({"rows": rows}, indent=1))

        stopped = await run_pool(order, one, a.concurrency)
        print(json.dumps({"stage": "confirm", "episodes_done": len(list(out.glob("*.json"))), "of": len(order),
                          "tiers": [len(tier_b), len(tier_a), len(rest)], "spent": round(meter.spent, 4),
                          "stopped_by_meter": stopped}))
        return

    if a.stage == "cluster":
        rows = [r for f in sorted((root / "check").glob("*.json")) for r in json.loads(f.read_text())["rows"]
                if r["haiku"] != "supported" and r["sonnet"] in ("unsupported", "contradicted")]
        # short notes (claim <= 160 chars, evidence <= 80) keep the call's worst case inside the cap
        listing = "\n".join(f"{r['id']}: {r['claim'][:160]} (evidence: {(r['evidence'] or '')[:80]})" for r in rows)
        from behaviors.types import ModelRequest, ReasoningParams, SamplingParams
        from behaviors.types.content import ContentText
        from behaviors.types.message import UserMessage

        api = model_api("cluster", reading.CHECKER_MODEL, meter, reading.CLUSTER_MAX_TOKENS, a.dry_run)
        req = ModelRequest(context=(UserMessage(content=(ContentText(text=reading.CLUSTER_INSTRUCTIONS + "\n\n" + listing),)),),
                           tools=(), tool_choice=None, parallel_tool_calls=None,
                           sampling=SamplingParams(max_tokens=reading.CLUSTER_MAX_TOKENS,
                                                   reasoning=ReasoningParams(enabled=True, effort="medium")))
        resp = await api.generate(req)
        text = "".join(getattr(b, "text", "") for m in getattr(resp, "output", ()) for b in m.content)
        (root / "clusters.json").write_text(json.dumps({"n_claims": len(rows), "raw": text}, indent=1))
        print(json.dumps({"stage": "cluster", "claims": len(rows), "spent": round(meter.spent, 4)}))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", required=True)
    ap.add_argument("--stage", choices=["leak", "read", "confirm", "cluster"], required=True)
    ap.add_argument("--cap", type=float, default=0.0)
    ap.add_argument("--concurrency", type=int, default=3)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--deadline", type=int, default=7200)
    a = ap.parse_args()

    def _expire():
        print(f"DEADLINE {a.deadline}s reached; exiting (rerun resumes)", flush=True)
        os._exit(3)

    t = threading.Timer(a.deadline, _expire)
    t.daemon = True
    t.start()
    from cvr import netguard

    if a.dry_run or a.stage == "leak":
        netguard.install()
        a.cap = a.cap or 1.0
    else:
        if a.cap <= 0:
            sys.exit("refusing paid readings without --cap")
        require_frozen_manifest()
        netguard.install(allow=("api.anthropic.com",))
        __import__("cvr.envfile", fromlist=["load_env"]).load_env()
    asyncio.run(main_async(a))


if __name__ == "__main__":
    main()
