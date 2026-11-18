"""Needs a docker host with real gVisor (the `gvisor` job in ci.yml has it): Sandbox.stats() follows what runs inside.

    AGVISOR_HOST=ssh://user@vm AGVISOR_IMAGE=python:3.12-slim uv run python tests/test_gvisor_stats.py

AGVISOR_HOST unset -> docker on this machine. AGVISOR_IMAGE: python 3.12+ on a Debian base, already on that host.
"""
import os
import time

from agvisor import SandboxGone, create_sandbox

host, image = os.environ.get("AGVISOR_HOST"), os.environ.get("AGVISOR_IMAGE")
MiB = 2**20


def until(sb, key, cond, seconds=30):  # docker's CPU figure needs the container to have been busy for a while
    end = time.monotonic() + seconds
    while not cond(s := sb.stats()):
        assert time.monotonic() < end, f"{key} never got there: {s}"
    return s


with create_sandbox(image, host=host, lifetime=300) as sb:
    idle = sb.stats()
    assert idle["memory_limit"] == 2 * 2**30, idle  # Limits' default, 2g
    assert 0 < idle["memory"] < 150 * MiB and idle["pids"] > 0, idle

    sb.spawn("python3 -c 'import time; a = bytearray(200 * 2**20); time.sleep(600)'")
    held = until(sb, "memory", lambda s: s["memory"] > idle["memory"] + 150 * MiB)  # 200 MiB held by a process
    print(f"idle {idle['memory'] // MiB} MiB, with 200 MiB held {held['memory'] // MiB} MiB")

    sb.spawn("sh -c 'while :; do :; done'")
    until(sb, "cpu", lambda s: s["cpu"] > 50)  # one core spinning reads about 100

    sb.exec("for i in $(seq 20); do sleep 600 & done; true")
    assert sb.stats()["pids"] > held["pids"], "20 more processes did not show"

    before = sb.stats()["memory"]
    sb.exec("dd if=/dev/zero of=/var/tmp/big bs=1M count=100 2>/dev/null")
    on_disk = sb.stats()["memory"]
    sb.exec("dd if=/dev/zero of=/tmp/big bs=1M count=100 2>/dev/null")
    in_ram = sb.stats()["memory"]
    assert on_disk - before < 50 * MiB, (before, on_disk)  # the root filesystem is not memory
    assert in_ram - on_disk > 80 * MiB, (on_disk, in_ram)  # /tmp is: the docstring says so
    print(f"100 MiB on the root fs: +{(on_disk - before) // MiB} MiB, in /tmp: +{(in_ram - on_disk) // MiB} MiB")

try:
    sb.stats()
except SandboxGone:
    pass
else:
    raise AssertionError("stats() of a deleted sandbox returned")
print("ok")
