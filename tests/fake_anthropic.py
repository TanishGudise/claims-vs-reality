"""A stand-in for anthropic.Anthropic that returns real SDK Message objects. No network.

Each episode: list the directory, run the tests or checks once, then send a final message.
"""

import itertools

from anthropic.types import Message

_ids = itertools.count(1)


class _Stream:
    def __init__(self, msg):
        self.msg = msg

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get_final_message(self):
        return self.msg


class _Messages:
    def __init__(self, calls):
        self.calls = calls

    def stream(self, **kw):
        self.calls.append(kw)
        n_assistant = sum(1 for m in kw["messages"] if m["role"] == "assistant")
        model = kw["model"]
        if kw.get("tool_choice", {}).get("type") == "none" or n_assistant >= 2:
            content = [{"type": "thinking", "thinking": "wrap up", "signature": "sig"},
                       {"type": "text", "text": "Done. I ran the tests and everything passes."}]
            stop = "end_turn"
            if any(t["name"] == "submit_report" for t in kw["tools"]):
                content = [{"type": "tool_use", "id": f"toolu_f{next(_ids)}", "name": "submit_report", "input": {
                    "overall_status": "complete", "requirements": [], "actions": [
                        {"type": "ran_all_tests", "target": "", "result": "pass"}],
                    "files_modified": [], "changed_or_bypassed_checks": False, "summary": "Done."}}]
                stop = "tool_use"
        else:
            cmd = "ls -R" if n_assistant == 0 else "python -m pytest -q 2>&1 | tail -5; python3 checks/validate.py 2>&1 | tail -3"
            content = [{"type": "thinking", "thinking": "look around", "signature": "sig"},
                       {"type": "tool_use", "id": f"toolu_f{next(_ids)}", "name": "bash", "input": {"command": cmd}}]
            stop = "tool_use"
        return _Stream(Message.model_validate({
            "id": f"msg_f{next(_ids)}", "type": "message", "role": "assistant", "model": model,
            "content": content, "stop_reason": stop, "stop_sequence": None,
            "usage": {"input_tokens": 1200, "output_tokens": 150, "cache_read_input_tokens": 800,
                      "cache_creation_input_tokens": 300}}))


class FakeClient:
    def __init__(self):
        self.calls = []
        self.messages = _Messages(self.calls)
