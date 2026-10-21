"""Export episodes as Docent AgentRuns (docent==0.1.88) with nothing that hints at the labels.

What the judge sees (preregistration §5.3): the system prompt, the task message, the
agent's text and tool calls, tool outputs, and harness notices. Never: thinking
summaries, variant or condition names, task ids, grades, ledger, labels. The run
metadata carries only an opaque episode id; the mapping lives outside the export.
"""

from __future__ import annotations

import hashlib
import json

from docent.data_models.agent_run import AgentRun, AgentRunView
from docent.data_models.chat.message import AssistantMessage, SystemMessage, ToolMessage, UserMessage
from docent.data_models.chat.tool import ToolCall
from docent.data_models.transcript import Transcript


def opaque_id(record_key: str, salt: str) -> str:
    return hashlib.sha256((salt + record_key).encode()).hexdigest()[:16]


def _text(content) -> str:
    if isinstance(content, str):
        return content
    return "\n".join(b.get("text", "") for b in content if b.get("type") == "text")


def to_agent_run(record: dict, episode_id: str) -> AgentRun:
    msgs = [SystemMessage(content=record["system"])]
    tool_names: dict[str, str] = {}
    for m in record["messages"]:
        if m["role"] == "assistant":
            blocks = m["content"] if isinstance(m["content"], list) else [{"type": "text", "text": m["content"]}]
            calls = []
            for b in blocks:
                if b.get("type") == "tool_use":
                    tool_names[b["id"]] = b["name"]
                    calls.append(ToolCall(id=b["id"], function=b["name"], arguments=dict(b.get("input") or {}),
                                          type="function"))
            text = "\n".join(b["text"] for b in blocks if b.get("type") == "text")  # thinking excluded
            msgs.append(AssistantMessage(content=text, tool_calls=calls or None))
        elif isinstance(m["content"], str):
            msgs.append(UserMessage(content=m["content"]))
        else:
            notes = []
            for b in m["content"]:
                if b.get("type") == "tool_result":
                    c = b.get("content")
                    msgs.append(ToolMessage(content=c if isinstance(c, str) else _text(c), tool_call_id=b["tool_use_id"],
                                            function=tool_names.get(b["tool_use_id"]),
                                            error={"is_error": True} if b.get("is_error") else None))
                elif b.get("type") == "text":
                    notes.append(b["text"])
            if notes:
                msgs.append(UserMessage(content="\n".join(notes)))
    return AgentRun(transcripts=[Transcript(messages=msgs)], metadata={"episode": episode_id})


FORBIDDEN_KEYS = ("variant", "condition", "slug", "grade", "ledger", "reference", "impossible", "q_impossible")


def leak_check(agent_run: AgentRun, record: dict, slug: str) -> list[str]:
    """Strings that must not appear in what the judge reads (gate §10.4)."""
    text = AgentRunView.from_agent_run(agent_run).to_text()
    problems = []
    if slug in text:
        problems.append(f"slug {slug!r} in judge text")
    if record.get("task") and record["task"] in text:
        problems.append("task id in judge text")
    for k in ("task", "variant", "condition", "arm", "policy"):
        token = json.dumps({k: record.get(k)})[1:-1]  # e.g. '"variant": "Q"'
        if token in text:
            problems.append(f"{token} in judge text")
    if set(agent_run.metadata) != {"episode"}:
        problems.append(f"unexpected metadata keys {sorted(agent_run.metadata)}")
    if any(b.get("type") == "thinking" and b.get("thinking") and b["thinking"][:40] in text
           for m in record["messages"] if m["role"] == "assistant" and isinstance(m["content"], list)
           for b in m["content"]):
        problems.append("thinking text in judge text")
    return problems


def dump(agent_run: AgentRun) -> str:
    return agent_run.model_dump_json()
