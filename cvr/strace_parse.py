"""Parse `strace -f -tt -y` output into file and exec events.

Only successful syscalls produce events, except opens, which are kept with ok=False
so callers can tell "tried to read a missing file" from "never looked".
"""

from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass, field

_LINE = re.compile(r"^(?P<pid>\d+)\s+(?P<ts>\d\d:\d\d:\d\d\.\d+)\s+(?P<rest>.*)$")
_CALL = re.compile(r"^(?P<name>\w+)\((?P<args>.*)\)\s+=\s+(?P<ret>.*)$")
_RESUMED = re.compile(r"^<\.\.\. (?P<name>\w+) resumed>(?P<tail>.*)$")
_STR = re.compile(r'"((?:[^"\\]|\\.)*)"(\.\.\.)?')
_ANNOT = re.compile(r"<(?P<path>/[^>]*)>")


@dataclass
class Event:
    pid: int
    ts: str
    kind: str  # exec | open | unlink | rename | truncate | link | symlink
    path: str = ""
    path2: str = ""  # rename/link target
    argv: list[str] = field(default_factory=list)
    flags: str = ""
    ok: bool = True

    @property
    def writes(self) -> bool:
        """Truncating, appending or write-only opens. A read/write open without those (SQLite
        opens its database O_RDWR|O_CREAT just to read it) is not a write by itself; changes it
        makes still show up in the final hash comparison."""
        f = self.flags
        return self.kind == "open" and ("O_TRUNC" in f or "O_APPEND" in f or "O_WRONLY" in f)


def _unescape(s: str) -> str:
    return bytes(s, "utf-8").decode("unicode_escape", errors="replace") if "\\" in s else s


def _split_args(args: str) -> list[str]:
    """Split a syscall argument list on top-level commas (ignores commas in strings/[]/{}/<>)."""
    out, depth, cur, in_str, esc = [], 0, [], False, False
    for ch in args:
        if in_str:
            cur.append(ch)
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch in "[{<":
            depth += 1
        elif ch in "]}>":
            depth -= 1
        elif ch == "," and depth == 0:
            out.append("".join(cur).strip())
            cur = []
            continue
        cur.append(ch)
    if cur:
        out.append("".join(cur).strip())
    return out


def _dirfd_base(arg: str) -> str:
    m = _ANNOT.search(arg)
    return m.group("path") if m else ""


def _path_arg(arg: str, base: str) -> str:
    m = _STR.search(arg)
    if not m:
        return ""
    p = _unescape(m.group(1))
    if not p.startswith("/") and base:
        p = posixpath.join(base, p)
    return posixpath.normpath(p)


def _ok(ret: str) -> bool:
    return not ret.lstrip().startswith("-1")


def _ret_path(ret: str) -> str:
    m = _ANNOT.search(ret)
    return m.group("path") if m else ""


def parse(text: str) -> list[Event]:
    pending: dict[int, tuple[str, str]] = {}
    events: list[Event] = []
    for raw in text.splitlines():
        m = _LINE.match(raw)
        if not m:
            continue
        pid, ts, rest = int(m.group("pid")), m.group("ts"), m.group("rest")
        if rest.endswith("<unfinished ...>"):
            pending[pid] = (ts, rest[: -len("<unfinished ...>")].rstrip())
            continue
        r = _RESUMED.match(rest)
        if r:
            start = pending.pop(pid, None)
            if start is None:
                continue
            ts, head = start
            rest = head + r.group("tail")
        c = _CALL.match(rest)
        if not c:
            continue
        ev = _event(pid, ts, c.group("name"), _split_args(c.group("args")), c.group("ret"))
        if ev is not None:
            events.append(ev)
    return events


def _event(pid: int, ts: str, name: str, args: list[str], ret: str) -> Event | None:
    ok = _ok(ret)
    if name in ("execve", "execveat"):
        if not ok:
            return None
        i = 1 if name == "execveat" else 0
        path = _path_arg(args[i], "") if len(args) > i else ""
        argv = [_unescape(s) for s, _ in _STR.findall(args[i + 1])] if len(args) > i + 1 else []
        return Event(pid, ts, "exec", path=path, argv=argv)
    if name in ("open", "openat", "creat"):
        if name == "openat":
            base, parg, flags = _dirfd_base(args[0]), args[1], args[2] if len(args) > 2 else ""
        else:
            base, parg = "", args[0]
            flags = "O_CREAT|O_WRONLY|O_TRUNC" if name == "creat" else (args[1] if len(args) > 1 else "")
        path = _ret_path(ret) if ok else ""
        return Event(pid, ts, "open", path=path or _path_arg(parg, base), flags=flags, ok=ok)
    if not ok:
        return None
    if name in ("unlink", "unlinkat"):
        base, parg = (_dirfd_base(args[0]), args[1]) if name == "unlinkat" else ("", args[0])
        return Event(pid, ts, "unlink", path=_path_arg(parg, base))
    if name in ("rename", "renameat", "renameat2"):
        if name == "rename":
            src, dst = _path_arg(args[0], ""), _path_arg(args[1], "")
        else:
            src = _path_arg(args[1], _dirfd_base(args[0]))
            dst = _path_arg(args[3], _dirfd_base(args[2]))
        return Event(pid, ts, "rename", path=src, path2=dst)
    if name in ("truncate", "ftruncate"):
        path = _path_arg(args[0], "") if name == "truncate" else _dirfd_base(args[0])
        return Event(pid, ts, "truncate", path=path)
    if name in ("link", "linkat", "symlink", "symlinkat"):
        kind = "symlink" if name.startswith("symlink") else "link"
        if name == "link":
            src, dst = _path_arg(args[0], ""), _path_arg(args[1], "")
        elif name == "linkat":
            src, dst = _path_arg(args[1], _dirfd_base(args[0])), _path_arg(args[3], _dirfd_base(args[2]))
        elif name == "symlink":
            src, dst = _path_arg(args[0], ""), _path_arg(args[1], "")
        else:
            src, dst = _path_arg(args[0], ""), _path_arg(args[2], _dirfd_base(args[1]))
        return Event(pid, ts, kind, path=src, path2=dst)
    return None
