"""Network guard. install() allows loopback only (tests, dry runs). install(allow=("api.anthropic.com",))
also allows those hosts and the addresses they resolve to (paid steps). Anything else raises."""

import socket

_real_connect = socket.socket.connect
_real_create = socket.create_connection
_real_gai = socket.getaddrinfo
_LOOPBACK = {"127.0.0.1", "::1", "localhost"}


def install(allow: tuple[str, ...] = ()):
    hosts = set(_LOOPBACK) | set(allow)
    addrs = set(_LOOPBACK)

    def norm(h):
        return h.decode("idna") if isinstance(h, (bytes, bytearray)) else h  # async clients pass bytes

    def check(addr):
        host = norm(addr[0] if isinstance(addr, tuple) else addr)
        if isinstance(host, str) and not host.startswith("/") and host not in hosts and host not in addrs:
            raise ConnectionRefusedError(f"netguard: blocked connection to {host!r}")

    def getaddrinfo(host, *a, **k):
        host = norm(host)
        if host is not None and host not in hosts:
            raise socket.gaierror(f"netguard: blocked DNS lookup for {host!r}")
        res = _real_gai(host, *a, **k)
        for r in res:
            addrs.add(r[4][0])
        return res

    def connect(self, addr):
        if self.family in (socket.AF_INET, socket.AF_INET6):
            check(addr)
        return _real_connect(self, addr)

    def create_connection(addr, *a, **k):
        check(addr)
        return _real_create(addr, *a, **k)

    socket.getaddrinfo = getaddrinfo
    socket.socket.connect = connect
    socket.create_connection = create_connection
