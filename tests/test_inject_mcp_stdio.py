"""Needs a docker host with real gVisor and internet (the `gvisor` job in ci.yml has both): AGVISOR_INJECT through agvisor-mcp,
driven over stdio by the real mcp client, the way an agent reaches it. Run with both extras:

    AGVISOR_HOST=ssh://user@vm AGVISOR_IMAGE=python:3.12-slim uv run --extra inject --extra mcp python tests/test_inject_mcp_stdio.py

AGVISOR_HOST unset -> docker on this machine. AGVISOR_IMAGE: python 3.12+ on a Debian base, already on that host, and python:3.12-slim
too: the egress proxy. Uses api.anthropic.com, which answers 401 either way but says why: no key, or a key it doesn't know.
"""
import asyncio
import json
import os
import shlex
import subprocess

from mcp import Client, StdioServerParameters

host = os.environ.get("AGVISOR_HOST")
KEY = "agvisor-test-not-a-real-key-3b8d2"
FETCH = ("import urllib.request as u, urllib.error as e\n"
         "try:\n r = u.urlopen('https://api.anthropic.com/v1/models', timeout=20); print(r.status)\n"
         "except e.HTTPError as x: print(x.code, x.read().decode()[:300])")
# the stdio client hands the server only a few variables by default: pass everything, plus a throwaway-friendly config
base = {k: v for k, v in os.environ.items() if k not in ("AGVISOR_NAME", "AGVISOR_NETWORK", "AGVISOR_EGRESS")}
base |= {"AGVISOR_IDLE_TTL": "120", "AGVISOR_LIFETIME": "600"}


def leftovers():  # sandboxes, proxies and networks that agvisor made and nobody removed
    docker = ["docker", *(["-H", host] if host else [])]
    ps = subprocess.run([*docker, "ps", "-a", "-q", "--filter", "label=agvisor.egress"], capture_output=True, text=True).stdout.split()
    return set(ps)


async def text(c, tool, **args):
    return "".join(b.text for b in (await c.call_tool(tool, args)).content)


async def main():
    before = leftovers()
    env = {**base, "AGVISOR_INJECT": json.dumps({"api.anthropic.com": {"x-api-key": KEY}})}
    async with Client(StdioServerParameters(command="agvisor-mcp", env=env)) as c:
        out = await text(c, "run", command=f"python3 -c {shlex.quote(FETCH)}")
        assert "invalid x-api-key" in out and "[exit 0]" in out, out  # a key reached the host, and the agent never sent one
        seen = await text(c, "run", command="env; ls /usr/local/share/agvisor; cat /proc/1/environ | tr '\\0' '\\n'")
        assert KEY not in seen and "bundle.pem" in seen, seen  # the agent can use the credential, not read it
    for _ in range(30):  # the server's atexit reset() runs once stdin closes
        if leftovers() <= before:
            break
        await asyncio.sleep(1)
    assert leftovers() <= before, "the proxy or the sandbox outlived the server"

    for bad in ("not json", '{"x.com": {"Host": "SECRET-VALUE"}}', '{"*.x.com": {"A": "SECRET-VALUE"}}'):  # the server refuses to start
        r = subprocess.run(["agvisor-mcp"], env={**base, "AGVISOR_INJECT": bad}, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60)
        assert r.returncode != 0 and "AGVISOR_INJECT" in r.stderr and "SECRET-VALUE" not in r.stderr, (bad, r.stderr)


asyncio.run(main())
print("ok")
