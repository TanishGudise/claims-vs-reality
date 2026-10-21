"""Convert episodes to Transluce's `behaviors` records (behaviors 0.0.1a2, test-only).

The harness never imports this; it exists so runs can be loaded into fxtr/behaviors
tooling. By default the same view as the Docent export (no thinking); pass
include_reasoning=True to keep the agent's summarized thinking as
ContentSummarizedReasoning blocks.
"""

from __future__ import annotations


def to_behaviors_agent_run(record: dict, episode_id: str, include_reasoning: bool = False):
    from behaviors.types import (AgentRun, AssistantMessage, ContentSummarizedReasoning, ContentText,
                                 ContentToolUse, SystemMessage, ToolMessage, Transcript, UserMessage)

    msgs = [SystemMessage(content=(ContentText(text=record["system"]),))]
    for m in record["messages"]:
        if m["role"] == "assistant":
            blocks = []
            for b in m["content"] if isinstance(m["content"], list) else [{"type": "text", "text": m["content"]}]:
                t = b.get("type")
                if t == "text":
                    blocks.append(ContentText(text=b["text"]))
                elif t == "tool_use":
                    blocks.append(ContentToolUse(id=b["id"], name=b["name"], input=dict(b.get("input") or {}),
                                                 parse_error=None))
                elif t == "thinking" and include_reasoning and b.get("thinking"):
                    blocks.append(ContentSummarizedReasoning(text=b["thinking"], signature=None))
            msgs.append(AssistantMessage(content=tuple(blocks)))
        elif isinstance(m["content"], str):
            msgs.append(UserMessage(content=(ContentText(text=m["content"]),)))
        else:
            notes = []
            for b in m["content"]:
                if b.get("type") == "tool_result":
                    c = b.get("content")
                    text = c if isinstance(c, str) else "\n".join(x.get("text", "") for x in c)
                    msgs.append(ToolMessage(content=(ContentText(text=text),), tool_call_id=b["tool_use_id"],
                                            is_error=bool(b.get("is_error")) or None))
                elif b.get("type") == "text":
                    notes.append(b["text"])
            if notes:
                msgs.append(UserMessage(content=(ContentText(text="\n".join(notes)),)))
    transcript = Transcript(name=None, description=None, messages=tuple(msgs), metadata={})
    return AgentRun(name=None, description=None, transcripts=(transcript,), groups=(),
                    metadata={"episode": episode_id})
