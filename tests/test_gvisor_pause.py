"""Needs a docker host with real gVisor (the `gvisor` job in ci.yml has it): Sandbox.pause() / resume().

    AGVISOR_HOST=ssh://user@vm AGVISOR_IMAGE=python:3.12-slim uv run python tests/test_gvisor_pause.py

AGVISOR_HOST unset -> docker on this machine. AGVISOR_IMAGE: python 3.12+ on a Debian base, already on that host.
"""
import os
import time

from agvisor import SandboxGone, SandboxPaused, create_sandbox, list_sandboxes

host, image = os.environ.get("AGVISOR_HOST"), os.environ.get("AGVISOR_IMAGE")
PAUSE = 4

with create_sandbox(image, host=host, lifetime=600) as sb:
    sb.python("x = 42")  # a process with state: it must still be there afterwards
    sb.spawn("while :; do date +%s%N >> /var/tmp/ticks; sleep 0.2; done")  # a heartbeat
    time.sleep(2)

    sb.pause()
    state = sb._docker("inspect", "-f", "{{.State.Paused}} {{.State.Running}}", sb.name).stdout.strip()
    assert state == "true true", state  # docker: paused, and still "running"
    try:
        sb.exec("true")
    except SandboxPaused as e:
        assert "resume()" in str(e)
    else:
        raise AssertionError("exec() in a paused sandbox returned")
    mine = [b for b in list_sandboxes(host) if b.name == sb.name][0]
    assert mine._paused and mine._running  # what `agvisor ls` shows
    try:
        sb.pause()
    except RuntimeError:
        pass
    else:
        raise AssertionError("pausing a paused sandbox didn't raise")
    time.sleep(PAUSE)
    sb.resume()
    try:
        sb.resume()
    except RuntimeError:
        pass
    else:
        raise AssertionError("resuming a running sandbox didn't raise")

    time.sleep(2)
    ticks = [int(t) / 1e9 for t in sb.exec("cat /var/tmp/ticks").stdout.split()]
    gaps = [b - a for a, b in zip(ticks, ticks[1:])]
    assert max(gaps) > PAUSE - 1, f"the heartbeat never stalled: biggest gap {max(gaps):.1f} s"  # nothing ran while it was frozen
    assert sorted(gaps)[-2] < 2, "it stalled more than once"
    assert ticks[-1] > ticks[len(ticks) // 2] and gaps[-1] < 2, "the heartbeat did not come back"
    assert sb.python("x").stdout.strip() == "42"  # the interpreter, and its variables, came back with it
    print(f"heartbeat stalled for {max(gaps):.1f} s of a {PAUSE} s pause")

    sb.pause()
    sb.stop()  # removing a paused sandbox works
    try:
        sb.exec("true")
    except SandboxGone:
        pass
    else:
        raise AssertionError("a removed sandbox answered")

# idle_ttl: a sandbox parked for longer than idle_ttl must not expire the moment it wakes (the watchdog's clock ran on)
with create_sandbox(image, host=host, idle_ttl=20) as sb:  # exec(timeout=) must stay under idle_ttl
    sb.exec("true", timeout=10)
    sb.pause()
    time.sleep(35)  # idle_ttl and then some
    sb.resume()
    time.sleep(8)  # the watchdog looks every 5 s: long enough for it to have expired us, were it going to
    assert sb.exec("echo alive", timeout=10).stdout == "alive\n"
    print("idle_ttl=20: alive 8 s after a 35 s pause")

# lifetime during a pause: not asserted, because what gVisor does with its clocks while paused was not known when this
# was written. The docstring says "not checked": this is where it gets checked, and the answer shows up in the CI log.
sb = create_sandbox(image, host=host, lifetime=25)
sb.pause()
time.sleep(35)
sb.resume()
time.sleep(1)
try:
    left = sb.exec("echo alive").stdout.strip()
except SandboxGone:
    left = "gone"
print(f"lifetime=25, paused for 35 s: after resume the sandbox is {left}")
sb.stop()
print("ok")
