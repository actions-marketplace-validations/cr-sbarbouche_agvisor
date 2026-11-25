"""Needs a docker host with real gVisor and internet (the `gvisor` job in ci.yml has both): the egress rules change while the sandbox
runs (allow, deny, `!entry`), what it was refused is listed, open tunnels are closed, and the CLI and MCP tool see the same.

    AGVISOR_HOST=ssh://user@vm AGVISOR_IMAGE=python:3.12-slim uv run --extra mcp python tests/test_gvisor_egress_live.py

AGVISOR_HOST unset -> docker on this machine. AGVISOR_IMAGE: python 3.12+, with pip, already on that host: it is the sandbox image here and
the proxy image (python:3.12-slim) must be on the host too.
"""
import os
import shlex
import time

from agvisor import cli, create_sandbox

host, image = os.environ.get("AGVISOR_HOST"), os.environ.get("AGVISOR_IMAGE")
FETCH = ("import urllib.request as u, urllib.error as e\n"
         "try: print(u.urlopen({!r}, timeout=20).status)\n"
         "except e.HTTPError as x: print(x.code, x.read().decode()[:120])\n"  # plain http: the proxy's own 403 says "agvisor egress: ..."
         "except Exception as x: print('error', x)")  # https: "Tunnel connection failed: 403 Forbidden"


def status(sb, url):
    return sb.exec(f"python3 -c {shlex.quote(FETCH.format(url))}", timeout=60).stdout.strip()


def refused(sb, url):  # the proxy said no (not the site: example.org may answer 403 to a client it doesn't like)
    out = status(sb, url)
    return "agvisor egress" in out or "Tunnel connection failed: 403" in out


def reached(sb, url):  # the request went through to the site, whatever the site then said
    out = status(sb, url)
    assert not refused(sb, url) and not out.startswith("error"), f"{url}: {out}"
    return True


def until(cond, what, seconds=20):
    end = time.monotonic() + seconds
    while not (v := cond()):
        assert time.monotonic() < end, f"{what} never happened"
        time.sleep(0.5)
    return v


# 1. the use the feature is for: pip fails, the user allows pypi.org, it works at once, in the same sandbox
with create_sandbox(image, host=host, egress=["example.com"], lifetime=900) as sb:
    assert sb.egress_rules() == ["example.com"]
    r = sb.exec("pip install --quiet --no-cache-dir --retries 0 --timeout 10 --target /tmp/t six", timeout=90)
    assert r.returncode != 0, "pip got through an allowlist that does not have pypi.org"
    assert "pypi.org:443" in sb.egress_denied(), sb.egress_denied()  # the user can see what it needed
    sb.allow("pypi.org", "*.pythonhosted.org")
    assert sb.egress_rules() == ["example.com", "pypi.org", "*.pythonhosted.org"]
    assert "pypi.org:443" not in sb.egress_denied()  # allowed now: no longer a thing to ask about
    r = sb.exec("pip install --quiet --no-cache-dir --target /tmp/t six && ls /tmp/t | head -2", timeout=120)
    assert r.returncode == 0 and "six" in r.stdout, (r.stdout, r.stderr)
    print("pip install: refused, allow() on the running sandbox, then it works")

# 2. '!' carves a name out of a wildcard; deny() takes effect on new connections at once; allow() undoes it
with create_sandbox(image, host=host, egress=["example.com", "*.example.org", "!www.example.org"], lifetime=900) as sb:
    assert sb.egress_rules() == ["example.com", "*.example.org", "!www.example.org"]
    assert reached(sb, "https://example.com/")
    assert refused(sb, "https://www.example.org/")  # the wildcard allows it, the deny wins
    assert refused(sb, "https://example.org/")  # `*.example.org` is not example.org itself
    assert refused(sb, "http://www.example.org:80/")  # whatever the port
    assert sb.egress_denied() == ["www.example.org:443", "example.org:443", "www.example.org:80"], sb.egress_denied()  # in the order it tried

    sb.allow("example.org", "www.example.org")  # un-denies the first
    assert sb.egress_rules() == ["example.com", "*.example.org", "example.org", "www.example.org"]
    assert reached(sb, "https://www.example.org/") and reached(sb, "https://example.org/")
    assert sb.egress_denied() == []

    sb.deny("example.com")
    assert refused(sb, "https://example.com/") and "example.com:443" in sb.egress_denied()
    assert sb.egress_rules()[-1] == "!example.com"

    # a tunnel that is open when its host is denied is closed: a download in progress doesn't outlive the decision
    sb.allow("example.com")
    hold = r'''
import os, socket, base64, urllib.parse as p
u = p.urlsplit(os.environ["HTTPS_PROXY"])
s = socket.create_connection((u.hostname, u.port), timeout=20)
auth = base64.b64encode(f"{u.username}:{u.password}".encode()).decode()
s.sendall(f"CONNECT example.com:443 HTTP/1.1\r\nHost: example.com:443\r\nProxy-Authorization: Basic {auth}\r\n\r\n".encode())
print("proxy says:", s.recv(100).split(b"\r\n")[0].decode(), flush=True)
s.settimeout(120)
try:
    print("closed" if s.recv(100) == b"" else "data", flush=True)
except OSError as e:
    print("closed", e, flush=True)
'''
    job = sb.spawn(f"python3 -c {shlex.quote(hold)}", timeout=150)
    until(lambda: b"200 Connection established" in job.read(), "the tunnel")
    time.sleep(1)
    assert job.poll() is None, "the tunnel closed by itself"
    t0 = time.monotonic()
    sb.deny("example.com")
    assert job.wait(timeout=30) == 0 and b"closed" in job.read(), job.read()
    print(f"open tunnel closed {time.monotonic() - t0:.1f} s after deny()")

    # 3. the CLI does the same on the running sandbox, from another process
    name, extra = sb.name, (["--host", host] if host else [])
    assert cli.main(["allow", name, "example.com", *extra]) == 0 and reached(sb, "https://example.com/")
    assert cli.main(["deny", name, "example.com", *extra]) == 0 and refused(sb, "https://example.com/")
    assert cli.main(["egress", name, *extra]) == 0 and cli.main(["egress", name, "--denied", *extra]) == 0
    for bad in (["allow", name, "not an entry", *extra], ["allow", name, "x.org:0", *extra]):
        try:
            cli.main(bad)
        except SystemExit as e:
            assert e.code == 2
        else:
            raise AssertionError(f"accepted {bad}")

# 4. the proxy is the sandbox's own: after stop() there is nothing to change
try:
    sb.allow("example.com")
except Exception as e:
    assert type(e).__name__ == "SandboxGone", e
else:
    raise AssertionError("a stopped sandbox accepted a rule")

# 5. the MCP server: the agent sees what was refused, and can't change it
os.environ.update(AGVISOR_EGRESS="example.com", AGVISOR_IDLE_TTL="300", AGVISOR_LIFETIME="900")
from agvisor import mcp_server as m  # noqa: E402

try:
    assert m.egress_blocked() == "nothing was refused"
    assert "403" in m.run(f"python3 -c {shlex.quote(FETCH.format('https://example.org/'))}")  # refused
    assert m.egress_blocked() == "example.org:443"
    assert not hasattr(m, "allow")
    name = m._sb.name
    assert cli.main(["allow", name, "example.org", *extra]) == 0  # the user, from a shell
    assert m.egress_blocked() == "nothing was refused"
    out = m.run(f"python3 -c {shlex.quote(FETCH.format('https://example.org/'))}")
    assert "Tunnel connection failed" not in out and "agvisor egress" not in out, out  # through the proxy now
finally:
    m.reset()
print("ok")
