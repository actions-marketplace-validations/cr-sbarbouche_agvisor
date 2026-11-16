"""Needs a docker host with real gVisor (the `gvisor` job in ci.yml has it): Pool keeps started sandboxes ready.

    AGVISOR_HOST=ssh://user@vm AGVISOR_IMAGE=python:3.12-slim uv run python tests/test_gvisor_pool.py

AGVISOR_HOST unset -> docker on this machine. AGVISOR_IMAGE: Debian/Ubuntu-based, already on that host.
"""
import os
import subprocess
import time

from agvisor import Pool, create_sandbox

host, image = os.environ.get("AGVISOR_HOST"), os.environ.get("AGVISOR_IMAGE")
docker = ["docker", *(["-H", host] if host else [])]


def mine():  # sandboxes agvisor made and nobody deleted
    return set(subprocess.run([*docker, "ps", "-a", "-q", "--filter", "label=agvisor=1"], capture_output=True, text=True).stdout.split())


def wait(cond, what, seconds=90):
    end = time.monotonic() + seconds
    while not cond():
        assert time.monotonic() < end, f"gave up waiting for {what}"
        time.sleep(0.2)


before = mine()
kw = dict(host=host, network="none")

t0 = time.monotonic()
create_sandbox(image, lifetime=60, **kw).stop()
cold = time.monotonic() - t0  # what a sandbox costs without a pool

with Pool(2, image, lifetime=300, **kw) as pool:
    wait(lambda: pool.spares == 2, "two spares")
    assert len(mine() - before) == 2
    t0 = time.monotonic()
    sb = pool.acquire()
    warm = time.monotonic() - t0
    print(f"a sandbox made on the spot: {cold:.1f} s, from the pool: {warm:.1f} s")
    assert warm < cold, (warm, cold)
    other = pool.acquire()
    assert sb.exec("echo $HOSTNAME").stdout != other.exec("echo $HOSTNAME").stdout  # two different containers, both usable
    wait(lambda: pool.spares == 2, "the replacements")
    assert len(mine() - before) == 4  # two acquired, two spares
    sb.stop()
    other.stop()
assert mine() == before, "close() left spares running"

# idle_ttl: a spare nobody uses expires; acquire() skips it and still hands out a working sandbox
with Pool(1, image, idle_ttl=15, **kw) as pool:
    wait(lambda: pool.spares == 1, "a spare")
    old = mine() - before
    wait(lambda: not (mine() & old), "the spare to expire", seconds=60)  # --rm took it
    sb = pool.acquire()
    ids = subprocess.run([*docker, "ps", "-q", "--filter", f"name=^{sb.name}$"], capture_output=True, text=True).stdout.split()
    assert sb.exec("echo alive").stdout == "alive\n" and ids and not set(ids) & old  # a live one, not the expired spare
    sb.stop()
assert mine() == before, mine()
print("ok")
