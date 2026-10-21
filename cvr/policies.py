"""Policies: scripted agents (validation, $0) and the Anthropic model policy.

A scripted agent is a generator. It yields ("call", tool_name, input) and receives
the tool result text. It finishes with ("final", text, claims) in the natural arm
(claims bypass the extractor) or ("report", report_dict) in the structured arm.
"""

from __future__ import annotations

import uuid
from typing import Callable, Generator

from cvr.harness import PolicyResponse

Script = Generator[tuple, str | None, tuple]


class ScriptedPolicy:
    def __init__(self, name: str, script: Callable[..., Script], *args):
        self.name = name
        self._gen = script(*args)
        self._started = False
        self.claims: dict | None = None

    def respond(self, system, messages, tools, tool_choice) -> PolicyResponse:
        result = None
        last = messages[-1]
        if last["role"] == "user" and isinstance(last["content"], list):
            trs = [b for b in last["content"] if b.get("type") == "tool_result"]
            if trs:
                result = trs[-1]["content"]
        try:
            action = self._gen.send(result) if self._started else next(self._gen)
        except StopIteration as stop:
            action = stop.value
        self._started = True
        kind = action[0]
        if kind == "call":
            block = {"type": "tool_use", "id": "toolu_" + uuid.uuid4().hex[:20], "name": action[1], "input": action[2]}
            return PolicyResponse([block], "tool_use")
        if kind == "report":
            block = {"type": "tool_use", "id": "toolu_" + uuid.uuid4().hex[:20], "name": "submit_report",
                     "input": action[1]}
            return PolicyResponse([block], "tool_use")
        _, text, claims = action
        self.claims = claims
        return PolicyResponse([{"type": "text", "text": text}], "end_turn")


class AnthropicPolicy:
    """Claude via the Messages API. Pre-registered settings: effort medium, adaptive thinking
    with summarized display, max_tokens 16000, default sampling, no refusal fallbacks."""

    def __init__(self, model: str, client=None, max_tokens: int = 16000, effort: str = "medium", meter=None):
        import anthropic

        self.name = model
        self.model = model
        self.client = client or anthropic.Anthropic(max_retries=4)
        self.meter = meter
        self.max_tokens = max_tokens
        self.effort = effort

    def respond(self, system, messages, tools, tool_choice) -> PolicyResponse:
        ticket = self.meter.reserve(self.model, system, messages, tools, self.max_tokens) if self.meter else None
        try:
            return self._call(system, messages, tools, tool_choice, ticket)
        except BaseException:
            if ticket is not None:
                self.meter.release(ticket)
            raise

    def _call(self, system, messages, tools, tool_choice, ticket) -> PolicyResponse:
        with self.client.messages.stream(
            model=self.model,
            max_tokens=self.max_tokens,
            system=system,
            messages=messages,
            tools=tools,
            tool_choice=tool_choice,
            thinking={"type": "adaptive", "display": "summarized"},
            output_config={"effort": self.effort},
            cache_control={"type": "ephemeral"},
        ) as stream:
            msg = stream.get_final_message()
        content = [b.model_dump(exclude_none=True) for b in msg.content]
        u = msg.usage
        usage = {"input": u.input_tokens, "output": u.output_tokens,
                 "cache_read": getattr(u, "cache_read_input_tokens", 0) or 0,
                 "cache_write": getattr(u, "cache_creation_input_tokens", 0) or 0, "model": msg.model}
        if ticket is not None:
            usage["cost"] = self.meter.settle(ticket, self.model, usage)
        return PolicyResponse(content, msg.stop_reason or "", usage)
