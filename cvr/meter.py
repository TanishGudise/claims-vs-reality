"""Hard spend cap for paid runs. Every call reserves its worst case before it is made
(1 token per character of input, the full max_tokens of output); the reservation is
replaced by the real cost from the returned usage. When a reservation would exceed the
cap, the call is refused and the episode stops (it is flagged, not scored)."""

from __future__ import annotations

import json
import threading
from pathlib import Path

# $ per million tokens (claude-api skill, cached 2026-10-06). cache_write = 1.25 x input (5 min TTL).
PRICES = {
    "claude-haiku-5-5": {"input": 0.10, "output": 0.50, "cache_read": 0.01, "cache_write": 0.125},
    "claude-sonnet-5-5": {"input": 2.00, "output": 10.00, "cache_read": 0.20, "cache_write": 2.50},
    "claude-opus-5-5": {"input": 4.00, "output": 20.00, "cache_read": 0.20, "cache_write": 5.00},
}


class CapReached(RuntimeError):
    pass


def prior_spend(*logs: Path) -> float:
    """Total already spent according to spend logs (last 'spent' of meter logs, or summed 'cost')."""
    total = 0.0
    for log in logs:
        if not log.exists():
            continue
        lines = [json.loads(l) for l in log.read_text().splitlines() if l.strip()]
        if lines and "spent" in lines[-1]:
            total += lines[-1]["spent"]
        else:
            total += sum(l.get("cost", 0.0) for l in lines)
    return total


# The pre-registered manifest hash (latest amendment). Changing tasks or grading definitions
# requires a new amendment that updates this constant.
FROZEN_MANIFEST = "700b080de671311f3451112063696d49a273a9691f7de40b3930e45054b9d1d6"


def require_frozen_manifest():
    """Every paid step first recomputes the task manifest and compares it with the pinned hash."""
    import importlib.util

    root = Path(__file__).resolve().parent.parent
    spec = importlib.util.spec_from_file_location("cvr_manifest", root / "scripts" / "manifest.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    got = mod.build()["manifest_sha256"]
    if got != FROZEN_MANIFEST:
        raise SystemExit(f"manifest {got} != pre-registered {FROZEN_MANIFEST}; refusing paid work")


class Meter:
    def __init__(self, cap_usd: float, ledger_path: Path | None = None, spent: float = 0.0):
        self.cap = cap_usd
        self.spent = spent
        self.reserved = 0.0
        self.lock = threading.Lock()
        self.path = ledger_path
        self._n = 0

    def _worst(self, model, system, messages, tools, max_tokens, extra_input_tokens=0) -> float:
        chars = len(system) + len(json.dumps(messages, default=str)) + len(json.dumps(tools))
        p = PRICES[model]
        # input could be billed at the cache-write rate in the worst case
        return (chars + extra_input_tokens) * max(p["input"], p["cache_write"]) / 1e6 + max_tokens * p["output"] / 1e6

    def reserve(self, model, system, messages, tools, max_tokens, extra_input_tokens: int = 0) -> dict:
        w = self._worst(model, system, messages, tools, max_tokens, extra_input_tokens)
        with self.lock:
            if self.spent + self.reserved + w > self.cap:
                raise CapReached(f"cap ${self.cap:.2f}: spent {self.spent:.4f} + reserved {self.reserved:.4f} + {w:.4f}")
            self.reserved += w
            self._n += 1
            return {"id": self._n, "amount": w}

    def release(self, ticket: dict):
        with self.lock:
            self.reserved -= ticket["amount"]

    def cost(self, model: str, usage: dict) -> float:
        p = PRICES[model]
        return (usage.get("input", 0) * p["input"] + usage.get("output", 0) * p["output"]
                + usage.get("cache_read", 0) * p["cache_read"] + usage.get("cache_write", 0) * p["cache_write"]) / 1e6

    def settle(self, ticket: dict, model: str, usage: dict) -> float:
        c = self.cost(model, usage)
        with self.lock:
            self.reserved -= ticket["amount"]
            self.spent += c
            if self.path:
                with open(self.path, "a") as f:
                    f.write(json.dumps({"model": model, "usage": usage, "cost": c, "spent": self.spent}) + "\n")
                    f.flush()
        return c
