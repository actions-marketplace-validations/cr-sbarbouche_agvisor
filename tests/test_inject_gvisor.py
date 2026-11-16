"""Needs a docker host with real gVisor and internet (the `gvisor` job in ci.yml has both): create_sandbox(inject=) gives a
sandbox a credential it can use and can't read. Run with the inject extra:

    AGVISOR_HOST=ssh://user@vm AGVISOR_IMAGE=python:3.12-slim uv run --extra inject python tests/test_inject_gvisor.py

AGVISOR_HOST unset -> docker on this machine. AGVISOR_IMAGE: python 3.12+ on a Debian base (it has a CA bundle), already on that host,
and python:3.12-slim too: the proxy. Uses api.anthropic.com, which answers 401 either way but says why: no key, or a key it doesn't know.
"""
import os
import shlex
import subprocess

from agvisor import create_sandbox

host, image = os.environ.get("AGVISOR_HOST"), os.environ.get("AGVISOR_IMAGE")
KEY = "agvisor-test-not-a-real-key-7c1e9"
FETCH = ("import urllib.request as u, urllib.error as e, json\n"
         "try:\n r = u.urlopen({url!r}, timeout=20); print(r.status)\n"
         "except e.HTTPError as x: print(x.code, x.read().decode()[:300])")


def fetch(sb, url):
    r = sb.exec(f"python3 -c {shlex.quote(FETCH.format(url=url))}", timeout=60)
    return r.stdout.strip() or r.stderr.strip()


def mine():  # proxies and networks that egress= made
    docker = ["docker", *(["-H", host] if host else [])]
    return subprocess.run([*docker, "ps", "-a", "-q", "--filter", "label=agvisor.egress"], capture_output=True, text=True).stdout.split()


before = mine()
with create_sandbox(image, host=host, egress=["api.anthropic.com"], lifetime=600) as plain:
    without = fetch(plain, "https://api.anthropic.com/v1/models")  # a tunnel: no key is sent
assert without.startswith("401"), without

with create_sandbox(image, host=host, egress=["pypi.org"], inject={"api.anthropic.com": {"x-api-key": KEY}}, lifetime=600) as sb:
    with_key = fetch(sb, "https://api.anthropic.com/v1/models")  # the sandbox sent no key: the proxy did
    print("without the credential:", without[:120], "\nwith it:", with_key[:120])
    assert with_key.startswith("401") and with_key != without, with_key  # a key reached api.anthropic.com, and it didn't know it

    # the credential is not in the sandbox: not in its environment, its files, or the trust material it was given
    seen = sb.exec(f"env; ls -l /usr/local/share/agvisor; cat /proc/1/environ | tr '\\0' '\\n'; "
                   f"grep -rl {KEY} / --exclude-dir=proc --exclude-dir=sys --exclude-dir=dev 2>/dev/null; true", timeout=120)
    assert KEY not in seen.stdout and "ca.pem" in seen.stdout and "bundle.pem" in seen.stdout, seen.stdout
    assert "BEGIN PRIVATE KEY" not in sb.exec("cat /usr/local/share/agvisor/*").stdout  # a certificate, never a key

    assert fetch(sb, "https://pypi.org/simple/six/").startswith("200")  # another host: a plain tunnel, the real certificate checks out
    r = fetch(sb, "http://api.anthropic.com/v1/models")  # a credential is not sent in the clear
    assert r.startswith("403") and "https" in r, r
    assert "403" in fetch(sb, "https://example.org/")  # not on the list: the CONNECT is refused
    r = sb.exec("python3 -c \"import urllib.request as u; u.urlopen('https://api.anthropic.com/', timeout=20)\"", timeout=60)
    assert "CERTIFICATE_VERIFY_FAILED" not in r.stderr, r.stderr  # the sandbox trusts the egress CA
    r = sb.exec("env -u SSL_CERT_FILE python3 -c \"import ssl, urllib.request as u; "
                "u.urlopen('https://api.anthropic.com/', timeout=20, context=ssl.create_default_context(cafile='/etc/ssl/certs/ca-certificates.crt'))\"", timeout=60)
    assert "CERTIFICATE_VERIFY_FAILED" in r.stderr, r.stderr  # and only through what it was told: the system roots alone refuse it
assert mine() == before, "stop() left the proxy or the network"
print("ok")
