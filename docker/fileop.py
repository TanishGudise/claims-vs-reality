"""In-sandbox helper behind the read_file / write_file / edit_file tools.

Runs as the agent user under the tracer, so every file access is in the ledger.
Arguments arrive base64-encoded in environment variables to avoid shell quoting.
"""

import base64
import os
import sys


def _arg(name: str) -> str:
    return base64.b64decode(os.environ.get(name, "")).decode("utf-8", errors="replace")


def main() -> int:
    op, path = sys.argv[1], _arg("CVR_PATH")
    try:
        if op == "read":
            with open(path, encoding="utf-8", errors="replace") as f:
                sys.stdout.write(f.read())
            return 0
        if op == "write":
            parent = os.path.dirname(path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write(_arg("CVR_CONTENT"))
            print(f"Wrote {path}")
            return 0
        if op == "edit":
            old, new = _arg("CVR_OLD"), _arg("CVR_NEW")
            with open(path, encoding="utf-8") as f:
                text = f.read()
            n = text.count(old)
            if n != 1:
                print(f"Error: old_string found {n} times in {path}; it must match exactly once.")
                return 1
            with open(path, "w", encoding="utf-8") as f:
                f.write(text.replace(old, new))
            print(f"Edited {path}")
            return 0
    except OSError as e:
        print(f"Error: {e}")
        return 1
    print(f"Error: unknown operation {op}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
