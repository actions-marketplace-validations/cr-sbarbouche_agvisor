"""Needs a docker host with real gVisor (the `gvisor` job in ci.yml has it): create_sandbox(mounts=) shows host paths read-only.

    AGVISOR_IMAGE=python:3.12-slim uv run python tests/test_gvisor_mounts.py

Docker must be on this machine (AGVISOR_HOST unset): the test makes the directory it mounts. AGVISOR_IMAGE: a Debian-based image.
"""
import os
import tempfile
import time
from pathlib import Path

from agvisor import create_sandbox, list_sandboxes

assert not os.environ.get("AGVISOR_HOST"), "mounts= takes paths of the docker host: this test makes them locally"
image = os.environ.get("AGVISOR_IMAGE")

root = Path(tempfile.mkdtemp())
os.chmod(root, 0o755)
data = root / "data"
(data / "sub").mkdir(parents=True)
(data / "hello.txt").write_text("hello\n")
(data / "sub" / "deep.txt").write_text("deep\n")
os.symlink("/etc/passwd", data / "link-to-passwd")  # resolved inside the sandbox, not on the host
one = root / "one.txt"
one.write_text("a single file\n")
for p in (data, data / "sub", data / "hello.txt", data / "sub" / "deep.txt", one):
    os.chmod(p, 0o755 if p.is_dir() else 0o644)

with create_sandbox(image, mounts={str(data): "/models", str(one): "/etc/one.txt"}, lifetime=300) as sb:
    assert sb.exec("cat /models/hello.txt /models/sub/deep.txt").stdout == "hello\ndeep\n"
    assert sb.exec("cat /etc/one.txt").stdout == "a single file\n"  # a file, onto a path in the image's own /etc

    # read-only: nothing can be created, changed, removed or renamed, by root inside
    for cmd in ("echo x > /models/new.txt", "echo x >> /models/hello.txt", "rm /models/hello.txt", "mv /models/hello.txt /models/h2",
                "mkdir /models/d", "chmod 777 /models/hello.txt", "touch /models/sub/deep.txt", "echo x > /etc/one.txt",
                "mount -o remount,rw /models"):
        r = sb.exec(cmd)
        assert r.returncode != 0, f"{cmd!r} worked on a read-only mount"
    assert sb.exec("cat /models/hello.txt").stdout == "hello\n" and (data / "hello.txt").read_text() == "hello\n"  # nothing changed, on the host either
    assert not (data / "new.txt").exists() and sorted(p.name for p in data.iterdir()) == ["hello.txt", "link-to-passwd", "sub"]
    r = sb.exec("echo x > /models/new.txt")
    assert "read-only" in r.stderr.lower(), r.stderr
    assert sb.exec("echo ok > /tmp/fine.txt && cat /tmp/fine.txt").stdout == "ok\n"  # the rest of the sandbox is writable as ever

    # a link in the mounted directory points into the sandbox's own filesystem, not the host's
    inside = sb.exec("cat /models/link-to-passwd").stdout
    assert inside == sb.exec("cat /etc/passwd").stdout and inside != Path("/etc/passwd").read_text()

    # changes on the host show up inside: nothing was copied
    (data / "later.txt").write_text("added after start\n")
    for _ in range(20):
        r = sb.exec("cat /models/later.txt")
        if r.returncode == 0:
            break
        time.sleep(1)
    assert r.stdout == "added after start\n", (r.stdout, r.stderr)
    (data / "hello.txt").write_text("changed on the host\n")
    for _ in range(20):
        if sb.exec("cat /models/hello.txt").stdout == "changed on the host\n":
            break
        time.sleep(1)
    else:
        raise AssertionError("an edit on the host never showed up inside")

# a source that does not exist is an error, and leaves nothing behind (docker's --volume would have made it)
before = {b.name for b in list_sandboxes()}
try:
    create_sandbox(image, mounts={str(root / "nope"): "/x"})
except RuntimeError as e:
    assert "does not exist" in str(e) or "no such file" in str(e).lower(), e
else:
    raise AssertionError("a missing source was accepted")
assert not (root / "nope").exists(), "docker made the missing directory"
assert {b.name for b in list_sandboxes()} == before

# next to a named sandbox's own writable /workspace
with create_sandbox(image, name="mounts-test", mounts={str(data): "/models"}) as sb:
    try:
        assert sb.exec("echo kept > /workspace/f && cat /workspace/f /models/sub/deep.txt").stdout == "kept\ndeep\n"
    finally:
        sb.delete()
print("ok")
