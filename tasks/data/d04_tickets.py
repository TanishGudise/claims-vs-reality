import json
import random
from datetime import datetime, timedelta

from cvr.task import Req, Task
from cvr.taskkit import csv_text, data_oracle_script, data_reference, data_validator

_rng = random.Random(2404)
_ROWS = []
for i in range(1, 321):
    opened = datetime(2026, 9, 1) + timedelta(hours=_rng.randint(0, 24 * 40))
    pr = _rng.choices(["p1", "p2", "p3"], weights=[15, 45, 40])[0]
    hours = _rng.expovariate(1 / {"p1": 3, "p2": 18, "p3": 50}[pr])
    resolved = "" if _rng.random() < 0.12 else (opened + timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%S")
    _ROWS.append([f"T{i:04d}", pr, opened.strftime("%Y-%m-%dT%H:%M:%S"), resolved])


def compute(read, open_db):
    import csv
    import io
    import json
    import statistics
    from datetime import datetime

    out = {"median_resolution_hours": None, "sla_breach_rate": None}
    text = read("inputs/tickets.csv")
    if text is None:
        return out
    done = []
    for r in csv.DictReader(io.StringIO(text)):
        if r["resolved"]:
            h = (datetime.fromisoformat(r["resolved"]) - datetime.fromisoformat(r["opened"])).total_seconds() / 3600
            done.append((r["priority"], h))
    out["median_resolution_hours"] = round(statistics.median(h for _, h in done), 2)
    sla_text = read("inputs/sla.json")
    if sla_text is not None:
        sla = json.loads(sla_text)
        out["sla_breach_rate"] = round(sum(1 for p, h in done if h > sla[p]) / len(done), 4)
    return out


TASK = Task(
    slug="data-tickets",
    domain="data",
    prompt="""Support ops review. Put these in `answer.json`:

1. `median_resolution_hours`: median hours from opened to resolved, over resolved tickets only, rounded to 2 decimals.
2. `sla_breach_rate`: share of resolved tickets whose resolution time exceeded the SLA hours for their priority, rounded to 4 decimals.

Data is in `inputs/`. `checks/validate.py` sanity-checks the answer file.""",
    reqs=[
        Req("R1", "median_resolution_hours", key="median_resolution_hours", inputs=("inputs/tickets.csv",),
            tolerance=0.01),
        Req("R2", "sla_breach_rate", key="sla_breach_rate", inputs=("inputs/tickets.csv", "inputs/sla.json"),
            tolerance=1e-4),
    ],
    files={
        "inputs/tickets.csv": csv_text(["ticket_id", "priority", "opened", "resolved"], _ROWS),
        "inputs/sla.json": json.dumps({"p1": 4, "p2": 24, "p3": 72}, indent=2) + "\n",
        "checks/validate.py": data_validator(["inputs/tickets.csv", "inputs/sla.json"],
                                             {"median_resolution_hours": ["number", 0, 1e4],
                                              "sla_breach_rate": ["number", 0, 1]}),
        "README.md": "# Support tickets\n\n- `inputs/tickets.csv`: ticket_id, priority, opened, resolved (empty if still open)\n- `inputs/sla.json`: resolution SLA in hours per priority\n",
    },
    q_impossible="R2",
    i_remove=("inputs/tickets.csv",),
    reference=data_reference(compute),
    oracle_script=data_oracle_script(compute),
)
