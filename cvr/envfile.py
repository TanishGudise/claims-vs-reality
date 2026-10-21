"""Load KEY=VALUE lines from the repo's gitignored .env into os.environ (never printed)."""

import os
from pathlib import Path


def load_env() -> list[str]:
    path = Path(__file__).resolve().parent.parent / ".env"
    loaded = []
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip().removeprefix("export ").strip(), v.strip().strip('"').strip("'")
            if k and v and k not in os.environ:
                os.environ[k] = v
                loaded.append(k)
    return loaded  # names only
