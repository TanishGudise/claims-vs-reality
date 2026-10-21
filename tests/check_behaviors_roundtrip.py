"""Round trip, run with .venv-behaviors: episode -> behaviors AgentRun -> Docent (via behaviors'
own converter) gives the same messages as our direct Docent export."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from behaviors.implementations.docent import to_docent_agent_run  # noqa: E402

from cvr.export_behaviors import to_behaviors_agent_run  # noqa: E402
from cvr.export_docent import to_agent_run  # noqa: E402


def norm(ar):
    out = []
    for m in ar.transcripts[0].messages:
        calls = [(c.function, json.dumps(c.arguments, sort_keys=True)) for c in (getattr(m, "tool_calls", None) or [])]
        out.append((m.role, m.text.strip(), calls, getattr(m, "tool_call_id", None)))
    return out


def main(episode_dir: str, n: int = 40) -> int:
    files = sorted(Path(episode_dir).glob("*.json"))[:n]
    bad = 0
    for f in files:
        rec = json.loads(f.read_text())["record"]
        direct = to_agent_run(rec, f.stem)
        via = to_docent_agent_run(to_behaviors_agent_run(rec, f.stem))
        if norm(direct) != norm(via):
            bad += 1
            a, b = norm(direct), norm(via)
            i = next(i for i in range(min(len(a), len(b))) if a[i] != b[i]) if len(a) == len(b) else -1
            print("MISMATCH", f.stem, len(a), len(b), a[i] if i >= 0 else "", b[i] if i >= 0 else "")
    print(f"{len(files) - bad}/{len(files)} episodes round-trip identically")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
