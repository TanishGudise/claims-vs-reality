"""Run the pre-registered Docent judges locally (docent==0.1.88 build_judge, our API key).

Main judge: claude-sonnet-5-5 at medium effort on every rubric x judged episode.
Subset judge: claude-opus-5-5 at medium effort on the Opus subset (Amendment 0).
This module makes API calls only when `judge_episode` is awaited inside an approved run.
"""

from __future__ import annotations

import random

from docent._llm_util.providers.preference_types import ModelOption
from docent.judges.types import Rubric

from cvr.rubrics import RUBRIC_NAMES, load_rubric

MAIN = ModelOption(provider="anthropic", model_name="claude-sonnet-5-5", reasoning_effort="medium")
SUBSET = ModelOption(provider="anthropic", model_name="claude-opus-5-5", reasoning_effort="medium")


def rubric_for(name: str, model: ModelOption) -> Rubric:
    return load_rubric(name).model_copy(update={"judge_model": model})


async def judge_episode(agent_run, model: ModelOption, llm_svc=None) -> dict:
    from docent._llm_util.llm_svc import BaseLLMService
    from docent.judges.impl import build_judge

    svc = llm_svc or BaseLLMService()
    out = {}
    for name in RUBRIC_NAMES:
        judge = build_judge(rubric_for(name, model), svc)
        res = await judge(agent_run)
        out[name] = {"type": str(res.result_type), "output": res.output, "meta": res.result_metadata}
    return out


def opus_subset(episodes: list[dict], n_negatives: int = 60, max_positives: int = 120, seed: int = 20261019) -> list[str]:
    """All reference-positive episodes in the judged set (capped by a seeded sample) plus a seeded
    sample of reference-negatives. `episodes`: [{"id", "positive"}], with pipeline labels taken
    before adjudication. Fixed in Amendment 0."""
    rng = random.Random(seed)
    pos = sorted(e["id"] for e in episodes if e["positive"])
    neg = sorted(e["id"] for e in episodes if not e["positive"])
    if len(pos) > max_positives:
        pos = sorted(rng.sample(pos, max_positives))
    return pos + sorted(rng.sample(neg, min(n_negatives, len(neg))))


def metered_service(meter, fake: bool = False):
    """A Docent BaseLLMService that reserves each judge call's worst case on `meter` first
    (Docent may retry an unparseable answer up to MAX_VALIDATION_ATTEMPTS times, each billed)
    and settles on the usage Docent reports. fake=True returns canned answers ($0, for tests)."""
    from docent._llm_util.data_models.llm_output import LLMCompletion, LLMOutput, UsageMetrics
    from docent._llm_util.llm_svc import MAX_VALIDATION_ATTEMPTS, BaseLLMService

    class Metered(BaseLLMService):
        async def get_completions(self, *, inputs, model_options, max_new_tokens=1024, **kw):
            model = model_options[0].model_name
            tickets = []
            try:
                for inp in inputs:
                    msgs = [m if isinstance(m, dict) else m.model_dump() for m in (inp() if callable(inp) else inp)]
                    for k in range(MAX_VALIDATION_ATTEMPTS):  # retry k may include k earlier outputs
                        tickets.append(meter.reserve(model, "", msgs, [], max_new_tokens,
                                                     extra_input_tokens=k * max_new_tokens))
                if fake:
                    outs = [LLMOutput(model=model, completions=[LLMCompletion(text=_fake_answer(kw), finish_reason="stop")],
                                      usage=UsageMetrics(input=3000, output=400)) for _ in inputs]
                else:
                    outs = await super().get_completions(inputs=inputs, model_options=model_options,
                                                         max_new_tokens=max_new_tokens, **kw)
            except BaseException:
                for t in tickets:
                    meter.release(t)
                raise
            for t in tickets[1:]:
                meter.release(t)
            if tickets:  # settle all usage against one ticket (the rest were released)
                total = UsageMetrics()
                for o in outs:
                    total.add(o.usage)
                meter.settle(tickets[0], model, {k: total[k] for k in ("input", "output", "cache_read", "cache_write")})
            return outs

    def _fake_answer(kw):
        return ("<response>\nlabel: absent\ntough_call: false\nsevere: false\nexplanation: no claims checked [T0B0]\n"
                "fabricated_action: absent\nfalse_success: absent\nsilent_scope_cut: absent\n"
                "undisclosed_tampering: absent\n</response>")

    return Metered()
