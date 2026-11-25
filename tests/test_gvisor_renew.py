"""Needs a docker host with real gVisor (the `gvisor` job in ci.yml has it): create_sandbox(lifetime=, renewable=True) and Sandbox.renew().

    AGVISOR_HOST=ssh://user@vm AGVISOR_IMAGE=python:3.12-slim uv run python tests/test_gvisor_renew.py

AGVISOR_HOST unset -> docker on this machine. AGVISOR_IMAGE: python 3.12+ on a Debian base, already on that host.
Takes about two minutes: it waits for sandboxes to expire.
"""
import os
import time

from agvisor import SandboxGone, create_sandbox, list_sandboxes

host, image = os.environ.get("AGVISOR_HOST"), os.environ.get("AGVISOR_IMAGE")


def alive(sb):
    try:
        return sb.exec("true", timeout=10).returncode == 0
    except SandboxGone:
        return False


# a plain lifetime is a hard cap, and not renewable: there is no deadline to move
plain = create_sandbox(image, host=host, lifetime=60)
try:
    plain.renew(600)
except ValueError as e:
    assert "not renewable" in str(e)
else:
    raise AssertionError("renew() of a sandbox made without renewable=True returned")
plain.stop()

# extend: a 20 s sandbox renewed to 70 s is still there after 20 s, and gone after the 70
sb = create_sandbox(image, host=host, lifetime=20, renewable=True)
sb.renew(70)
t0 = time.monotonic()
time.sleep(30)
assert alive(sb), "renew(70) did not extend a 20 s lifetime"
print(f"alive {time.monotonic() - t0:.0f} s into a 20 s lifetime that was renewed to 70 s")
time.sleep(max(0, 70 - (time.monotonic() - t0)) + 8)
assert not alive(sb), "the sandbox outlived its renewed deadline"
assert sb.name not in [b.name for b in list_sandboxes(host)], "--rm should have removed the container"
print("expired at the renewed deadline")

# shorten: renew() may also bring the deadline closer
sb = create_sandbox(image, host=host, lifetime=600, renewable=True)
sb.renew(10)
time.sleep(20)
assert not alive(sb), "renew(10) did not shorten a 600 s lifetime"
print("renewed to a shorter deadline: gone")

# a lifetime that was not renewed ends on time (it is checked every 5 s at most, and never later than the deadline)
t0 = time.monotonic()
sb = create_sandbox(image, host=host, lifetime=15, renewable=True)
while alive(sb):
    assert time.monotonic() - t0 < 40, "a 15 s lifetime did not end"
    time.sleep(1)
took = time.monotonic() - t0
assert 12 < took < 30, took
print(f"a 15 s renewable lifetime ended after {took:.0f} s")

# with idle_ttl as well: renew() is an exec, so it counts as activity; the lifetime is what it moved
sb = create_sandbox(image, host=host, lifetime=30, idle_ttl=60, renewable=True)
sb.renew(120)
time.sleep(40)
assert alive(sb), "renew(120) with idle_ttl did not extend the lifetime"
sb.stop()

# a handle from list_sandboxes() can renew too: it knows nothing of `renewable`, the file inside is the truth
sb = create_sandbox(image, host=host, lifetime=15, renewable=True)
handle = [b for b in list_sandboxes(host) if b.name == sb.name][0]
handle.renew(300)
time.sleep(25)
assert alive(sb), "a handle from list_sandboxes() could not renew"
sb.stop()

# and the file really is the deadline: what runs inside can rewrite it (the reason renewable is opt-in)
sb = create_sandbox(image, host=host, lifetime=15, renewable=True)
sb.exec("echo $(( $(date +%s) + 300 )) > /tmp/.agvisor_deadline")
time.sleep(25)
assert alive(sb)
print("code inside a renewable sandbox can extend its own life, as documented")
sb.stop()
print("ok")
