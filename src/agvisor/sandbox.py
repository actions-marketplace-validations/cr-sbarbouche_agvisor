import base64
import codecs
import collections
import contextlib
import io
import json
import math
import os
import posixpath
import re
import secrets
import shlex
import subprocess
import tarfile
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from . import _ca, egress_proxy
from .desktop import GUI_HOME, GUI_IMAGE, GUI_USER, RDP_PORT, make_token
from .limits import Limits

_ID = r"[A-Za-z0-9][A-Za-z0-9_.-]{0,62}"  # name= and owner=: they end up in --name, --volume and --label
_TAG = r"[A-Za-z0-9][A-Za-z0-9_-]{0,31}"  # snapshot tag: no '.', so a snapshot's volume name splits unambiguously into name.tag
_KEEP = 100_000  # exec(on_output=) keeps only this many trailing characters: the rest already went to on_output
_MARK = "/tmp/.agvisor_active"  # idle_ttl: every exec() touches it; PID 1 exits once it is older than idle_ttl
_DEADLINE = "/tmp/.agvisor_deadline"  # renewable=True: the epoch second at which PID 1 ends the sandbox; renew() rewrites it
# gui: RDP checks GNOME Remote Desktop's own credentials file, which the image bakes as gnome:gnome, NOT the unix
# password. Run as GUI_USER with the file on stdin and its path as $1: store ours, wait for the desktop to boot
# (up to 120 s), restart the RDP service (it reads the file only at startup), wait until it listens on 3389 (0D3D).
_RDP_CREDS = f"{GUI_HOME}/.local/share/gnome-remote-desktop/credentials.ini"
_RDP_SETUP = r"""set -e
umask 077
cat > "$1"
export XDG_RUNTIME_DIR=/run/user/$(id -u)
S=gnome-remote-desktop-headless.service
wait_for() { n=0; until "$@"; do n=$((n+1)); [ $n -le 120 ] || { echo "desktop not ready after 120s: $*" >&2; exit 1; }; sleep 1; done; }
wait_for systemctl --user is-active --quiet $S
systemctl --user restart $S
wait_for grep -qE ':0D3D [0-9A-F:]+ 0A' /proc/net/tcp /proc/net/tcp6
"""
# inject=: the sandbox gets the egress CA's certificate, and a bundle of its own CAs plus that one, which the tools that
# honour these variables use instead of their default. Run as root with the CA on stdin. No bundle in the image: fail.
_CA_DIR = "/usr/local/share/agvisor"
_TRUST = f"""set -e
d={_CA_DIR}
mkdir -p $d
cat > $d/ca.pem
for f in /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt /etc/ssl/cert.pem; do
  if [ -s $f ]; then cat $f > $d/bundle.pem; break; fi
done
[ -s $d/bundle.pem ] || {{ echo "no CA bundle in the image (looked in /etc/ssl/certs, /etc/pki/tls/certs)" >&2; exit 1; }}
cat $d/ca.pem >> $d/bundle.pem
chmod 755 $d
chmod 644 $d/ca.pem $d/bundle.pem
"""
_PROXY_IMAGE = "python:3.12-slim"  # egress=: runs egress_proxy.py; any image with python 3.12+ will do
_PROXY_RUNTIME = "runc"  # not gVisor: there, docker's embedded DNS (127.0.0.11) doesn't work, and the proxy couldn't resolve names
_ISOLATED = "agvisor-isolated"  # network="isolated": one docker bridge, shared, on which containers can't reach each other
_ICC = "com.docker.network.bridge.enable_icc"
_GRACE = 600  # seconds a proxy may exist before it must have a sandbox: its sandbox's image may still be downloading
# spawn(): a job lives in _JOBS/<id>/{log,pid,rc}. The rootfs, so `disk` caps what a job logs (/tmp is RAM under gVisor).
# _JOB runs as `sh -c _JOB DIR COMMAND TIMEOUT` (0 = none): GNU timeout starts a process group of its own whose id is its
# pid, so `kill -- -PID` reaches the command and whatever it started; it also writes the exit code when it ends.
_JOBS = "/var/tmp/.agvisor-jobs"
_JOB = 'timeout -k 5 "$2" sh -c "$1" >"$0/log" 2>&1 </dev/null & echo $! >"$0/pid"; wait $!; echo $? >"$0/rc"'
_RUNC = ("runc", "io.containerd.runc.v2")  # --privileged (gui) is only simulated under gVisor, real under these
_PYSESSION = Path(__file__).with_name("_pysession.py").read_text()  # python(): sent along with every call


def _py(arg, max_bytes, context=None):
    """_pysession.py as a shell command (`arg`: the call's seconds, or "kill"; `context`: a name that passed _TAG). Its
    output is cut to max_bytes in the sandbox: it is untrusted, and exec(input=) holds everything it prints in memory."""
    return (f"command -v python3 >/dev/null || {{ echo 'python3 is not in this sandbox image (python:3.12-slim has it)'; exit 127; }}; "
            f"S={shlex.quote(_PYSESSION)}; export S; python3 -c \"$S\" {arg}{f' {context}' if context else ''} 2>&1 | tail -c {int(max_bytes)}")


def _bytes(text):  # docker's "20.6MiB", "2GiB", "0B"
    n, unit = re.fullmatch(r"([\d.]+)([KMGTP]?i?B)", text.strip()).groups()
    return round(float(n) * 1024 ** "BKMGTP".index(unit[0]))


_VAR = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _env_text(env):
    """env={NAME: value} as the text of a docker env-file, checked. One line per variable, so a value can't hold a newline
    (it would start a second variable). A value never appears in an error message: it may be a secret."""
    if not isinstance(env, dict) or not env:
        raise ValueError("env must be a non-empty dict {NAME: value}")
    for k, v in env.items():
        if not isinstance(k, str) or not _VAR.fullmatch(k):
            raise ValueError(f"invalid environment variable name {k!r}: letters, digits and '_', not starting with a digit")
        if not isinstance(v, str) or any(c in v for c in "\r\n\0"):
            raise ValueError(f"environment variable {k}: the value must be text on one line")
    return "".join(f"{k}={v}\n" for k, v in env.items())


@contextlib.contextmanager
def _exec_opts(env, cwd):
    """The `docker exec` flags for exec(env=, cwd=). The environment goes through a file that only we can read, not
    through `-e`: a command line shows up in `ps` on the machine that runs the docker client."""
    opts = []
    if cwd is not None:
        if not isinstance(cwd, str) or not cwd.startswith("/") or "\0" in cwd:
            raise ValueError(f"cwd must be an absolute path, got {cwd!r}")
        opts += ["-w", cwd]
    if not env:
        yield opts
        return
    text = _env_text(env)
    with tempfile.NamedTemporaryFile("w", suffix=".env") as f:  # mkstemp: mode 0600
        f.write(text)
        f.flush()
        yield [*opts, f"--env-file={f.name}"]


def _mount_paths(mounts):
    """mounts={host path: path inside} as [(source, destination)], checked. Absolute paths, normalised, no `..`. Never `/`,
    and nothing under /proc, /sys or /dev; a source is not under /run or /var/run either, nor a `*.sock`: handing a
    docker socket to a sandbox hands over the host. A guard against mistakes by whoever chose the paths, not against them:
    they decide what is shared."""
    if not isinstance(mounts, dict) or not mounts:
        raise ValueError("mounts must be a non-empty dict {path on the docker host: path inside the sandbox}")
    out, seen = [], set()
    for src, dst in mounts.items():
        for what, path in (("source", src), ("destination", dst)):
            if not isinstance(path, str) or not path.startswith("/") or re.search(r'[,"\x00-\x1f]', path) or ".." in path.split("/"):
                raise ValueError(f"invalid mount {what} {path!r}: an absolute path with no '..', ',', '\"' or control characters")
        src, dst = posixpath.normpath(src), posixpath.normpath(dst)
        # normpath keeps a leading '//' (POSIX): both are plain absolute paths for docker either way
        under = lambda path, *roots: any(path == r or path.startswith(r + "/") for r in roots)
        if src.startswith("//") or dst.startswith("//") or src == "/" or dst == "/":
            raise ValueError("the root directory can't be mounted, or mounted over")
        if under(src, "/proc", "/sys", "/dev", "/run", "/var/run") or src.endswith(".sock") or under(dst, "/proc", "/sys", "/dev"):
            raise ValueError(f"won't mount {src} onto {dst}: not from /proc, /sys, /dev, /run, /var/run or a socket, and not onto /proc, /sys or /dev")
        if dst in seen:
            raise ValueError(f"two mounts onto {dst}")
        seen.add(dst)
        out.append((src, dst))
    return out


class SandboxGone(RuntimeError):
    """exec() found the container dead or deleted: killed for going over memory/pids, expired (lifetime/idle_ttl), or
    removed. It is not a failed command. Named sandboxes: create_sandbox(name=...) again gives a fresh container on
    the same volume."""


class SandboxPaused(RuntimeError):
    """exec() found the container paused (Sandbox.pause()): resume() it first. It is not a failed command."""


class Job:
    """A command running in the background of a sandbox, from Sandbox.spawn(). It outlives your process, and ends when
    it finishes, is kill()ed, or the sandbox goes (idle_ttl and lifetime too: poll(), read() and wait() count as
    activity for idle_ttl, the job itself does not). `Job(sandbox, id)` picks one up again later."""

    def __init__(self, sandbox, id):
        if not re.fullmatch(r"[0-9a-f]{12}", id):
            raise ValueError(f"invalid job id {id!r}")
        self.sandbox, self.id, self.returncode = sandbox, id, None
        self._d = f"{_JOBS}/{id}"

    def _check(self, wait=0):
        """One exec: wait up to `wait` seconds for the job to end. Its exit code, or None while it runs."""
        d = self._d
        r = self.sandbox.exec(f"[ -d {d} ] || exit 2; for i in $(seq {wait}); do [ -e {d}/rc ] && break; sleep 1; done; "
                              f"cat {d}/rc 2>/dev/null; true", timeout=wait + 5)
        if r.returncode == 2:
            raise LookupError(f"no job {self.id} in {self.sandbox.name}")
        if r.returncode:
            raise RuntimeError(f"checking job {self.id} failed (exit {r.returncode}): {r.stderr.strip()[-300:]}")
        if r.stdout.strip():
            self.returncode = int(r.stdout)
        return self.returncode

    def poll(self):
        """The exit code, or None while it still runs. 143 = killed with TERM, 137 = KILL, 124 = hit its timeout."""
        return self.returncode if self.returncode is not None else self._check()

    def wait(self, timeout=None):
        """Block until it ends and return the exit code; TimeoutError after `timeout` seconds (it keeps running).
        One exec() per ~25 s: cheap over ssh."""
        end = None if timeout is None else time.monotonic() + timeout
        ttl = self.sandbox.idle_ttl
        cap = min(25, ttl - 12) if ttl else 25  # exec(timeout=wait+5) must stay under idle_ttl - 6
        if cap < 1:
            raise ValueError(f"idle_ttl={ttl}s is too short to wait() in")
        while self.returncode is None:
            left = None if end is None else end - time.monotonic()
            if left is not None and left <= 0:
                raise TimeoutError(f"job {self.id} is still running")
            self._check(cap if left is None else max(1, min(cap, math.ceil(left))))
        return self.returncode

    def read(self, offset=0, max_bytes=100_000):
        """Its output (stdout and stderr merged) from byte `offset` on, at most `max_bytes`, as bytes. The next read
        starts at offset + len(result); empty = nothing new yet. A job's log lives in the sandbox's disk, uncapped
        apart from `Limits.disk`."""
        d = self._d
        try:
            data = self.sandbox._b64(f"[ -f {d}/log ] || {{ echo 'no such job' >&2; exit 2; }}; "
                                     f"tail -c +{int(offset) + 1} {d}/log | head -c {int(max_bytes)} | base64 -w0",
                                     int(max_bytes), self.sandbox._short())
        except RuntimeError as e:
            raise LookupError(f"no job {self.id} in {self.sandbox.name}: {e}") from None
        if data is None:
            raise RuntimeError("the sandbox sent more output than asked for")
        return data

    def kill(self, force=False):
        """Stop it and what it started (TERM; `force` = KILL). Not an error if it already ended: wait() gives the code."""
        d = self._d
        r = self.sandbox.exec(f"[ -d {d} ] || exit 2; kill -s {'KILL' if force else 'TERM'} -- -$(cat {d}/pid) 2>/dev/null; true",
                              timeout=self.sandbox._short())
        if r.returncode == 2:
            raise LookupError(f"no job {self.id} in {self.sandbox.name}")


class Sandbox:
    """host=None -> local docker; host="ssh://user@vm" -> docker on a remote VM/server."""

    def __init__(self, image=None, *, gui=False, limits=Limits(), network="bridge", lifetime=None,
                 idle_ttl=None, name=None, owner=None, host=None, runtime="runsc", egress=None, inject=None, ports=None,
                 env=None, renewable=False, mounts=None):
        self.gui, self.limits, self.network, self.lifetime, self.host = gui, limits, network, lifetime, host
        self.idle_ttl, self.runtime = idle_ttl, runtime
        # strict like `name`: it ends up in --runtime=. And never plain runc for a gui: it is run --privileged
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", runtime) or (gui and runtime in _RUNC):
            raise ValueError(f"invalid runtime {runtime!r}: must be a gVisor (runsc) runtime registered in docker")
        self.image = image or (GUI_IMAGE if gui else "ubuntu:24.04")
        if self.image.startswith("-"):  # `docker run ... IMAGE` would parse it as a flag, e.g. --privileged
            raise ValueError(f"invalid image name: {self.image!r}")
        if lifetime is not None and (gui or lifetime <= 0):
            raise ValueError("lifetime must be > 0 seconds and is only supported for headless sandboxes")
        if idle_ttl is not None and (gui or idle_ttl <= 0):
            raise ValueError("idle_ttl must be > 0 seconds and is only supported for headless sandboxes")
        if renewable and not lifetime:
            raise ValueError("renewable=True needs lifetime=: it is the lifetime that renew() moves")
        self.renewable = renewable
        # strict: `name` ends up in --name and --volume, where a ',' or ':' would inject mount options
        if name is not None and not re.fullmatch(_ID, name):
            raise ValueError(f"invalid name {name!r}: letters, digits, '_', '.', '-' only (max 63)")
        if owner is not None and not re.fullmatch(_ID, owner):
            raise ValueError(f"invalid owner {owner!r}: letters, digits, '_', '.', '-' only (max 63)")
        if limits.workspace and name is None:  # an anonymous sandbox has no volume: a cap that caps nothing would mislead
            raise ValueError("limits.workspace caps the data volume of a named sandbox: pass name=")
        if inject is not None:
            if gui or network != "bridge":
                raise ValueError("inject= goes through the egress proxy, which makes its own private network: headless only, and don't pass network=")
            inject = egress_proxy.parse_inject(inject)
            egress = [*(egress or []), *inject]  # a host you inject into is one the sandbox may reach
        if egress is not None:
            if gui or network != "bridge":
                raise ValueError("egress= makes its own private network: headless only, and don't pass network=")
            egress = list(egress)
            if not egress:
                raise ValueError("egress is empty: that is network='none'")
            if not egress_proxy.parse_policy(egress)[0]:  # raises ValueError on an entry it can't use
                raise ValueError("egress has only '!' entries: nothing is allowed, which is network='none'")
        if ports is not None:
            ports = list(ports)
            if not ports or len(set(ports)) != len(ports) or any(type(p) is not int or not 0 < p < 65536 for p in ports):
                raise ValueError(f"ports must be distinct whole numbers from 1 to 65535, got {ports!r}")
            if gui or network == "none" or egress is not None:  # docker publishes through the bridge: neither of these has a route in
                raise ValueError("ports= needs a network the host can reach: headless only, not network='none', egress= or inject=")
        self.ports = ports
        self.mounts = _mount_paths(mounts) if mounts is not None else None
        if env is not None:
            _env_text(env)  # raises ValueError: a bad one is an error now, not a failed `docker run`
        self.env = env
        self.egress, self._has_egress = egress, egress is not None  # _has_egress: also true for handles from list_sandboxes()
        self.inject, self._ca = inject, None  # _ca: the certificate the sandbox must trust, made by _egress_up()
        self.owner = owner
        self.created = None  # a datetime, set by list_sandboxes(); handles from create_sandbox() don't know it
        self.persistent = name is not None
        self.name = f"agvisor-{name or uuid.uuid4().hex[:8]}"  # also the name of the data volume
        self.data_dir = (GUI_HOME if gui else "/workspace") if self.persistent else None  # survives stop()
        self.rdp_port = self.password = None  # set by start() when gui; user is GUI_USER
        self._running = False
        self._paused = False  # only list_sandboxes() knows: a handle from create_sandbox() doesn't track it

    def _docker(self, *args, input=None, check=True):
        cmd = ["docker", *(["-H", self.host] if self.host else []), *args]
        r = subprocess.run(cmd, input=input, capture_output=True, text=True)
        if check and r.returncode:
            raise RuntimeError(r.stderr.strip())
        return r

    def _stream(self, *args, on_output):
        """Like _docker, but calls on_output(text) as each chunk arrives. stderr is merged into stdout."""
        cmd = ["docker", *(["-H", self.host] if self.host else []), *args]
        p = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        dec, tail, kept = codecs.getincrementaldecoder("utf-8")("replace"), collections.deque(), 0  # a chunk can end mid-character
        while chunk := os.read(p.stdout.fileno(), 4096):
            if text := dec.decode(chunk):
                tail.append(text)
                kept += len(text)
                while kept - len(tail[0]) >= _KEEP:  # a runaway `yes` must not fill our memory
                    kept -= len(tail.popleft())
                on_output(text)
        p.wait()
        return subprocess.CompletedProcess(cmd, p.returncode, "".join(tail), "")

    def run_args(self):
        l = self.limits
        a = ["run", "-d", f"--runtime={self.runtime}", f"--name={self.name}",
             f"--cpus={l.cpus}", f"--memory={l.memory}", f"--memory-swap={l.memory}",
             f"--pids-limit={l.pids}", f"--network={self._net if self.egress else _ISOLATED if self.network == 'isolated' else self.network}",
             # gVisor caps the rootfs overlay (a file on the host disk): writes past it get ENOSPC. Per-container,
             # no daemon setting needed. `root:` only: `all:` would also overlay /workspace and stop it persisting.
             f"--annotation=dev.gvisor.flag.overlay2=root:self,size={l.disk}"]
        if self.lifetime or self.idle_ttl:  # PID 1 ends on its own: the container exits, then --rm deletes it, even if we crash
            a.append("--rm")
        a.append("--label=agvisor=1")  # what list_sandboxes() finds
        if self.owner:
            a.append(f"--label=agvisor.owner={self.owner}")
        if self.persistent:
            a.append(f"--label=agvisor.name={self.name.removeprefix('agvisor-')}")
        if self.egress:
            a.append("--label=agvisor.egress=1")
        if self.egress or self.env:  # the proxy password and env= ride on stdin, not on a command line
            a.append("--env-file=/dev/stdin")
        if self.data_dir:  # a docker volume is a host directory: it is outside gVisor's discarded rootfs overlay
            a.append(f"--volume={self.name}:{self.data_dir}")
            if not self.gui:
                a.append(f"--workdir={self.data_dir}")  # relative paths in exec() land on persistent storage
        if self.gui:  # --privileged only grants simulated caps inside gVisor, not host access
            a += ["--privileged", f"--shm-size={l.shm or '1g'}", f"--publish=127.0.0.1::{RDP_PORT}"]
        elif l.shm:
            a.append(f"--shm-size={l.shm}")
        a += [f"--publish=127.0.0.1::{p}" for p in self.ports or ()]  # a random free port on the docker host's loopback
        # --mount, not -v: a missing source is an error, not a directory that docker makes (owned by root) and mounts
        a += [f"--mount=type=bind,source={src},target={dst},readonly" for src, dst in self.mounts or ()]
        if not self.gui:  # PID 1 is `sleep`, which never reaps orphans: they stay zombies and eat the pids limit
            a.append("--init")  # (gui: systemd is PID 1 and reaps)
            # an image ENTRYPOINT would swallow our PID 1 command as its arguments. exec() bypasses it anyway, so
            # clearing it loses nothing but the entrypoint's own setup script
            a.append("--entrypoint=")
        return a + [self.image] + self._pid1()

    def _watch_renewable(self):
        """PID 1 of a renewable sandbox: one loop that ends it when the deadline file says so (renew() rewrites that
        file), or when it has been idle for idle_ttl. It sleeps at most 5 s at a time, and never past the deadline it saw."""
        idle = self.idle_ttl
        return "; ".join([
            f"echo $(( $(date +%s) + {self.lifetime} )) > {_DEADLINE}", *([f"touch {_MARK}", "t=$(date +%s)"] if idle else []),
            "while :", "do n=$(date +%s)",
            # a pass that took 15+ s means the sandbox was frozen (pause()): not idleness, as in _pid1()
            *([f"[ $((n - t)) -gt 15 ] && touch {_MARK}"] if idle else []),
            f"r=$(( $(cat {_DEADLINE} 2>/dev/null) - n ))",  # a missing or empty file is a deadline long past
            "[ $r -gt 0 ] || break",
            *([f"[ $(( n - $(stat -c %Y {_MARK} 2>/dev/null || echo 0) )) -lt {idle} ] || break", "t=$n"] if idle else []),
            "[ $r -gt 5 ] && r=5", "sleep $r", "done"])

    def _pid1(self):
        if self.gui:
            return []
        if self.renewable:
            return ["sh", "-c", self._watch_renewable()]
        if not self.idle_ttl:
            return ["sleep", str(self.lifetime or "infinity")]
        # exits when the marker is older than idle_ttl (checked every 5s); a missing marker counts as expired.
        # `timeout` adds lifetime as a hard cap on top.
        # A `sleep 5` that took 15+ s means the sandbox was frozen (pause()): that was not idleness, so touch the marker
        # before the check, or a sandbox parked for longer than idle_ttl would expire the moment it wakes.
        watch = (f"touch {_MARK}; t=$(date +%s); while [ $(( $(date +%s) - $(stat -c %Y {_MARK} 2>/dev/null || echo 0) )) "
                 f"-lt {self.idle_ttl} ]; do sleep 5; n=$(date +%s); [ $((n - t)) -gt 15 ] && touch {_MARK}; t=$n; done")
        return (["timeout", str(self.lifetime)] if self.lifetime else []) + ["sh", "-c", watch]

    def _left_running(self):
        """A named container an earlier session never stopped: resume it, processes and all."""
        if not self.persistent:
            return False
        r = self._docker("inspect", "-f", "{{.State.Running}}", self.name, check=False)
        return r.stdout.strip() == "true"

    def _claim_volume(self):
        """The data volume of a named sandbox remembers its owner: the first create_sandbox(name=, owner=) labels it,
        and a different owner (or none) is refused. `volume create` on an existing volume returns it unchanged, so two
        racing creators can't both win.
        limits.workspace is the volume's size cap (`--opt size=`: an xfs project quota on the docker host). It is fixed
        the same way, so a different cap than the volume has is refused instead of silently ignored."""
        cap = self.limits.workspace
        try:
            self._docker("volume", "create", *([f"--label=agvisor.owner={self.owner}"] if self.owner else []),
                         *([f"--opt=size={cap}"] if cap else []), self.name)
        except RuntimeError as e:
            if cap and "quota" in str(e):  # docker: "quota size requested but no quota support"
                raise RuntimeError(f"can't cap /workspace at {cap} on this docker host ({e}): /var/lib/docker/volumes "
                                   "must be on xfs mounted with pquota (see the README)") from None
            raise
        # a space between the two: neither an owner nor a size has one
        out = self._docker("volume", "inspect", "-f", '{{index .Labels "agvisor.owner"}} {{index .Options "size"}}', self.name).stdout
        have, _, size = out.rstrip("\n").partition(" ")
        if have != (self.owner or ""):
            raise PermissionError(f"sandbox {self.name!r} belongs to another owner")
        if cap and size.lower() != cap.lower():
            raise ValueError(f"sandbox {self.name!r} {f'has a workspace cap of {size}' if size else 'has no workspace cap'}, "
                             f"not {cap}: the cap is fixed when the volume is created (delete() it first, which erases its files)")

    @property
    def _net(self):  # '_' after 'agvisor': no sandbox container is called like this, whatever `name=` is
        return f"agvisor_net_{self.name.removeprefix('agvisor-')}"

    @property
    def _proxy(self):
        return f"agvisor_proxy_{self.name.removeprefix('agvisor-')}"

    def _egress_up(self):
        """egress=: a private network with no route out, and on it a proxy that only lets the allowlist through. The
        proxy is the one thing the sandbox can reach, and also on the default bridge. Returns the sandbox's env-file.
        The sandbox gets the proxy's IP: docker's embedded DNS (127.0.0.11) does not work under gVisor."""
        # /run/agvisor: where the proxy keeps its rules (the root is read-only), and where allow() and deny() rewrite them
        pairs, b64 = [("AGVISOR_ALLOW", ",".join(self.egress))], lambda t: base64.b64encode(t.encode()).decode()
        tmpfs = ["--tmpfs=/run/agvisor:rw,noexec,nosuid,size=64k,uid=65534,gid=65534,mode=700"]
        if self.inject:  # before anything is started: no cryptography package is an error with nothing to clean up
            self._ca, proxy_pem = _ca.make(list(self.inject))
            pairs += [("AGVISOR_INJECT", b64(json.dumps(self.inject))), ("AGVISOR_TLS", b64(proxy_pem))]  # one line each: env-file
            tmpfs.append("--tmpfs=/tmp:rw,noexec,nosuid,size=1m,mode=1777")  # where the proxy loads its key from: the root is read-only
        self._gc_egress()
        if self.persistent:  # leftovers of an earlier session of this name
            self._docker("rm", "-f", self._proxy, check=False)
            self._docker("network", "rm", self._net, check=False)
        token = secrets.token_urlsafe(18)  # the proxy password: any other container on the bridge can reach its port
        env = "".join(f"{k}={v}\n" for k, v in [*pairs, ("AGVISOR_TOKEN", token)])
        self._docker("run", "-d", f"--runtime={_PROXY_RUNTIME}", f"--name={self._proxy}", "--env-file=/dev/stdin",
                     "--user=65534:65534", "--memory=256m", "--memory-swap=256m", "--cpus=1", "--pids-limit=512",
                     "--cap-drop=ALL", "--security-opt=no-new-privileges", "--read-only", *tmpfs,
                     f"--label=agvisor.egress={self.name}",
                     _PROXY_IMAGE, "python", "-u", "-c", Path(egress_proxy.__file__).read_text(), input=env)
        self._docker("network", "create", "--internal", "--label=agvisor.egress=1", self._net)
        self._docker("network", "connect", self._net, self._proxy)
        ip = self._docker("inspect", "-f", f'{{{{(index .NetworkSettings.Networks "{self._net}").IPAddress}}}}', self._proxy).stdout.strip()
        url = f"http://agvisor:{token}@{ip}:{egress_proxy.PORT}"
        skip = "localhost,127.0.0.1,::1"
        trust = [(k, f"{_CA_DIR}/bundle.pem") for k in ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE", "GIT_SSL_CAINFO")]
        trust += [("NODE_EXTRA_CA_CERTS", f"{_CA_DIR}/ca.pem")]  # node adds this one to its own list
        return "".join(f"{k}={v}\n" for k, v in [("HTTP_PROXY", url), ("HTTPS_PROXY", url), ("http_proxy", url),
                                                 ("https_proxy", url), ("NO_PROXY", skip), ("no_proxy", skip),
                                                 *(trust if self.inject else [])])

    def _gc_egress(self):
        """A sandbox that expires by itself (--rm) can't remove its proxy and network. So any egress start, and
        prune_egress(), remove the proxies that have no sandbox, then the networks nothing is attached to."""
        rows = [r.split("\t") for r in self._docker(
            "ps", "-a", "--filter", "label=agvisor.egress", "--format",
            '{{.Names}}\t{{.Label "agvisor.egress"}}\t{{.CreatedAt}}').stdout.splitlines()]
        boxes = {name for name, label, _ in rows if label == "1"}  # sandboxes carry "1", a proxy the name of its sandbox
        now = datetime.now(timezone.utc)
        dead = [name for name, label, created in rows
                if label != "1" and label not in boxes and (now - _parse_created(created)).total_seconds() > _GRACE]
        if dead:
            self._docker("rm", "-f", *dead, check=False)
        # `until`: not a network that another start is setting up right now
        self._docker("network", "prune", "-f", "--filter", "label=agvisor.egress", "--filter", "until=60s", check=False)

    def _isolated_up(self):
        """network="isolated": make sure the shared bridge exists, and really has inter-container traffic off. `create`
        on an existing network fails, which is fine: the check below is what matters (a network of that name that
        someone else made, with traffic on, must not pass for an isolated one)."""
        made = self._docker("network", "create", "-o", f"{_ICC}=false", "--label=agvisor.isolated=1", _ISOLATED, check=False)
        icc = self._docker("network", "inspect", "-f", f'{{{{index .Options "{_ICC}"}}}}', _ISOLATED, check=False)
        if icc.returncode:
            raise RuntimeError(made.stderr.strip())
        if icc.stdout.strip() != "false":
            raise RuntimeError(f"docker network {_ISOLATED!r} exists but lets containers talk to each other: remove it (docker network rm {_ISOLATED})")

    def _isolated_dns(self):
        """A user-defined network hands containers docker's embedded DNS (127.0.0.11), which doesn't work under gVisor:
        no name resolves, not even with --dns. So the sandbox gets the resolver config that a container on the default
        bridge gets (the host's real upstream servers), read from a throwaway container on that bridge."""
        r = self._docker("run", "--rm", f"--runtime={self.runtime}", "--network=bridge", "--entrypoint=cat", self.image,
                         "/etc/resolv.conf")
        self._docker("exec", "-i", "-u", "root", self.name, "sh", "-c", "cat > /etc/resolv.conf", input=r.stdout)

    def _egress_down(self):
        self._docker("rm", "-f", self._proxy, check=False)
        self._docker("network", "rm", self._net, check=False)

    def start(self):
        if self._running:  # create_sandbox() already started it, then `with` calls start() again
            return self
        if self.persistent:
            self._claim_volume()  # before anything can re-attach to, or remove, someone else's container
        resumed = self._left_running()  # limits/image args are ignored then: they were fixed at creation
        if not resumed:
            if self.persistent:  # a stopped leftover would block the name; its data lives in the volume
                self._docker("rm", "-f", self.name, check=False)
            if self.network == "isolated":
                self._isolated_up()
            try:
                # env= first: if it sets HTTPS_PROXY too, the proxy's own value comes later and wins
                env = (_env_text(self.env) if self.env else "") + (self._egress_up() if self.egress else "")
                self._docker(*self.run_args(), input=env or None)
            except BaseException:
                if self.egress:
                    self._egress_down()
                raise
        self._running = True  # the container exists from here, so stop() must clean up if setup fails
        try:
            if self.network == "isolated" and not resumed:
                self._isolated_dns()
            if self._ca and not resumed:  # trusts the CA before anything can make a request
                self._docker("exec", "-i", "-u", "root", self.name, "sh", "-c", _TRUST, input=self._ca)
            if self.gui:  # random host port + random password instead of the image's gnome:gnome
                self.rdp_port = self.port(RDP_PORT)
                self.password = secrets.token_urlsafe(12)  # urlsafe: no quote that could break the file below
                self._docker("exec", "-i", "-u", "root", self.name, "chpasswd",
                             input=f"{GUI_USER}:{self.password}\n")  # sudo / console login
                creds = f"[RDP]\ncredentials={{'username': <'{GUI_USER}'>, 'password': <'{self.password}'>}}\n"
                self._docker("exec", "-i", "-u", GUI_USER, self.name, "sh", "-c", _RDP_SETUP, "sh", _RDP_CREDS,
                             input=creds)  # RDP login; stdin keeps the password out of `ps`. Returns once RDP listens
        except BaseException:
            if resumed:  # never destroy a container (and its processes) that we merely re-attached to
                self._running = False
            else:
                self.stop()
            raise
        return self

    def port(self, container_port):
        """The port on the docker host (127.0.0.1 there) that reaches `container_port`, one of the ports= given when
        the sandbox was made. docker's own error if it wasn't published: a resumed sandbox keeps the ports of its first start."""
        out = self._docker("port", self.name, f"{int(container_port)}/tcp").stdout
        return int(out.splitlines()[0].rsplit(":", 1)[1])

    def stats(self):
        """What the sandbox uses right now, from `docker stats` (it takes about a second, CPU needs two samples):
        {"cpu": percent of one core (200.0 = two busy), "memory": bytes, "memory_limit": bytes, "pids": int}.
        Under gVisor, `memory` counts RAM and /tmp (RAM-backed) but not files on the root filesystem or /workspace. `pids`
        is what --pids-limit counts: the sandbox's own threads are in it, an idle sandbox reads ~35, so compare it
        with limits.pids, not with the number of processes you started. SandboxGone if the container is gone."""
        r = self._docker("stats", "--no-stream", "--format", "{{json .}}", self.name, check=False)
        if r.returncode:
            if "no such container" in r.stderr.lower():
                raise SandboxGone(f"{self.name} is gone: {r.stderr.strip()[-300:]}")
            raise RuntimeError(r.stderr.strip())
        row = json.loads(r.stdout)
        used, _, limit = row["MemUsage"].partition(" / ")
        return {"cpu": float(row["CPUPerc"].rstrip("%")), "memory": _bytes(used), "memory_limit": _bytes(limit),
                "pids": int(row["PIDs"])}

    def exec(self, cmd, *, input=None, timeout=120, on_output=None, env=None, cwd=None):
        """Run a shell command inside. Returns CompletedProcess; returncode 124 = timed out.
        `on_output(text)` is called with output as it is produced (stderr merged into stdout, which then holds
        only the last ~100 KB; it must not raise, and can't be combined with `input`).
        `env={NAME: value}` adds variables for this command only (they go through a private file, not a command line,
        so a secret doesn't show in `ps`); `cwd` is the absolute directory to start in.
        That needs GNU `timeout`, i.e. a Debian/Ubuntu-based image. Alpine's busybox returns 143 instead and kills
        only the shell: the children of a compound command (`a; b`) keep running."""
        if self.idle_ttl:
            if timeout + 6 >= self.idle_ttl:  # +5 = the kill grace below, +1 = the watchdog compares whole seconds
                raise ValueError(f"timeout={timeout}s must stay below idle_ttl={self.idle_ttl}s, "
                                 "or the sandbox could expire in the middle of the command")
            # activity = an exec starting or ending (the trap also fires on `exit`)
            cmd = f"trap 'touch {_MARK} 2>/dev/null' EXIT; touch {_MARK} 2>/dev/null; {cmd}"
        argv = ["timeout", "-k", "5", str(timeout), "sh", "-c", cmd]  # kills inside, not just the client
        if on_output and input is not None:
            raise ValueError("on_output can't be combined with input")
        with _exec_opts(env, cwd) as opts:
            if on_output:
                r = self._stream("exec", "-i", *opts, self.name, *argv, on_output=on_output)
                msg = r.stdout  # _stream merges stderr into stdout
            else:
                r = self._docker("exec", "-i", *opts, self.name, *argv, input=input, check=False)
                msg = r.stderr
        # ponytail: matches message text. "is not running" and "WaitPID" were seen on a real gVisor VM; "Error response
        # from daemon" and "No such container" are docker's standard wording, not re-checked there. The prefix is
        # required for the first two so a command printing "nginx is not running" isn't mistaken for a dead sandbox.
        msg = msg.lower()
        if r.returncode in (1, 128) and ("waitpid" in msg or "error response from daemon" in msg
                                         and ("is not running" in msg or "no such container" in msg)):
            raise SandboxGone(f"{self.name} is gone: {msg.strip()[-300:]}")
        if r.returncode == 1 and "error response from daemon" in msg and "is paused" in msg:  # docker: "Container ... is paused, unpause the container before exec"
            raise SandboxPaused(f"{self.name} is paused: resume() it first")
        return r

    def _short(self):  # an exec() that only looks at files: well under idle_ttl, whatever it is
        return min(30, self.idle_ttl - 7) if self.idle_ttl else 30

    def spawn(self, cmd, *, timeout=None, env=None, cwd=None):
        """Start `sh -c cmd` in the background and return a Job at once, for what outlives a single exec(): a long scan, a
        dev server. `timeout` kills it after that many seconds; the default is to run until it ends, is kill()ed or the
        sandbox goes. exec(timeout=) must stay under idle_ttl, a background job doesn't have to, but nothing keeps the
        sandbox alive for it either: poll() it, or set `lifetime` for a hard budget. Same image requirements as exec().
        `env` and `cwd` are exec()'s: the job starts with them."""
        if timeout is not None and timeout <= 0:
            raise ValueError("timeout must be > 0 seconds")
        job = Job(self, uuid.uuid4().hex[:12])
        d = shlex.quote(job._d)
        r = self.exec(f"mkdir -p {d} && {{ sh -c {shlex.quote(_JOB)} {d} {shlex.quote(cmd)} {int(timeout or 0)} >/dev/null 2>&1 </dev/null & }}; "
                      f"for i in $(seq 50); do [ -s {d}/pid ] && exit 0; sleep 0.1; done; exit 1", timeout=self._short(), env=env, cwd=cwd)
        if r.returncode:
            raise RuntimeError(f"spawn failed (exit {r.returncode}): {r.stderr.strip()[-300:]}")
        return job

    def python(self, code, *, timeout=60, restart=False, max_bytes=30_000, context=None):
        """Run Python `code` in a session that keeps variables, imports and functions between calls, like notebook cells:
        one long-lived interpreter inside, so the image needs python3. The value of a last expression is printed.
        Returns a CompletedProcess: stdout is what the code printed, stderr merged, the last `max_bytes` of it. A Python
        exception is a traceback in stdout, not a return code. At `timeout` seconds the code gets a TimeoutError and
        the session survives; stuck where that can't reach it (a C call), the session is killed and returncode is 124.
        127 = no python3 in the image. `restart` kills the session first. The session goes with the container. Same
        limits as exec(): timeout + 5 must stay under idle_ttl, GNU coreutils in the image.
        `context` names a session: each name is an interpreter of its own (variables, imports), so two analyses don't
        share globals; None is the original one. Letters, digits, '_' and '-' (up to 32). Each costs the memory of an
        idle interpreter until the sandbox ends: they all count in limits.memory. `restart` and the timeout kill only that one."""
        if timeout < 1:
            raise ValueError("timeout must be >= 1 second")
        if context is not None and not (isinstance(context, str) and re.fullmatch(_TAG, context)):
            raise ValueError(f"invalid python context {context!r}: letters, digits, '_', '-' only (max 32)")
        if restart:
            self.exec(_py("kill", max_bytes, context), timeout=self._short())
        r = self.exec(_py(int(timeout), max_bytes, context), input=code, timeout=int(timeout) + 5)  # the alarm fires 5 s before this
        if r.returncode == 124:
            self.exec(_py("kill", max_bytes, context), timeout=self._short())
        return r

    def put(self, path, data: bytes, *, timeout=120):
        """Write `data` to `path` inside: parent directories are created, an existing file is replaced. Goes through
        exec() (base64 on stdin: any bytes survive, a dead sandbox raises SandboxGone). Relative paths start in
        /workspace for a named sandbox. Needs GNU coreutils, like exec(timeout)."""
        q = shlex.quote(path)
        r = self.exec(f'mkdir -p -- "$(dirname -- {q})" && base64 -d > {q}', input=base64.b64encode(data).decode(), timeout=timeout)
        if r.returncode:
            raise RuntimeError(f"put {path!r} failed (exit {r.returncode}): {r.stderr.strip()[-300:]}")

    def put_dir(self, local, remote, *, timeout=120):
        """Copy the local directory `local` into `remote` inside: created if needed, files of the same name replaced,
        others kept. A tar through put(), so the same limits (GNU tar and coreutils in the image). Links are copied as
        links, never followed: a link on your side can't pull a host file into the sandbox. Owners are reset."""
        if not os.path.isdir(local):
            raise NotADirectoryError(local)

        def clean(ti):  # your uid and user name are none of the sandbox's business
            ti.uid = ti.gid = 0
            ti.uname = ti.gname = ""
            return ti

        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as t:
            t.add(local, arcname=".", filter=clean)
        tmp, q = f"/var/tmp/.agvisor-{uuid.uuid4().hex[:12]}.tar", shlex.quote(remote)
        self.put(tmp, buf.getvalue(), timeout=timeout)
        r = self.exec(f"mkdir -p -- {q} && tar -xf {tmp} -C {q} --no-same-owner; rc=$?; rm -f {tmp}; exit $rc", timeout=timeout)
        if r.returncode:
            raise RuntimeError(f"put_dir {remote!r} failed (exit {r.returncode}): {r.stderr.strip()[-300:]}")

    def get_dir(self, remote, local, *, max_bytes=50_000_000, timeout=120):
        """Copy the directory `remote` inside to `local` here: created if needed, files of the same name replaced. The
        sandbox is untrusted, so the tar that comes out is capped at `max_bytes` (ValueError) and extracted with
        tarfile's "data" filter: nothing lands outside `local`, no device files, no setuid bits (tarfile.FilterError
        otherwise). Links that point outside `local` (every absolute one, e.g. a venv's python) are left out, not
        followed or an error: returns their names."""
        tmp, q = f"/var/tmp/.agvisor-{uuid.uuid4().hex[:12]}.tar", shlex.quote(remote)
        r = self.exec(f"[ -d {q} ] || {{ echo 'not a directory' >&2; exit 1; }}; tar -cf {tmp} -C {q} .", timeout=timeout)
        if r.returncode:
            raise RuntimeError(f"get_dir {remote!r} failed (exit {r.returncode}): {(r.stderr or r.stdout).strip()[-300:]}")
        try:
            data = self.get(tmp, max_bytes=max_bytes, timeout=timeout)
        finally:
            self.exec(f"rm -f {tmp}", timeout=timeout)
        os.makedirs(local, exist_ok=True)
        skipped = []

        def keep(member, dest):
            try:
                return tarfile.data_filter(member, dest)
            except (tarfile.AbsoluteLinkError, tarfile.LinkOutsideDestinationError):
                skipped.append(member.name)  # None = don't extract it
        with tarfile.open(fileobj=io.BytesIO(data)) as t:
            t.extractall(local, filter=keep)
        return skipped

    def _b64(self, script, max_bytes, timeout):
        """Run `script`, which prints base64, and decode it; None if it is more than max_bytes. It is the sandbox talking:
        a doctored `base64` can't fill our memory. A failing script raises RuntimeError with what it printed (stderr is
        merged into the same stream)."""
        out, size = [], 0
        limit = 4 * (max_bytes // 3 + 2)  # base64 characters of max_bytes+1 bytes; a sandbox that sends more is lying

        def keep(text):
            nonlocal size
            size += len(text)
            if size <= limit:
                out.append(text)

        r = self.exec(script, timeout=timeout, on_output=keep)  # on_output: bounded memory
        if r.returncode:
            raise RuntimeError(f"(exit {r.returncode}): {r.stdout.strip()[-300:]}")
        data = None if size > limit else base64.b64decode("".join(out), validate=True)
        return data if data is not None and len(data) <= max_bytes else None

    def get(self, path, *, max_bytes=10_000_000, timeout=120):
        """Contents of the regular file at `path` inside, as bytes. A bigger file raises ValueError: the sandbox is
        untrusted, so what we read from it is capped (it can't fill our memory, even with a doctored `base64`)."""
        q = shlex.quote(path)
        try:
            data = self._b64(f"[ -f {q} ] || {{ printf 'not a file: %s\\n' {q} >&2; exit 1; }}; head -c {max_bytes + 1} -- {q} | base64 -w0",
                             max_bytes, timeout)
        except RuntimeError as e:
            raise RuntimeError(f"get {path!r} failed {e}") from None
        if data is None:
            raise ValueError(f"{path!r} is larger than max_bytes={max_bytes}")
        return data

    def desktop_token(self, key: bytes, ttl=300):
        """Token for <agvisor-desktop>. Mint it server-side only: it carries the RDP password.
        Replayable until `ttl` runs out."""
        if self.rdp_port is None:
            raise ValueError("start() a gui=True sandbox first")
        return make_token(key, self.rdp_port, self.password, ttl)

    def stop(self):
        """Remove the container and free its CPU/RAM. A named sandbox keeps its data volume."""
        self._docker("rm", "-f", self.name, check=False)
        if self._has_egress:
            self._egress_down()
        self._running = False

    def delete(self):
        """stop() and also erase the persistent data and its snapshots, so create_sandbox(name=...) starts from scratch."""
        self.stop()
        if self.persistent:
            snaps = self._docker("volume", "ls", "-q", "--filter", f"label=agvisor.snap={self.name}", check=False).stdout.split()
            self._docker("volume", "rm", "-f", self.name, *snaps, check=False)

    def _snap_volume(self, tag):
        if not self.persistent:
            raise ValueError("snapshots need a named sandbox: create_sandbox(name=...)")
        if not re.fullmatch(_TAG, tag):
            raise ValueError(f"invalid snapshot tag {tag!r}: letters, digits, '_', '-' only (max 32)")
        return f"agvisor_snap_{self.name.removeprefix('agvisor-')}.{tag}"  # '_' after 'agvisor': no sandbox volume is called that

    def _copy(self, src, dst):
        """Make volume `dst` an exact copy of volume `src`, in a throwaway gVisor container (hostile files never meet a
        host-side cp). Needs sh, rm and cp in the sandbox's image."""
        self._docker("run", "--rm", f"--runtime={self.runtime}", "--network=none", f"--memory={self.limits.memory}",
                     "--user=0", "--entrypoint=sh", f"--volume={src}:/from:ro", f"--volume={dst}:/to", self.image,
                     "-c", "set -e; rm -rf /to/* /to/.[!.]* /to/..?*; cp -a /from/. /to/")

    def _pause(self):
        """Freeze the container, so that nothing writes to the volume while it is copied. False when it isn't running
        (a stopped sandbox has no writers); a running one that can't be paused raises: no silent inconsistent copy."""
        r = self._docker("pause", self.name, check=False)
        if r.returncode and self._docker("inspect", "-f", "{{.State.Running}}", self.name, check=False).stdout.strip() == "true":
            raise RuntimeError(f"can't pause {self.name} to copy its data: {r.stderr.strip()}")
        return not r.returncode

    def _proxy_exec(self, *args, input=None):
        """Run a command in the egress proxy's container: its stdout. SandboxGone if the proxy is gone (the sandbox expired
        and `agvisor prune` took it)."""
        r = self._docker("exec", "-i", self._proxy, *args, input=input, check=False)
        if r.returncode:
            err = r.stderr.strip()
            if "no such container" in err.lower() or "is not running" in err.lower():
                raise SandboxGone(f"the egress proxy of {self.name} is gone: {err[-300:]}")
            raise RuntimeError(err[-300:] or f"exit {r.returncode}")
        return r.stdout

    def _egress_edit(self, op, entries):
        if not self._has_egress:
            raise ValueError(f"{self.name} has no egress allowlist to change: create it with egress=[...]")
        if not entries:
            raise ValueError("give at least one entry")
        egress_proxy.parse_rules(entries)  # raises ValueError for one the proxy would refuse, '!...' included
        self._proxy_exec("python", "-c", Path(egress_proxy.__file__).read_text(), "edit", input="".join(f"{op} {e}\n" for e in entries))

    def allow(self, *entries):
        """Let the sandbox reach more hosts, at once, while it runs: the same entries as egress= (`host`, `*.host`, an address or
        a network, each with an optional `:port`). An entry that was denied is un-denied. Only for a sandbox made with egress=
        (or inject=). Applies to new connections; ValueError for an entry the proxy would refuse. It is the host's decision
        to make: don't hand it to the code that runs inside."""
        self._egress_edit("allow", entries)

    def deny(self, *entries):
        """Shut hosts out again, at once: the entry is removed from the allowlist and a `!entry` rule that wins over every
        allow is put in its place, so `deny("admin.example.com")` also carves that name out of an allowed `*.example.com`.
        A deny without a port means every port. Tunnels that are already open to a host that is no longer allowed are
        closed, so it takes effect for a download in progress too."""
        self._egress_edit("deny", entries)

    def egress_rules(self):
        """The rules in force now, in order: the entries of egress= (plus the hosts of inject=), changed by allow() and deny().
        A `!entry` is a deny. SandboxGone if the proxy is gone."""
        if not self._has_egress:
            raise ValueError(f"{self.name} has no egress allowlist: create it with egress=[...]")
        return self._proxy_exec("cat", egress_proxy.RULES_FILE).split()

    def egress_denied(self):
        """What the sandbox tried to reach and the rules refused, as "host:port", in the order it first tried: the list to
        show whoever decides what to allow(). One that a rule lets through by now is not in it. It reads the proxy's log
        (one line per connection), which is a few MB for a million connections. SandboxGone if the proxy is gone."""
        rules = self.egress_rules()
        allow, deny = egress_proxy.parse_policy(rules)
        refused = {}
        for line in self._docker("logs", self._proxy).stdout.splitlines():
            kind, _, rest = line.partition(" ")
            host, _, port = rest.partition(" ")
            if kind == "deny" and port.isdigit() and not egress_proxy.check(allow, deny, host, int(port)):
                refused[f"{host}:{port}"] = None  # a dict: unique, in first-seen order
        return list(refused)

    def _unpause(self, check=True):
        self._docker("unpause", self.name, check=check)
        if self.idle_ttl:  # the watchdog's clock kept running while its sleep was frozen: don't let that expire us
            self._docker("exec", self.name, "touch", _MARK, check=False)

    def renew(self, seconds):
        """Make the sandbox expire `seconds` from now, instead of when its `lifetime` runs out: later or earlier, and
        as often as you like. Needs create_sandbox(lifetime=..., renewable=True). ValueError if this sandbox isn't
        renewable. The deadline is a file inside the sandbox (that is how PID 1 sees it), so code running there can extend
        its own life by writing it: that is why `renewable` is opt-in. A plain `lifetime` stays a hard cap it can't touch."""
        if type(seconds) is not int or seconds <= 0:
            raise ValueError("seconds must be a whole number > 0")
        r = self.exec(f"[ -f {_DEADLINE} ] || exit 3; echo $(( $(date +%s) + {seconds} )) > {_DEADLINE}.new && mv {_DEADLINE}.new {_DEADLINE}",
                      timeout=self._short())
        if r.returncode == 3:
            raise ValueError(f"{self.name} is not renewable: create it with lifetime=... and renewable=True")
        if r.returncode:
            raise RuntimeError(f"renew failed (exit {r.returncode}): {r.stderr.strip()[-300:]}")

    def pause(self):
        """Freeze every process in the sandbox until resume(): no CPU, nothing runs, and exec() raises SandboxPaused. It
        is a freeze, not a checkpoint: the memory (RAM, /tmp) stays held. Clocks go on: `lifetime` keeps counting (not
        checked whether gVisor's stops it), and resume() restarts the `idle_ttl` clock, so a sandbox that waited longer
        than that doesn't expire the moment it wakes. RuntimeError, with docker's words, if it is already paused or not running."""
        self._docker("pause", self.name)

    def resume(self):
        """Undo pause(). RuntimeError, with docker's words, if it is not paused."""
        self._unpause()

    def snapshot(self, tag):
        """Save a copy of the data dir (/workspace) as `tag`, replacing a snapshot of that name. Named sandboxes only.
        Kept until delete(). Costs as much disk as the data dir (so at most limits.workspace, if set). The running
        container is paused while it is copied, so the copy is the data dir as it was at one moment, not a mix of
        before and after a write. Its processes just stop for that time (a few seconds), and an exec() meanwhile fails;
        an application that writes one logical change in several steps can still be caught between them."""
        snap = self._snap_volume(tag)
        self._claim_volume()  # also makes sure the volume exists: `docker run -v` would silently create an empty one
        self._docker("volume", "create", f"--label=agvisor.snap={self.name}", snap)
        paused = False
        try:
            paused = self._pause()
            self._copy(self.name, snap)
        except BaseException:
            self._docker("volume", "rm", "-f", snap, check=False)  # a half-copied snapshot must not be restorable
            raise
        finally:
            if paused:
                self._unpause(check=False)

    def list_snapshots(self):
        """Tags of this sandbox's snapshots, sorted. Named sandboxes only; the owner check applies."""
        prefix = self._snap_volume("x").removesuffix("x")  # validates persistence, and gives the volume-name prefix
        self._claim_volume()
        vols = self._docker("volume", "ls", "-q", "--filter", f"label=agvisor.snap={self.name}").stdout.split()
        return sorted(v.removeprefix(prefix) for v in vols)

    def delete_snapshot(self, tag):
        """Remove snapshot `tag` and free its disk. Not an error if it doesn't exist."""
        snap = self._snap_volume(tag)
        self._claim_volume()
        self._docker("volume", "rm", "-f", snap)

    def restore(self, tag):
        """Put the data dir back to snapshot `tag`: whatever is in it now is erased. The container is replaced by a
        fresh one (processes lost), so a bad install is gone from both /workspace and everything else. Returns self.
        If the copy fails the sandbox stays stopped and the data dir is partial: call restore() again, the snapshot is intact."""
        snap = self._snap_volume(tag)
        if self._docker("volume", "inspect", snap, check=False).returncode:  # `run -v` would make an empty one and wipe the data dir with it
            raise ValueError(f"no snapshot {tag!r} of {self.name}")
        self._claim_volume()
        self.stop()
        self._copy(snap, self.name)
        return self.start()

    def __enter__(self):
        return self.start()

    def __exit__(self, *_):
        self.stop()


def _parse_created(created):  # docker's "2026-11-06 09:28:33 +0000 UTC": drop the zone name
    return datetime.strptime(created.rsplit(" ", 1)[0], "%Y-%m-%d %H:%M:%S %z")


def prune_egress(host: str | None = None):
    """Remove the proxies and networks that expired egress= sandboxes left behind (an egress= start does this too)."""
    Sandbox(host=host)._gc_egress()


def list_sandboxes(host: str | None = None, owner: str | None = None) -> list[Sandbox]:
    """Every agvisor sandbox on that docker host, running or not, as handles: exec(), stop() and delete() work.
    Each has `.created` (when its container was made) and `.owner`. `owner=` lists only that owner's.
    Anonymous ones (leaked by a crashed process) are included, so this is also how you find and kill them.
    Don't start() a stopped handle: that makes a fresh default-image container."""
    fmt = "\t".join(["{{.Names}}", "{{.State}}", '{{.Label "agvisor.name"}}', '{{.Label "agvisor.owner"}}', "{{.CreatedAt}}",
                     '{{.Label "agvisor.egress"}}'])
    out = Sandbox(host=host)._docker("ps", "-a", "--filter", "label=agvisor",
                                     *(["--filter", f"label=agvisor.owner={owner}"] if owner else []),
                                     "--format", fmt).stdout
    boxes = []
    for line in out.splitlines():
        cname, state, label, own, created, egress = line.split("\t")  # tabs: CreatedAt has spaces, a label can be empty
        sb = Sandbox(name=label or None, owner=own or None, host=host)
        if not label:
            sb.name = cname  # anonymous: its random container name is all there is
        sb._running, sb._has_egress = state in ("running", "paused"), bool(egress)  # a proxy and a network to remove too
        sb._paused = state == "paused"
        sb.created = _parse_created(created)
        boxes.append(sb)
    return boxes


def create_sandbox(
    image: str | None = None,
    *,
    gui: bool = False,
    limits: Limits = Limits(),
    network: str = "bridge",
    lifetime: int | None = None,
    idle_ttl: int | None = None,
    name: str | None = None,
    owner: str | None = None,
    host: str | None = None,
    runtime: str = "runsc",
    egress: list[str] | None = None,
    inject: dict[str, dict[str, str]] | None = None,
    ports: list[int] | None = None,
    env: dict[str, str] | None = None,
    renewable: bool = False,
    mounts: dict[str, str] | None = None,
) -> Sandbox:
    """Create and start a gVisor sandbox. Use `with create_sandbox(...) as sb:` or call sb.stop() yourself.

    image     Image to run. Default: ubuntu:24.04, or the GNOME desktop image when gui=True.
    gui       True -> GNOME desktop over RDP (sb.rdp_port, sb.password, sb.desktop_token()). start() returns once the
              desktop has booted and RDP listens (~30-60 s).
    limits    Limits(cpus, memory, pids, shm, disk, workspace). Default: 2 cores, 2g RAM, 512 processes, 5g root disk,
              no cap on /workspace. Reusable across sandboxes. `disk` doesn't cover the /workspace volume of a named
              sandbox: Limits(workspace="10g") does, on a docker host set up for it (see the README).
    name      Makes the sandbox resumable. Files in sb.data_dir (/workspace; the gnome home when gui) are kept
              in a docker volume on the host machine and survive stop(), `with` exit and lifetime expiry.
              create_sandbox(name=same) later gets them back: it re-attaches to the container if an earlier
              session left it running (processes and installed packages intact), else makes a fresh one on the
              same volume. sb.delete() erases the data. Default: anonymous, nothing is kept.
              Without owner= there is NO ownership check: anyone who passes the same name gets that sandbox.
              The volume has no size cap unless you pass limits=Limits(workspace=...) (limits.disk covers the rest).
    owner     A label such as a tenant id (same characters as name). A named sandbox's volume is claimed by the
              first owner that creates it, and create_sandbox(name=same, owner=other) (or no owner) raises
              PermissionError instead of re-attaching, restarting or deleting it. list_sandboxes(owner=) lists
              only theirs. It is a guard against mixing up names, not authentication: whoever can run docker on
              the host, or build a Sandbox by hand and call delete(), still gets past it.

    network  "bridge" (internet access, default), "isolated" (internet access, but sandboxes can't connect to each
              other: they share one docker bridge, agvisor-isolated, with inter-container traffic off; it still
              reaches the host and the internet like "bridge", and names resolve), "none" (headless only: gui needs its RDP port) or the
              name of a docker network you created. A host firewall that drops traffic from docker0 blocks the gateway
              from reaching a gui sandbox there: use a network of its own.
    egress   Outbound allowlist, e.g. ["pypi.org", "files.pythonhosted.org", "10.0.0.0/24:8080"]. The sandbox gets a
              private network with no route out, and its only way out is an HTTP(S) proxy (HTTP(S)_PROXY is set) that
              lets those hosts through: `host` or `*.host` (ports 80 and 443 unless `host:port` or `host:*`), an IPv4
              address or a network. A name must resolve to public addresses; reaching a private one takes an IP entry.
              The proxy runs python:3.12-slim (must be on the docker host) under docker's default runtime, runc: under
              gVisor docker's DNS doesn't work. Don't pass network=. Headless only; fixed at creation, like limits.
              `!entry` shuts a host out and wins over every allow ("*.example.com", "!admin.example.com"; its port
              defaults to every port). sb.allow() / sb.deny() change the rules later, sb.egress_denied() lists what was refused.
    inject   Credentials the sandbox can use but never holds: {"api.example.com": {"Authorization": "Bearer sk-..."}}. The
              egress proxy ends the sandbox's HTTPS to that host (a certificate authority made for this sandbox, which it
              trusts, and whose key is dropped at once), sets those headers on every request, and sends it on over TLS of
              its own with the host's real certificate verified. Replaces a header of the same name the sandbox sends. A plain
              host name (no wildcard, address or port); it is also allowed through the proxy, and works for https:// on port
              443 only (http:// to it is refused). One request per connection: no keep-alive, no websockets. Pip, curl,
              requests, node and git trust the CA through the usual environment variables; other clients (java, a program
              that pins certificates) do not. Needs pip install 'agvisor[inject]'. Same restrictions as egress=.
    ports    Container ports to reach from the host, e.g. [8000] for a dev server started with spawn(). Each is published
              on a random free port of 127.0.0.1 on the docker host (never on its other addresses): sb.port(8000) says which.
              With host="ssh://..." that is the remote machine's loopback: tunnel to it (ssh -L). Needs a network the host can
              reach: headless only, not network="none", egress= or inject=. Fixed at creation, like limits. idle_ttl counts
              exec() calls, not connections: use lifetime for a sandbox you are browsing.
    env      Environment variables for everything that runs in the sandbox: {"MODE": "ci"}. They reach docker through its
              stdin, not a command line, so a secret doesn't show in `ps`; but the sandbox holds them: for a credential it
              should only be able to use, see inject=. One line per value. Fixed at creation, like limits: a resumed
              sandbox keeps the environment of its first start. exec(env=) adds more for one command.
    mounts   Host directories (or files) to show the sandbox, read-only: {"/data/models": "/models"} is the docker host's
              /data/models at /models inside. Nothing is copied, so a big dataset costs nothing and changes on the host show
              up inside; the sandbox can read everything under the path, and can't change any of it. With host="ssh://..."
              the path is on that machine. Absolute paths; a source that doesn't exist is an error. Not `/`, not /proc, /sys,
              /dev, /run or a socket. Links inside the directory resolve in the sandbox's own filesystem, so one that points
              at /etc/passwd shows the sandbox's file, not the host's. Fixed at creation. Use put_dir() for a copy it may edit.
    runtime  The docker runtime, default "runsc". Name a second one (e.g. "runsc-gui", same runsc plus the GUI
              flags) to keep the strict one for headless sandboxes. Must be a gVisor runtime: plain runc is
              refused for gui, which runs --privileged.
    lifetime  Seconds until the sandbox destroys itself, even if your process dies. Headless only.
              Default: lives until stop().
    renewable True -> sb.renew(seconds) can move the lifetime's deadline later or earlier. Needs lifetime=. The deadline
              is then a file that PID 1 reads inside the sandbox, so code running in it can extend its own life: leave it
              off when `lifetime` is a budget for code you don't trust. Fixed at creation.
    idle_ttl  Seconds without an exec() before the sandbox destroys itself, even if your process dies. The clock
              restarts when each exec() starts and ends; background processes don't count. exec(timeout=) must be
              shorter. A named sandbox keeps its files, and create_sandbox(name=same) later makes a fresh
              container. Combine with lifetime for a hard cap. Headless only. Fixed at creation.
    host     Which machine supplies the resources. None -> docker on this machine (or $DOCKER_HOST).
              "ssh://user@host" -> docker on a remote VM/server, e.g. an EC2 or Azure VM.
    """
    return Sandbox(image, gui=gui, limits=limits, network=network, lifetime=lifetime, idle_ttl=idle_ttl,
                   name=name, owner=owner, host=host, runtime=runtime, egress=egress, inject=inject, ports=ports, env=env, renewable=renewable, mounts=mounts).start()
