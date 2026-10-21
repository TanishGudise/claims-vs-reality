"""Write manifest.json (Amendment 0): every task's id, slug, domain, content hash, oracle path
length and budget, plus the extractor prompt hash and rubric hashes. $0.

usage: python scripts/manifest.py [--check]   (--check: recompute and compare, exit 1 on drift)
"""

import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cvr import extractor  # noqa: E402
from cvr.harness import NEUTRAL_BUDGET, budget_for  # noqa: E402
from cvr.rubrics import rubric_hashes  # noqa: E402
from cvr.runner import oracle_steps  # noqa: E402
from cvr.task import load_tasks, manifest_entry  # noqa: E402

PATH = Path(__file__).resolve().parent.parent / "manifest.json"


def build() -> dict:
    tasks = []
    for t in sorted(load_tasks(), key=lambda t: (t.pilot, t.domain, t.slug)):
        e = manifest_entry(t)
        e.update(oracle_steps=oracle_steps(t), budget_B=budget_for(t, "B", oracle_steps(t)),
                 budget_other=NEUTRAL_BUDGET, requirements=[r.id for r in t.reqs], q_impossible=t.q_impossible,
                 check_pattern=t.check_pattern())
        tasks.append(e)
    root = Path(__file__).resolve().parent.parent
    code = {f: hashlib.sha256((root / f).read_bytes()).hexdigest()  # Amendment 7: frozen scoring + form
            for f in ("cvr/detect.py", "cvr/grade.py", "cvr/harness.py", "cvr/extractor.py")}
    body = {"tasks": tasks, "extractor_prompt_sha256": extractor.prompt_hash(), "rubrics_sha256": rubric_hashes(),
            "frozen_code_sha256": code}
    body["manifest_sha256"] = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
    return body


if __name__ == "__main__":
    m = build()
    if "--check" in sys.argv:
        old = json.loads(PATH.read_text())
        same = old["manifest_sha256"] == m["manifest_sha256"]
        print("manifest", "unchanged" if same else "DRIFTED", m["manifest_sha256"])
        sys.exit(0 if same else 1)
    PATH.write_text(json.dumps(m, indent=1) + "\n")
    print(len(m["tasks"]), "tasks; manifest", m["manifest_sha256"])
