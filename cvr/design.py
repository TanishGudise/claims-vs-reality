"""Exact job lists for the pilot and the main run (preregistration §8, §11; Amendment 0).

A job is (model, task_id, variant, condition, arm). The Sonnet/Opus task subset is drawn
with random.Random(20261017) from the frozen manifest order (domain, slug), 6 per domain.
"""

from __future__ import annotations

import random

from cvr.task import CONDITIONS, DOMAINS, Task, load_tasks

HAIKU, SONNET, OPUS = "claude-haiku-5-5", "claude-sonnet-5-5", "claude-opus-5-5"
SUBSET_SEED = 20261017


def main_tasks() -> list[Task]:
    return sorted((t for t in load_tasks() if not t.pilot), key=lambda t: (t.domain, t.slug))


def pilot_tasks() -> list[Task]:
    return sorted((t for t in load_tasks() if t.pilot), key=lambda t: (t.domain, t.slug))


def frontier_subset(n_per_domain: int = 6) -> list[Task]:
    """Seeded order per domain; rung 5 keeps the first 4 of each domain's order."""
    rng = random.Random(SUBSET_SEED)
    out = []
    for d in DOMAINS:
        ts = [t for t in main_tasks() if t.domain == d]
        out += rng.sample(ts, len(ts))[:n_per_domain]  # a seeded permutation, truncated
    return out


def main_jobs(rung: int = 0, open_weight: str | None = None) -> list[tuple]:
    jobs = []
    tasks = main_tasks()
    for t in tasks:  # Haiku: full grid, natural; I+Q x 4 conditions, structured
        for v in ("P", "I", "Q"):
            for c in CONDITIONS:
                jobs.append((HAIKU, t.id, v, c, "natural"))
                if v != "P":
                    jobs.append((HAIKU, t.id, v, c, "structured"))
    n_frontier = 4 if rung >= 5 else 6
    for model in (SONNET, OPUS):
        for t in frontier_subset(n_frontier):
            for v in ("I", "Q"):
                for c in ("N", "U", "B"):
                    jobs.append((model, t.id, v, c, "natural"))
                if rung < 4:
                    jobs.append((model, t.id, v, "N", "structured"))
            jobs.append((model, t.id, "P", "N", "natural"))
    if open_weight and rung < 6:
        for t in tasks:
            for v in ("P", "I", "Q"):
                for c in CONDITIONS:
                    jobs.append((open_weight, t.id, v, c, "natural"))
    return jobs


def judged_per_model(rung: int) -> int | None:
    """None = every natural-arm episode (rung 0). Judge cuts come before any agent cut."""
    return {0: None, 1: 150, 2: 100, 3: 75}.get(rung, 50)


def pilot_jobs() -> list[tuple]:
    jobs = []
    for t in pilot_tasks():
        for v in ("P", "I", "Q"):
            for c in CONDITIONS:
                for arm in ("natural", "structured"):
                    jobs.append((HAIKU, t.id, v, c, arm))
        for v in ("I", "Q"):
            for c in ("N", "U", "B"):
                jobs.append((SONNET, t.id, v, c, "natural"))
            jobs.append((OPUS, t.id, v, "N", "natural"))
        jobs.append((SONNET, t.id, "P", "N", "natural"))
    return jobs
