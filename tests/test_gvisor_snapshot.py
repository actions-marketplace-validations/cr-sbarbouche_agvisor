"""Needs a docker host with real gVisor (the `gvisor` job in ci.yml has it): snapshot() and restore() copy volumes in a runsc container.

    AGVISOR_HOST=ssh://user@vm AGVISOR_IMAGE=node:22-alpine uv run python tests/test_gvisor_snapshot.py

AGVISOR_HOST unset -> docker on this machine. AGVISOR_IMAGE: any image already on that host (needs sh, rm, cp).
"""
import os
import time
import uuid

from agvisor import create_sandbox

host, image = os.environ.get("AGVISOR_HOST"), os.environ.get("AGVISOR_IMAGE")
kw = dict(image=image, name=f"snaptest-{uuid.uuid4().hex[:8]}", host=host, network="none")  # unique: never meets someone's sandbox

sb = create_sandbox(**kw)
try:
    sb.exec("mkdir -p venv/bin && echo good > venv/bin/tool && echo cfg > .hidden && ln -s venv/bin/tool link")
    sb.snapshot("clean")
    sb.exec("echo evil > venv/bin/tool && rm .hidden link && echo junk > extra")
    sb.restore("clean")
    out = sb.exec("cat venv/bin/tool .hidden; readlink link; ls -A").stdout
    assert out.split() == ["good", "cfg", "venv/bin/tool", ".hidden", "link", "venv"], out  # evil and extra are gone
    try:
        sb.restore("nope")
    except ValueError:
        pass
    else:
        raise AssertionError("restored a snapshot that does not exist")
    assert sb.exec("cat venv/bin/tool").stdout.strip() == "good"  # the failed restore wiped nothing
    sb.snapshot("other")
    assert sb.list_snapshots() == ["clean", "other"]
    sb.delete_snapshot("other")
    sb.delete_snapshot("other")  # already gone: fine
    assert sb.list_snapshots() == ["clean"]

    # a writer that never stops: it rewrites 200 files with the next number, one pass after another. The data dir at one
    # moment has every file at n or n-1; a copy taken while it runs would spread over the passes made meanwhile
    sb.exec("mkdir -p w && cd w && for n in $(seq 200); do echo 0 > f$n; done")
    job = sb.spawn("cd /workspace/w; i=0; while :; do i=$((i+1)); for f in f*; do echo $i > $f; done; done")
    time.sleep(2)

    def spread(tag):
        sb.restore(tag)  # kills the writer too, and the numbers are read from the snapshot's files
        nums = sorted(int(n) for n in sb.exec("cat w/f*").stdout.split())
        return nums[0], nums[-1]

    sb.snapshot("live")
    sb.exec("true")  # the sandbox runs again: not left frozen
    first = int(sb.exec("cat w/f1").stdout)
    time.sleep(1)
    assert int(sb.exec("cat w/f1").stdout) > first, "the writer did not run on after the snapshot"
    low, high = spread("live")
    assert low > 0 and high - low <= 1, (low, high)  # one moment

    job = sb.spawn("cd /workspace/w; i=0; while :; do i=$((i+1)); for f in f*; do echo $i > $f; done; done")
    time.sleep(2)
    sb._pause = lambda: False  # the same copy, with the writer running
    sb.snapshot("torn")
    low, high = spread("torn")
    print(f"copy of a running writer spans {high - low} passes (a paused one spans at most 1)")
finally:
    sb.delete()  # volumes too, even when an assert failed
print("ok")
