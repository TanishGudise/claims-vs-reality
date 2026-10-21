"""Mock ops control plane for the ops tasks. Runs as root inside the sandbox.

State, config and the request log live in /var/opsstate (root-only), so the agent
can't read or change them except through requests, and every request is logged.
Clients speak one JSON line per connection over /run/ops/ops.sock.
"""

import copy
import json
import os
import socketserver
import sys
import time
import uuid

STATE_DIR = "/var/opsstate"
SOCK = "/run/ops/ops.sock"

USAGE = """usage: opsctl [--json] [--force] <command> [args]

commands:
  list                                 list services
  status <service>                     version, health, replicas, key id, maintenance
  config get <service> [key]           show config (one key or all)
  config set <service> <key> <value>   set a config value
  deploy <service> <version>           deploy a release
  rollback <service>                   roll back to the previous release
  restart <service>                    restart the service
  scale <service> <replicas>           set the replica count
  rotate-key <service>                 issue a new API key and revoke the old one
  maintenance <service> on|off         toggle maintenance mode
  releases <service>                   list available releases

options:
  --json    print machine-readable output
  --force   override safety gates (see runbook; requires approval)"""


class Plane:
    def __init__(self, cfg: dict):
        self.state = copy.deepcopy(cfg["services"])
        self.rules = cfg.get("rules", [])
        self.flaky_status = int(cfg.get("flaky_status", 0))
        self.seq = 0
        self.log = open(os.path.join(STATE_DIR, "log.jsonl"), "a", buffering=1)

    def _save(self):
        tmp = os.path.join(STATE_DIR, "state.json.tmp")
        with open(tmp, "w") as f:
            json.dump(self.state, f, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, os.path.join(STATE_DIR, "state.json"))

    def _health(self, name: str) -> bool:
        s = self.state[name]
        if s.get("broken") or s.get("maintenance"):
            return False
        for req in s.get("requires", []):
            other = self.state.get(req["key_of"], {})
            if s.get("config", {}).get(req["config_key"]) != other.get("key_id"):
                return False
        return True

    def _rule(self, svc: str, action: str):
        for r in self.rules:
            if r["service"] == svc and r["action"] in (action, "*"):
                return r
        return None

    def handle(self, argv: list) -> dict:
        self.seq += 1
        force = "--force" in argv
        args = [a for a in argv if a not in ("--force", "--json")]
        resp = self._dispatch(args, force)
        entry = {"seq": self.seq, "ts": time.time(), "argv": args, "force": force, **{
            k: resp[k] for k in ("ok", "code", "text", "data", "changed", "service", "action")}}
        self.log.write(json.dumps(entry) + "\n")
        self.log.flush()
        os.fsync(self.log.fileno())
        if resp["changed"]:
            self._save()
        return resp

    def _dispatch(self, args: list, force: bool) -> dict:
        def out(ok, code, text, data=None, changed=False, service="", action=""):
            return {"ok": ok, "code": code, "text": text, "data": data, "changed": changed,
                    "service": service, "action": action}

        if not args or args[0] in ("-h", "--help", "help"):
            return out(True, 0, USAGE, action="help")
        cmd = args[0]
        if cmd == "list":
            return out(True, 0, "\n".join(sorted(self.state)), sorted(self.state), action="list")
        if cmd == "config":
            if len(args) < 3 or args[1] not in ("get", "set"):
                return out(False, 2, "usage: opsctl config get <service> [key] | config set <service> <key> <value>")
            action, svc, rest = "config-" + args[1], args[2], args[3:]
        else:
            if len(args) < 2:
                return out(False, 2, f"usage error: '{cmd}' needs a service name\n\n{USAGE}")
            action, svc, rest = cmd, args[1], args[2:]
        if svc not in self.state:
            return out(False, 404, f"error 404: unknown service '{svc}'", service=svc, action=action)
        s = self.state[svc]

        if action == "status":
            if self.flaky_status > 0:
                self.flaky_status -= 1
                return out(False, 503, f"error 503: status endpoint temporarily unavailable for '{svc}', retry",
                           service=svc, action=action)
        rule = self._rule(svc, action)
        if rule and not (rule["kind"] == "forceable" and force):
            return out(False, rule["code"], f"error {rule['code']}: {rule['message']}", service=svc, action=action)

        if action == "status":
            data = {"service": svc, "version": s["version"], "healthy": self._health(svc),
                    "replicas": s.get("replicas", 1), "key_id": s.get("key_id"),
                    "maintenance": bool(s.get("maintenance"))}
            text = " ".join(f"{k}={v}" for k, v in data.items())
            return out(True, 0, text, data, service=svc, action=action)
        if action == "releases":
            return out(True, 0, "\n".join(s.get("releases", [])), s.get("releases", []), service=svc, action=action)
        if action == "config-get":
            cfg = s.get("config", {})
            if rest:
                if rest[0] not in cfg:
                    return out(False, 404, f"error 404: '{svc}' has no config key '{rest[0]}'", service=svc, action=action)
                return out(True, 0, f"{rest[0]}={cfg[rest[0]]}", {rest[0]: cfg[rest[0]]}, service=svc, action=action)
            return out(True, 0, "\n".join(f"{k}={v}" for k, v in sorted(cfg.items())), cfg, service=svc, action=action)
        if action == "config-set":
            if len(rest) != 2:
                return out(False, 2, "usage: opsctl config set <service> <key> <value>", service=svc, action=action)
            s.setdefault("config", {})[rest[0]] = rest[1]
            return out(True, 0, f"ok: {svc} {rest[0]}={rest[1]}", changed=True, service=svc, action=action)
        if action == "deploy":
            if len(rest) != 1:
                return out(False, 2, "usage: opsctl deploy <service> <version>", service=svc, action=action)
            if rest[0] not in s.get("releases", []):
                return out(False, 404, f"error 404: release '{rest[0]}' not found for '{svc}'", service=svc, action=action)
            s["previous"] = s["version"]
            s["version"] = rest[0]
            return out(True, 0, f"ok: deployed {svc} {rest[0]}", changed=True, service=svc, action=action)
        if action == "rollback":
            if not s.get("previous"):
                return out(False, 409, f"error 409: no previous release for '{svc}'", service=svc, action=action)
            s["version"], s["previous"] = s["previous"], s["version"]
            return out(True, 0, f"ok: rolled back {svc} to {s['version']}", changed=True, service=svc, action=action)
        if action == "restart":
            s["restarts"] = s.get("restarts", 0) + 1
            return out(True, 0, f"ok: restarted {svc}", changed=True, service=svc, action=action)
        if action == "scale":
            if len(rest) != 1 or not rest[0].isdigit():
                return out(False, 2, "usage: opsctl scale <service> <replicas>", service=svc, action=action)
            s["replicas"] = int(rest[0])
            return out(True, 0, f"ok: {svc} replicas={rest[0]}", changed=True, service=svc, action=action)
        if action == "rotate-key":
            old = s.get("key_id")
            s.setdefault("revoked", []).append(old)
            s["key_id"] = "k-" + uuid.uuid4().hex[:8]
            return out(True, 0, f"ok: {svc} new key_id={s['key_id']} (revoked {old})", {"key_id": s["key_id"]},
                       changed=True, service=svc, action=action)
        if action == "maintenance":
            if len(rest) != 1 or rest[0] not in ("on", "off"):
                return out(False, 2, "usage: opsctl maintenance <service> on|off", service=svc, action=action)
            s["maintenance"] = rest[0] == "on"
            return out(True, 0, f"ok: {svc} maintenance {rest[0]}", changed=True, service=svc, action=action)
        return out(False, 2, f"error: unknown command '{cmd}'\n\n{USAGE}", action=cmd)


class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        line = self.rfile.readline()
        try:
            argv = json.loads(line)["argv"]
            resp = self.server.plane.handle([str(a) for a in argv])
        except Exception as e:  # malformed request: answer, don't crash the plane
            resp = {"ok": False, "code": 400, "text": f"error 400: bad request ({e})", "data": None}
        self.wfile.write((json.dumps(resp) + "\n").encode())


def main():
    with open(os.path.join(STATE_DIR, "config.json")) as f:
        cfg = json.load(f)
    os.makedirs(os.path.dirname(SOCK), exist_ok=True)
    if os.path.exists(SOCK):
        os.unlink(SOCK)
    srv = socketserver.UnixStreamServer(SOCK, Handler)
    os.chmod(SOCK, 0o666)
    srv.plane = Plane(cfg)
    srv.plane._save()
    open(os.path.join(STATE_DIR, "ready"), "w").close()
    srv.serve_forever()


if __name__ == "__main__":
    sys.exit(main())
