"""Needs docker on this machine with real gVisor (the `gvisor` job in ci.yml has it): ports= publishes a sandbox's port on the
loopback of the docker host, through create_sandbox() and through agvisor-mcp driven over stdio.

    AGVISOR_IMAGE=python:3.12-slim uv run --extra mcp python tests/test_gvisor_ports.py

AGVISOR_IMAGE: python 3.12+ on a Debian base, already on this machine. Not for AGVISOR_HOST: a published port is on the remote
machine's loopback, and this script connects from here.
"""
import asyncio
import os
import socket
import subprocess
import time
import urllib.request

from mcp import Client, StdioServerParameters

from agvisor import create_sandbox

assert not os.environ.get("AGVISOR_HOST"), "ports are on the docker host's loopback: this test needs docker on this machine"
image = os.environ.get("AGVISOR_IMAGE")
SERVE = "mkdir -p /tmp/www && echo hello-from-the-sandbox > /tmp/www/index.html && cd /tmp/www && exec python3 -m http.server {}"
web = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # a proxy variable on this machine must not catch 127.0.0.1


def fetch(address, seconds=60):  # the server inside takes a moment to listen
    end = time.monotonic() + seconds
    while True:
        try:
            return web.open(f"http://{address}/index.html", timeout=5).read().decode()
        except OSError:
            assert time.monotonic() < end, f"nothing answered at {address}"
            time.sleep(0.5)


def refused(address):
    try:
        socket.create_connection(address, timeout=5).close()
    except ConnectionRefusedError:
        return True
    return False


def mine():  # sandboxes agvisor made and nobody deleted
    return set(subprocess.run(["docker", "ps", "-a", "-q", "--filter", "label=agvisor=1"], capture_output=True, text=True).stdout.split())


def lan_address():  # this machine's address on the network, not loopback: where a port bound to 0.0.0.0 would answer too
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.connect(("10.255.255.255", 1))  # a UDP connect sends nothing
        return s.getsockname()[0]


before = mine()
for network in ("bridge", "isolated"):
    with create_sandbox(image, ports=[8000, 8001], network=network, lifetime=300) as sb:
        sb.spawn(SERVE.format(8000))
        a, b = sb.port(8000), sb.port(8001)
        assert a != b and 1024 < a < 65536, (a, b)
        assert fetch(f"127.0.0.1:{a}") == "hello-from-the-sandbox\n", network  # out of the sandbox and back through the published port
        listing = subprocess.run(["docker", "port", sb.name], capture_output=True, text=True).stdout.split("\n")
        assert all(l.split(" -> ")[1].startswith("127.0.0.1:") for l in listing if l), listing  # loopback only
        ip = lan_address()
        if not ip.startswith("127."):
            assert refused((ip, a)), f"port {a} also answers on {ip}"
        try:
            sb.port(9999)
        except RuntimeError as e:
            assert "9999" in str(e), e  # docker's answer: never published
        else:
            raise AssertionError("port() of an unpublished port returned")
    assert refused(("127.0.0.1", a)), "the port outlived the sandbox"
assert mine() == before, mine() - before

# the same through agvisor-mcp: the port tool exists only when AGVISOR_PORTS is set, and the agent gets the address from it
base = {k: v for k, v in os.environ.items() if not k.startswith("AGVISOR_") or k == "AGVISOR_IMAGE"}
base |= {"AGVISOR_IDLE_TTL": "120", "AGVISOR_LIFETIME": "600"}


async def text(c, tool, **args):
    return "".join(b.text for b in (await c.call_tool(tool, args)).content)


async def main():
    async with Client(StdioServerParameters(command="agvisor-mcp", env=base | {"AGVISOR_NETWORK": "bridge"})) as c:
        assert "port" not in {t.name for t in (await c.list_tools()).tools}
    async with Client(StdioServerParameters(command="agvisor-mcp", env=base | {"AGVISOR_NETWORK": "bridge", "AGVISOR_PORTS": "8000"})) as c:
        assert "port" in {t.name for t in (await c.list_tools()).tools}
        await text(c, "start_job", command=SERVE.format(8000))
        address = await text(c, "port", container_port=8000)
        assert address.startswith("127.0.0.1:"), address
        assert fetch(address) == "hello-from-the-sandbox\n"
        assert "9999" in await text(c, "port", container_port=9999)  # an answer, not a crash
    for _ in range(30):  # the server's atexit reset() runs once stdin closes
        if mine() <= before:
            break
        await asyncio.sleep(1)
    assert mine() <= before, "the sandbox outlived the server"

    for bad in ({"AGVISOR_PORTS": "8000"},  # network defaults to "none"
                {"AGVISOR_PORTS": "8000", "AGVISOR_NETWORK": "bridge", "AGVISOR_EGRESS": "pypi.org"},
                {"AGVISOR_PORTS": "web", "AGVISOR_NETWORK": "bridge"}):  # the server refuses to start
        r = subprocess.run(["agvisor-mcp"], env=base | bad, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60)
        assert r.returncode != 0 and "AGVISOR_PORTS" in r.stderr, (bad, r.stderr)


asyncio.run(main())
print("ok")
