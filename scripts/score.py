"""Score every episode in a run with the exact detectors. $0.

usage: python scripts/score.py --run runs/<phase> [--claims-override labels/claims_override.json]
Claims: structured arm from the report form; natural arm from runs/<phase>/claims/<episode>.json
(extractor) or, after adjudication, from the override file (episode -> claims). Writes
runs/<phase>/scores.jsonl, one line per episode.
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cvr import detect, extractor  # noqa: E402
from cvr.task import load_tasks  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", required=True)
    ap.add_argument("--claims-override", default="")
    ap.add_argument("--adjudicated", default="", help="hand-check corrections: {episode: {behavior: bool, 'reason': str}}")
    a = ap.parse_args()
    run = Path(a.run)
    tasks = {t.id: t for t in load_tasks()}
    override = json.loads(Path(a.claims_override).read_text()) if a.claims_override else {}
    adj = json.loads(Path(a.adjudicated).read_text()) if a.adjudicated else {}
    n = missing = extractor_failed = 0
    with open(run / "scores.jsonl", "w") as out:
        for f in sorted((run / "episodes").glob("*.json")):
            d = json.loads(f.read_text())
            r, rec = d["result"], d["record"]
            t = tasks[r["task"]]
            source = "report"
            if r["arm"] == "structured":
                claims = detect.claims_from_report(t, rec["report"])
            elif f.stem in override:
                claims, source = override[f.stem], "adjudicated"
            else:
                cf = run / "claims" / f.name
                if not rec.get("final_text"):
                    claims, source = None, "no_final"
                elif not cf.exists():
                    missing += 1
                    continue
                elif "output" not in json.loads(cf.read_text()):
                    extractor_failed += 1
                    continue
                else:
                    claims, source = extractor.to_claims(json.loads(cf.read_text())["output"]), "extractor"
            det = detect.detect(t, r["variant"], rec, r["grade"], claims)
            exact = {k: det[k] for k in ("a", "b", "c", "d")}
            if f.stem in adj:  # §12 hand-check correction of the reference; the frozen detector output is kept too
                det.update({k: v for k, v in adj[f.stem].items() if k in ("a", "b", "c", "d")})
                det["any"] = det["a"] or det["b"] or det["c"] or det["d"]
            out.write(json.dumps({"episode": f.stem, "model": r.get("model", rec["policy"]), "task": r["task"],
                                  "domain": t.domain, "variant": r["variant"], "condition": r["condition"],
                                  "arm": r["arm"], "claims_source": source, "flags": r["flags"],
                                  "ledger_ok": r.get("ledger_ok"), "ledger_count_ok": r.get("ledger_count_ok"),
                                  "excluded": not (r.get("ledger_ok") and r.get("ledger_count_ok")),
                                  **{k: det[k] for k in ("a", "b", "c", "d", "any")}, "d_plus": det.get("d_plus"),
                                  "ambiguous": (det.get("details") or {}).get("ambiguous", []),
                                  "exact_frozen": exact, "adjudication": adj.get(f.stem, {}).get("reason"),
                                  "opportunity": det["opportunity"], "tampering_events": det["tampering_events"],
                                  "details": det.get("details")}) + "\n")
            n += 1
    fails = run / "failures.jsonl"
    infra = {}
    if fails.exists():
        for line in fails.read_text().splitlines():
            k = json.loads(line)["job"]
            infra[k] = infra.get(k, 0) + 1
    print(json.dumps({"scored": n, "awaiting_claims": missing, "extractor_failed": extractor_failed,
                      "infra_excluded": sum(1 for v in infra.values() if v >= 2)}))


if __name__ == "__main__":
    main()
