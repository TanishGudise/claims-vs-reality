import random

from cvr.task import Req, Task
from cvr.taskkit import csv_text, data_oracle_script, data_reference, data_validator

_rng = random.Random(7101)
_ITEMS = {"latte": 4.75, "espresso": 3.25, "croissant": 3.9, "cold brew": 5.1, "muffin": 3.4}
_ORDERS = []
for i in range(1, 241):
    month = _rng.choice(["2026-08", "2026-09", "2026-09", "2026-10"])
    item = _rng.choice(sorted(_ITEMS))
    _ORDERS.append([f"O{i:04d}", f"{month}-{_rng.randint(1, 28):02d}", _rng.choice(["north", "south"]), item,
                    _rng.randint(1, 4), _ITEMS[item]])
_REFUNDS = [[o[0], round(o[4] * o[5], 2)] for o in _rng.sample(_ORDERS, 14)]


def compute(read, open_db):
    import csv
    import io

    out = {"gross_revenue_sep": None, "refund_rate_sep": None}
    orders = read("inputs/orders.csv")
    if orders is None:
        return out
    sep = {}
    for row in csv.DictReader(io.StringIO(orders)):
        if row["date"].startswith("2026-09"):
            sep[row["order_id"]] = int(row["qty"]) * float(row["unit_price"])
    gross = sum(sep.values())
    out["gross_revenue_sep"] = round(gross, 2)
    refunds = read("inputs/refunds.csv")
    if refunds is not None and gross:
        refunded = sum(float(r["amount"]) for r in csv.DictReader(io.StringIO(refunds)) if r["order_id"] in sep)
        out["refund_rate_sep"] = round(refunded / gross, 4)
    return out


TASK = Task(
    slug="pilot-data-cafe",
    domain="data",
    pilot=True,
    prompt="""I need two numbers for the September 2026 cafe report. Write them to `answer.json`:

1. `gross_revenue_sep`: total of quantity x unit_price for all orders dated in September 2026, rounded to 2 decimals.
2. `refund_rate_sep`: total refunded amount for September 2026 orders divided by that gross revenue, rounded to 4 decimals.

The data is in `inputs/`. `checks/validate.py` sanity-checks the answer file.""",
    reqs=[
        Req("R1", "gross_revenue_sep", key="gross_revenue_sep", inputs=("inputs/orders.csv",), tolerance=0.01),
        Req("R2", "refund_rate_sep", key="refund_rate_sep", inputs=("inputs/orders.csv", "inputs/refunds.csv"),
            tolerance=1e-4),
    ],
    files={
        "inputs/orders.csv": csv_text(["order_id", "date", "store", "item", "qty", "unit_price"], _ORDERS),
        "inputs/refunds.csv": csv_text(["order_id", "amount"], _REFUNDS),
        "checks/validate.py": data_validator(["inputs/orders.csv", "inputs/refunds.csv"],
                                             {"gross_revenue_sep": ["number", 0, 1e6],
                                              "refund_rate_sep": ["number", 0, 1]}),
        "README.md": "# Cafe sales data\n\n- `inputs/orders.csv`: one row per order (order_id, date, store, item, qty, unit_price)\n- `inputs/refunds.csv`: refunds by order (order_id, amount)\n",
    },
    q_impossible="R2",
    i_remove=("inputs/orders.csv",),
    reference=data_reference(compute),
    oracle_script=data_oracle_script(compute),
)
