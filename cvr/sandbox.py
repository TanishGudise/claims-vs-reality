"""One Docker container per episode; every agent command runs traced as uid 1000.

The tracer (strace) runs as root and writes to /var/ledger, which only root can
read. The host copies each trace out right after the call. Containers are tracked
by exact id and removed by that id.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import subprocess
import tarfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

IMAGE = "claims-vs-reality-agent:0.2"
TRACE_CALLS = ("execve,execveat,openat,open,creat,unlink,unlinkat,rename,renameat,renameat2,"
               "truncate,ftruncate,link,linkat,symlink,symlinkat")
CMD_TIMEOUT = 120  # seconds per tool call
KILL_AGENT = ("import os\nfor p in os.listdir('/proc'):\n"
              "    try:\n        p.isdigit() and os.stat('/proc/'+p).st_uid == 1000 and os.kill(int(p), 9)\n"
              "    except OSError:\n        pass\n")
CAPS = ["SYS_PTRACE", "SETUID", "SETGID", "KILL", "CHOWN", "DAC_OVERRIDE", "FOWNER"]


def _run(args, timeout=60, input_bytes=None):
    return subprocess.run(args, capture_output=True, timeout=timeout, input=input_bytes)


@dataclass
class ExecResult:
    exit_code: int
    output: str  # stdout + stderr as the agent would see it
    trace: str
    timed_out: bool
    started: float
    ended: float


class Sandbox:
    def __init__(self, workspace: dict[str, bytes], ops_config: dict | None = None, label: str = ""):
        self.id = ""
        self.workspace = workspace
        self.ops_config = ops_config
        self.label = label
        self._n = 0

    # lifecycle -------------------------------------------------------------
    def start(self, registry: Path | None = None) -> "Sandbox":
        args = ["docker", "run", "-d", "--init", "--network", "none", "--cap-drop", "ALL"]
        for c in CAPS:
            args += ["--cap-add", c]
        args += ["--security-opt", "no-new-privileges", "--pids-limit", "512", "--memory", "1g",
                 "--label", f"cvr={self.label or 'episode'}", IMAGE, "sleep", "3600"]
        r = _run(args)
        if r.returncode != 0:
            raise RuntimeError(f"docker run failed: {r.stderr.decode()[-500:]}")
        self.id = r.stdout.decode().strip()
        if registry is not None:  # exact-id record so cleanup never needs a pattern
            registry.parent.mkdir(parents=True, exist_ok=True)
            with open(registry, "a") as f:
                f.write(self.id + "\n")
        self._put_tree("/work", self.workspace, owner=1000)
        if self.ops_config is not None:
            self._put_tree("/var/opsstate", {"config.json": json.dumps(self.ops_config).encode()}, owner=0)
            r = _run(["docker", "exec", "-d", self.id, "python3", "/opt/ops/server.py"])
            if r.returncode != 0:
                raise RuntimeError("ops server failed to start")
            for _ in range(100):
                if _run(["docker", "exec", self.id, "test", "-e", "/var/opsstate/ready"]).returncode == 0:
                    break
                time.sleep(0.05)
            else:
                raise RuntimeError("ops server not ready")
        return self

    def stop(self):
        if self.id:
            _run(["docker", "rm", "-f", self.id])
            self.id = ""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.stop()

    # files -----------------------------------------------------------------
    def _put_tree(self, dest: str, files: dict[str, bytes], owner: int):
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tar:
            dirs = set()
            for path in sorted(files):
                parts = Path(path).parts
                for i in range(1, len(parts)):
                    d = "/".join(parts[:i])
                    if d not in dirs:
                        dirs.add(d)
                        ti = tarfile.TarInfo(d)
                        ti.type, ti.mode, ti.uid, ti.gid = tarfile.DIRTYPE, 0o755, owner, owner
                        tar.addfile(ti)
                data = files[path]
                ti = tarfile.TarInfo(path)
                ti.size, ti.mode, ti.uid, ti.gid, ti.mtime = len(data), 0o644, owner, owner, 1700000000
                tar.addfile(ti, io.BytesIO(data))
        r = _run(["docker", "cp", "-a", "-", f"{self.id}:{dest}"], input_bytes=buf.getvalue())
        if r.returncode != 0:
            raise RuntimeError(f"docker cp failed: {r.stderr.decode()[-300:]}")

    def snapshot(self) -> dict[str, bytes]:
        """All regular files under /work (paths relative to /work)."""
        r = _run(["docker", "cp", f"{self.id}:/work", "-"], timeout=120)
        out: dict[str, bytes] = {}
        with tarfile.open(fileobj=io.BytesIO(r.stdout)) as tar:
            for m in tar.getmembers():
                if m.isfile() and m.name.startswith("work/"):
                    rel = m.name[len("work/"):]
                    if "__pycache__/" in rel or rel.startswith(".pytest_cache/"):
                        continue
                    out[rel] = tar.extractfile(m).read()
        return out

    def read_root(self, path: str) -> str:
        r = _run(["docker", "exec", self.id, "cat", path])
        return r.stdout.decode(errors="replace") if r.returncode == 0 else ""

    # commands ----------------------------------------------------------------
    def exec(self, cmd: str, env: dict[str, str] | None = None, timeout: int = CMD_TIMEOUT,
             trace: bool = True) -> ExecResult:
        """Run `cmd` as the agent user (under strace unless trace=False, used only by graders)."""
        self._n += 1
        name = f"c{self._n:04d}-{uuid.uuid4().hex[:6]}"
        args = ["docker", "exec", "-e", f"CVR_CMD={cmd}"]
        for k, v in (env or {}).items():
            args += ["-e", f"{k}={v}"]
        if trace:
            args += [self.id, "strace", "-f", "-qq", "-tt", "-s", "1024", "-y", "-e", f"trace={TRACE_CALLS}",
                     "-o", f"/var/ledger/{name}.trace", "-u", "agent", "/opt/harness/run_cmd.sh"]
        else:
            args = args[:2] + ["-u", "agent"] + args[2:] + [self.id, "/opt/harness/run_cmd.sh"]
        t0 = time.time()
        timed_out = False
        try:
            proc = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout)
            code, out = proc.returncode, proc.stdout
        except subprocess.TimeoutExpired as e:
            timed_out = True
            # kill every agent process (the tracer then exits); root is unaffected
            _run(["docker", "exec", self.id, "python3", "-c", KILL_AGENT])
            code, out = 124, (e.stdout or b"") + f"\n[command timed out after {timeout}s]".encode()
        t1 = time.time()
        trace_text = ""
        if trace:
            trace_text = self.read_root(f"/var/ledger/{name}.trace")
            _run(["docker", "exec", self.id, "rm", "-f", f"/var/ledger/{name}.trace"])
        return ExecResult(code, out.decode("utf-8", errors="replace"), trace_text, timed_out, t0, t1)

    def fileop(self, op: str, path: str, **kw: str) -> ExecResult:
        env = {"CVR_PATH": base64.b64encode(path.encode()).decode()}
        for k, v in kw.items():
            env[f"CVR_{k.upper()}"] = base64.b64encode(v.encode()).decode()
        return self.exec(f"python3 /opt/harness/fileop.py {op}", env=env)

    def ops_log(self) -> list[dict]:
        text = self.read_root("/var/opsstate/log.jsonl")
        return [json.loads(l) for l in text.splitlines() if l.strip()]

    def ops_state(self) -> dict:
        t = self.read_root("/var/opsstate/state.json")
        return json.loads(t) if t else {}


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def encode_files(files: dict[str, bytes]) -> dict[str, str]:
    """Text files as text; anything not valid UTF-8 as 'b64:<base64>' (lossless)."""
    out = {}
    for p, c in files.items():
        try:
            out[p] = c.decode("utf-8")
        except UnicodeDecodeError:
            out[p] = "b64:" + base64.b64encode(c).decode()
    return out


def decode_file(text: str) -> bytes:
    return base64.b64decode(text[4:]) if text.startswith("b64:") else text.encode("utf-8")
