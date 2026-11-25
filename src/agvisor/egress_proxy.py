"""Allowlist HTTP(S) proxy that agvisor runs in a container next to an `egress=` sandbox. Stdlib only, one file: agvisor
passes this source to `python -c` in a stock python image, and imports it to validate `egress=` entries.

Environment: AGVISOR_ALLOW (comma-separated entries), AGVISOR_TOKEN (the proxy password).

Entry grammar, `host[:port]`: port defaults to 80 and 443, `*` = any port.
  example.com      exactly that name         *.example.com   its subdomains, not example.com itself
  1.2.3.4          an IPv4 address           10.0.0.0/24     an IPv4 network
A name only gets through if it resolves to public addresses: a name is not a way to reach 10.x, the host or the cloud
metadata endpoint. Reaching a private address takes an entry that says so (an IP or a network).
`!entry` is a deny: it wins over every allow, and its port defaults to any (`!admin.example.com` shuts out all of it).

The rules live in a file on a tmpfs (RULES_FILE), written at start from AGVISOR_ALLOW. `python -c <this file> edit` changes
them from outside (ops on stdin: `allow ENTRY`, `deny ENTRY`) and signals PID 1 (this proxy), which reloads them on SIGHUP
and closes the open tunnels that the new rules no longer let through.

Credentials: for a host named in AGVISOR_INJECT, a CONNECT to port 443 is not a blind tunnel. The proxy ends the client's
TLS with its own certificate (AGVISOR_TLS, which the client was told to trust), checks the request, sets the configured
headers on it and sends it on over TLS of its own, with the real certificate of that host verified. So the client can
use the credential and never holds it. One request per connection, like plain http. Plain http:// to such a host is refused.
AGVISOR_INJECT: base64 of the JSON {host: {header: value}}; AGVISOR_TLS: base64 of a PEM certificate chain + private key."""
import asyncio
import base64
import hmac
import ipaddress
import json
import os
import re
import signal
import socket
import ssl
import sys
import tempfile
import time
from urllib.parse import urlsplit

PORT = 3128
RULES_FILE = "/run/agvisor/rules"  # one entry per line; on a tmpfs, because the proxy's own root is read-only
_MAX = 256  # concurrent connections; the container's memory is the real limit
_IDLE = 900  # seconds a tunnel may carry no byte in either direction before it is closed: longer than a quiet request is expected to take
_NAME = re.compile(r"(\*\.)?[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)*")
_HEADER = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+")
_NOT_INJECTED = {"host", "content-length", "transfer-encoding", "connection", "keep-alive", "proxy-authorization",
                 "proxy-connection", "upgrade", "te", "trailer"}  # these frame the request or the hop: not ours to set


def parse_rules(entries):
    """[(kind, value, ports)]: kind "net" (value an ip_network) or "name"; ports a tuple, or None for any port."""
    rules = []
    for e in entries:
        host, sep, port = e.lower().partition(":")
        if sep and port != "*" and not (re.fullmatch(r"[0-9]{1,5}", port) and 0 < int(port) < 65536):
            raise ValueError(f"invalid egress entry {e!r}: bad port")
        ports = None if port == "*" else (int(port),) if sep else (80, 443)
        if re.fullmatch(r"[0-9./]+", host):
            try:
                rules.append(("net", ipaddress.ip_network(host, strict=False), ports))
            except ValueError:
                raise ValueError(f"invalid egress entry {e!r}: not an IPv4 address or network") from None
        elif _NAME.fullmatch(host) and len(host) <= 253:
            rules.append(("name", host, ports))
        else:
            raise ValueError(f"invalid egress entry {e!r}: expected host[:port], *.host[:port], an IPv4 address or network")
    return rules


def parse_policy(entries):
    """(allow, deny): parse_rules' tuples for the entries that let a host through, and for the `!entry` ones that shut it out.
    A deny's port defaults to any port, not to 80 and 443: `!example.com` is all of it."""
    allow, deny = [], []
    for e in entries:
        if not e.startswith("!"):
            allow += parse_rules([e])
            continue
        (kind, value, ports), = parse_rules([e[1:]])
        deny.append((kind, value, ports if ":" in e else None))
    return allow, deny


def parse_inject(inject):
    """{host: {header: value}}, checked, with the hosts lowercased. Plain host names only (no wildcard, address or port): a
    credential goes to exactly the host it was given for. A value never appears in an error message."""
    if not isinstance(inject, dict) or not inject:
        raise ValueError("inject must be a dict {host: {header: value}}")
    out = {}
    for host, headers in inject.items():
        h = host.lower() if isinstance(host, str) else ""
        if not _NAME.fullmatch(h) or h.startswith("*.") or re.fullmatch(r"[0-9.]+", h) or len(h) > 253 or h in out:
            raise ValueError(f"invalid inject host {host!r}: a plain host name, listed once (no wildcard, address or port)")
        if not isinstance(headers, dict) or not headers:
            raise ValueError(f"inject[{host!r}] must be a dict {{header: value}}")
        seen = out[h] = {}
        for name, value in headers.items():
            if not isinstance(name, str) or not _HEADER.fullmatch(name) or name.lower() in _NOT_INJECTED:
                raise ValueError(f"invalid inject header name {name!r} for {host}")
            if name.lower() in {n.lower() for n in seen}:
                raise ValueError(f"inject header {name!r} for {host} is given twice")
            if not isinstance(value, str) or any(c in value for c in "\r\n\0") or not value.isascii():
                raise ValueError(f"inject header {name!r} for {host}: the value must be ASCII text on one line")
            seen[name] = value
    return out


def match(rules, host, port):
    """"net" or "name" if a rule lets (host, port) through, else None. `host` is lowercase, without a trailing dot."""
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    for kind, val, ports in rules:
        if ports is not None and port not in ports:
            continue
        if ip is not None:
            if kind == "net" and ip in val:  # an address of the other IP version is never "in"
                return "net"
        elif kind == "name" and (host == val or val.startswith("*.") and host.endswith(val[1:])):
            return "name"


def check(allow, deny, host, port):
    """"net" or "name" if (host, port) may be reached: some allow rule lets it through and no deny rule names it."""
    if not match(deny, host, port):
        return match(allow, host, port)


def drop(tunnels, allow, deny):
    """Close the open tunnels that the rules no longer let through. Their pipes end as if the far side had hung up."""
    for host, port, cw, uw in list(tunnels):
        if not check(allow, deny, host, port):
            cw.close()
            uw.close()


def edit(ops, path=RULES_FILE, pid=1):
    """Apply [("allow" | "deny", entry)] to the rules file in one step, then tell the proxy (PID 1) to reload. allow(x) removes
    `x` and `!x` and adds `x`; deny(x) removes both and adds `!x`, so the last word on an entry is the one that stands.
    ValueError, with nothing written, if the result isn't valid. The file is written whole and renamed over, so the proxy
    never reads half of it."""
    with open(path) as f:
        entries = f.read().split()
    for op, entry in ops:
        if op not in ("allow", "deny"):
            raise ValueError(f"unknown operation {op!r}: allow or deny")
        entry = entry.lower().removeprefix("!")
        entries = [e for e in entries if e not in (entry, "!" + entry)] + [entry if op == "allow" else "!" + entry]
    parse_policy(entries)
    with open(path + ".new", "w") as f:
        f.write("".join(e + "\n" for e in entries))
    os.replace(path + ".new", path)
    os.kill(pid, signal.SIGHUP)


def _public(addr):
    ip = ipaddress.ip_address(addr.split("%")[0])
    return (getattr(ip, "ipv4_mapped", None) or ip).is_global  # ::ffff:10.0.0.1 is 10.0.0.1


class _Denied(Exception):
    pass


async def _dial(host, port, kind):
    """Connect to what we checked, not to what a second lookup might say: no DNS rebinding."""
    if kind == "net":
        addrs = [host]
    else:
        infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
        addrs = [i[4][0] for i in infos if _public(i[4][0])]
        if not addrs:
            raise _Denied(f"{host} resolves to no public address")
    err = OSError("no address")
    for a in addrs:
        try:
            return await asyncio.wait_for(asyncio.open_connection(a, port), 10)
        except OSError as e:  # includes TimeoutError
            err = e
    raise err


async def _reply(w, code, why, body="", extra=""):
    b = body.encode()
    w.write(f"HTTP/1.1 {code} {why}\r\n{extra}Content-Type: text/plain\r\nContent-Length: {len(b)}\r\nConnection: close\r\n\r\n".encode() + b)
    await w.drain()


async def _pipe(r, w, seen):
    """r -> w until r ends. seen[0] is when either direction of this tunnel last carried a byte: one that stays quiet
    for _IDLE seconds is closed, so a stalled connection doesn't hold a socket until the sandbox goes."""
    try:
        while True:
            try:
                data = await asyncio.wait_for(r.read(65536), max(seen[0] + _IDLE - time.monotonic(), 0))
            except asyncio.TimeoutError:
                if time.monotonic() - seen[0] < _IDLE:  # the other direction moved meanwhile: a download says nothing back
                    continue
                w.close()
                return
            if not data:
                break
            seen[0] = time.monotonic()
            w.write(data)
            await w.drain()
        if w.can_write_eof():
            w.write_eof()  # the peer half-closed: pass it on, the answer may still be coming
        else:
            w.close()  # a TLS stream can't be half-closed: the request is over, or the answer is
    except OSError:
        w.close()


async def _mitm(cr, cw, uw, host, creds, server_ctx, upstream_ctx):
    """CONNECT to a host with credentials: end the client's TLS here, read its one request, and send it on over TLS of our
    own with `creds` set. False (and the client told why) if the request isn't one to give a credential to."""
    await cw.drain()  # the 200 must be out before the client starts its handshake
    await asyncio.wait_for(cw.start_tls(server_ctx), 15)
    await asyncio.wait_for(uw.start_tls(upstream_ctx, server_hostname=host), 15)  # verifies the certificate, and that it is `host`'s
    head = await asyncio.wait_for(cr.readuntil(b"\r\n\r\n"), 15)
    first, *lines = head[:-4].decode("latin-1").split("\r\n")
    parts = first.split(" ")
    hdr = [(l.partition(":")[0].strip().lower(), l.partition(":")[2].strip(), l) for l in lines]
    hosts = [v.lower().removesuffix(":443").rstrip(".") for k, v, _ in hdr if k == "host"]
    # origin-form only: an absolute URL in the request line could steer a shared front end to another site, with our credential
    if len(parts) != 3 or not parts[1].startswith("/") or not parts[2].startswith("HTTP/1."):
        await _reply(cw, 400, "Bad Request", "agvisor egress: not an HTTP/1.x request with a path")
    elif any(l[:1] in " \t" or ":" not in l for l in lines):
        await _reply(cw, 400, "Bad Request", "agvisor egress: header folding is not accepted on a host with credentials")
    elif hosts != [host]:
        await _reply(cw, 403, "Forbidden", f"agvisor egress: the Host header must be {host}")
    else:
        drop = {"connection", "keep-alive", "proxy-connection", "proxy-authorization"} | {n.lower() for n in creds}
        uw.write((first + "\r\n" + "".join(l + "\r\n" for k, _, l in hdr if k not in drop)
                  + "".join(f"{n}: {v}\r\n" for n, v in creds.items()) + "Connection: close\r\n\r\n").encode("latin-1"))
        return True
    return False


def _server_ctx(pem):
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.set_alpn_protocols(["http/1.1"])  # no h2: the request has to be readable here
    with tempfile.NamedTemporaryFile("w", suffix=".pem") as f:  # the key touches only a tmpfs, and not for long
        f.write(pem)
        f.flush()
        ctx.load_cert_chain(f.name)
    return ctx


async def serve(rules, token, host="0.0.0.0", port=PORT, inject=None, server_ctx=None, upstream_ctx=None, deny=(), tunnels=None):
    """`rules` and `deny` are parse_policy()'s lists: change them in place and the next connection follows. `tunnels` is a
    set that fills with (host, port, client writer, upstream writer) of every open tunnel, for drop().
    `inject`: parse_inject()'s result; then `server_ctx` (what the client's TLS ends in) and `upstream_ctx` (how the proxy
    verifies the real host) are needed too."""
    tunnels = set() if tunnels is None else tunnels
    want = b"Basic " + base64.b64encode(f"agvisor:{token}".encode())
    live = 0
    inject = inject or {}
    if inject and not (server_ctx and upstream_ctx):
        raise ValueError("inject needs server_ctx and upstream_ctx")

    async def handle(cr, cw):
        nonlocal live
        live += 1
        try:
            if live > _MAX:
                return await _reply(cw, 503, "Service Unavailable", "too many connections")
            head = await asyncio.wait_for(cr.readuntil(b"\r\n\r\n"), 15)
            first, *lines = head[:-4].decode("latin-1").split("\r\n")
            hdr = [(l.partition(":")[0].strip().lower(), l.partition(":")[2].strip(), l) for l in lines]
            if not hmac.compare_digest(next((v for k, v, _ in hdr if k == "proxy-authorization"), "").encode("latin-1"), want):
                return await _reply(cw, 407, "Proxy Authentication Required", extra='Proxy-Authenticate: Basic realm="agvisor"\r\n')
            try:
                method, target, version = first.split(" ")
                if method == "CONNECT":
                    host, _, p = target.rpartition(":")
                    host, port, path = host.strip("[]"), int(p), None
                else:
                    u = urlsplit(target)
                    if u.scheme != "http":
                        raise ValueError("a proxy is asked for http:// URLs or CONNECT")
                    host, port = u.hostname, u.port or 80
                    path = (u.path or "/") + (f"?{u.query}" if u.query else "")
                host = host.lower().rstrip(".")
                if not (_NAME.fullmatch(host) or ipaddress.ip_address(host)):  # a literal address raises ValueError if it isn't one
                    raise ValueError("bad host")
            except (ValueError, AttributeError):
                return await _reply(cw, 400, "Bad Request", "agvisor egress proxy: send CONNECT host:port or an absolute http:// URL")
            kind = check(rules, deny, host, port)
            try:
                if not kind:
                    raise _Denied(f"{host}:{port} is not in this sandbox's egress allowlist")
                if path is not None and host in inject:  # a credential must not cross the network in the clear
                    raise _Denied(f"{host} gets credentials injected, which happens over https only: use https://")
                ur, uw = await _dial(host, port, kind)
            except _Denied as e:
                print("deny", host, port, flush=True)
                return await _reply(cw, 403, "Forbidden", f"agvisor egress: {e}")
            except OSError as e:
                print("fail", host, port, e, flush=True)
                return await _reply(cw, 502, "Bad Gateway", f"agvisor egress: cannot connect to {host}:{port}: {e}")
            print("allow", host, port, flush=True)
            tunnel = host, port, cw, uw
            tunnels.add(tunnel)
            try:
                if path is None:
                    cw.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
                    if host in inject and port == 443 and not await _mitm(cr, cw, uw, host, inject[host], server_ctx, upstream_ctx):
                        return
                else:  # one request per connection: `Connection: close`, so a later request can't be sent anywhere else
                    drop = {"proxy-authorization", "proxy-connection", "connection", "keep-alive"}
                    uw.write(f"{method} {path} {version}\r\n".encode("latin-1")
                             + "".join(f"{l}\r\n" for k, _, l in hdr if k not in drop).encode("latin-1") + b"Connection: close\r\n\r\n")
                seen = [time.monotonic()]
                await asyncio.gather(_pipe(cr, uw, seen), _pipe(ur, cw, seen))
            finally:
                tunnels.discard(tunnel)
                uw.close()
        except (OSError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
            pass  # a client that vanished or sent junk
        finally:
            live -= 1
            cw.close()

    return await asyncio.start_server(handle, host, port, limit=2**14)


async def main():
    inject = server_ctx = upstream_ctx = None
    if os.environ.get("AGVISOR_INJECT"):
        inject = parse_inject(json.loads(base64.b64decode(os.environ["AGVISOR_INJECT"])))
        server_ctx = _server_ctx(base64.b64decode(os.environ["AGVISOR_TLS"]).decode())
        upstream_ctx = ssl.create_default_context()
    path = os.environ.get("AGVISOR_RULES", RULES_FILE)  # ponytail: the env var is only for the tests
    entries = [e.lower() for e in os.environ["AGVISOR_ALLOW"].split(",")]
    allow, deny = parse_policy(entries)
    tunnels = set()

    def reload():  # SIGHUP: edit() wrote a new file
        try:
            with open(path) as f:
                new_allow, new_deny = parse_policy(f.read().split())
        except (OSError, ValueError) as e:  # keep the rules we have: a bad file must not open the sandbox up, or shut it
            print("reload failed, keeping the old rules:", e, flush=True)
            return
        allow[:], deny[:] = new_allow, new_deny
        drop(tunnels, allow, deny)
        print("reload", len(allow), "allow", len(deny), "deny", flush=True)

    asyncio.get_running_loop().add_signal_handler(signal.SIGHUP, reload)
    server = await serve(allow, os.environ["AGVISOR_TOKEN"], port=int(os.environ.get("AGVISOR_PORT", PORT)),  # ponytail: the port is only for the tests
                         inject=inject, server_ctx=server_ctx, upstream_ctx=upstream_ctx, deny=deny, tunnels=tunnels)
    with open(path, "w") as f:  # what edit() changes and the host reads back; it exists once we listen: the host waits for it
        f.write("".join(e + "\n" for e in entries))
    await server.serve_forever()


if __name__ == "__main__":
    if sys.argv[1:2] == ["edit"]:  # `python -c <this file> edit`, run by Sandbox.allow() / deny() inside the proxy's container
        try:
            edit([tuple(l.split(" ", 1)) for l in sys.stdin.read().splitlines() if l], pid=int(os.environ.get("AGVISOR_PID", 1)),
                 path=os.environ.get("AGVISOR_RULES", RULES_FILE))
        except ValueError as e:
            sys.exit(f"agvisor egress: {e}")
    else:
        asyncio.run(main())
