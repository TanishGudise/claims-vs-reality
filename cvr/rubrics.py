"""Load the pre-registered judge rubrics as Docent `Rubric` objects (docent==0.1.88)."""

import hashlib
import json
from pathlib import Path

from docent._llm_util.providers.preference_types import ModelOption
from docent.judges.types import Rubric

RUBRIC_DIR = Path(__file__).resolve().parent.parent / "rubrics"
RUBRIC_NAMES = ("transluce_overselling", "transluce_monitor_evasion", "claims_vs_reality")
# Main judge (Amendment 0): Claude Sonnet 5.5 at medium effort, one rollout per input.
# The Opus 5.5 subset judge is set in cvr/judge.py.
JUDGE_MODEL = ModelOption(provider="anthropic", model_name="claude-sonnet-5-5", reasoning_effort="medium")


def load_rubric(name: str) -> Rubric:
    schemas = json.loads((RUBRIC_DIR / "schemas.json").read_text())
    return Rubric(
        id=name,
        rubric_text=(RUBRIC_DIR / f"{name}.md").read_text(),
        output_schema=schemas[name],
        judge_model=JUDGE_MODEL,
    )


def rubric_hashes() -> dict[str, str]:
    """sha256 of each rubric text plus the shared schema file, for the pre-registration."""
    files = [f"{n}.md" for n in RUBRIC_NAMES] + ["schemas.json"]
    return {f: hashlib.sha256((RUBRIC_DIR / f).read_bytes()).hexdigest() for f in files}
