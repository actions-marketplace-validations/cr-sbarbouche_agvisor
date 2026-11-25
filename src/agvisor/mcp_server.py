"""MCP server: a coding agent runs shell commands, edits files and runs Python in a gVisor sandbox instead of on the host.

Configured by environment variables only, so the agent can't pick its own image, network or limits:
AGVISOR_HOST (ssh://user@vm; default local docker / $DOCKER_HOST), AGVISOR_IMAGE, AGVISOR_NETWORK (default "none"),
AGVISOR_CPUS (2), AGVISOR_MEMORY (2g), AGVISOR_IDLE_TTL (600 s), AGVISOR_LIFETIME (3600 s).
AGVISOR_EGRESS (unset): comma-separated hosts the sandbox may reach (create_sandbox(egress=)), e.g.
"pypi.org,files.pythonhosted.org"; it replaces AGVISOR_NETWORK. A `!host` entry shuts one out ("*.example.com,!admin.example.com").
The agent can read what was refused (egress_blocked), and you can allow it while it runs: `agvisor allow NAME host`.
AGVISOR_INJECT (unset): credentials the sandbox can use but never sees, JSON like
{"api.example.com": {"Authorization": "Bearer sk-..."}} (create_sandbox(inject=); needs agvisor[inject]); those hosts are
reachable, and it replaces AGVISOR_NETWORK like AGVISOR_EGRESS does.
AGVISOR_ENV (unset): environment variables for the sandbox, JSON like {"MODE": "ci"} (create_sandbox(env=)). The sandbox holds
them: for a credential it should only use, AGVISOR_INJECT is the way.
AGVISOR_MOUNTS (unset): HOST_DIR:/DEST[,HOST_DIR:/DEST...], directories of the docker host shown to the sandbox read-only
(create_sandbox(mounts=)), e.g. "/data/models:/models". Nothing is copied, and the agent can't change them.
AGVISOR_PORTS (unset): comma-separated container ports to reach from your machine, e.g. "8000,3000" (create_sandbox(ports=));
the agent learns the address from the port tool. Needs AGVISOR_NETWORK=bridge or isolated, and no AGVISOR_EGRESS/AGVISOR_INJECT.
AGVISOR_NAME (unset) makes the sandbox a named one: /workspace is kept in a docker volume, and the snapshot tools
(snapshot, restore, list_snapshots, delete_snapshot) exist. AGVISOR_SNAPSHOTS (3): how many snapshots may exist at once.
AGVISOR_PUT_DIR (unset): HOST_DIR:/DEST, a directory of yours copied into the sandbox when it is made (put_dir), e.g.
"/home/me/proj:/workspace/proj". Skipped when /DEST already exists, so a resumed named sandbox keeps its edits."""
import atexit
import json
import os
import shlex
import threading

from . import egress_proxy
from .limits import Limits
from .sandbox import Job, Sandbox, SandboxGone, create_sandbox

_MAX = 30_000  # characters of output handed back to the agent (the tail)
_READ = 100_000  # read_file(): bytes of one file
_EDIT = 1_000_000  # edit_file(): bytes of one file
_CHECKPOINT = "checkpoint"  # the tag snapshot() and restore() use when the agent names none
_sb = None  # One sandbox per server process. Anonymous unless AGVISOR_NAME is set: then /workspace is a volume with
_lock = threading.Lock()  # no size cap, shared by every server process using that name.


def _env(key, default=None):
    return os.environ.get(f"AGVISOR_{key}") or default


def _t(sb, seconds):  # exec() refuses a timeout that could outlive the idle watchdog: keep it 7 s under
    return max(1, min(seconds, sb.idle_ttl - 7))


def _put():
    """AGVISOR_PUT_DIR as (host dir, destination), or None. Checked when the server starts, not on the agent's first call."""
    spec = _env("PUT_DIR")
    if not spec:
        return None
    host, _, dest = spec.rpartition(":")  # rpartition: a Windows host path has a ':' of its own
    if not os.path.isdir(host) or not dest.startswith("/"):
        raise SystemExit(f"AGVISOR_PUT_DIR must be HOST_DIR:/DEST with HOST_DIR an existing directory, got {spec!r}")
    return host, dest


def _inject():
    """AGVISOR_INJECT as {host: {header: value}}, or None. The values are secrets: an error never repeats them."""
    raw = _env("INJECT")
    if not raw:
        return None
    try:
        return egress_proxy.parse_inject(json.loads(raw))
    except ValueError as e:  # a bad JSON text is one too
        raise SystemExit(f'AGVISOR_INJECT must be JSON like {{"api.example.com": {{"Authorization": "Bearer ..."}}}}: {e}') from None


def _envvars():
    """AGVISOR_ENV as {NAME: value}, or None. The values may be secrets: an error never repeats them."""
    raw = _env("ENV")
    if not raw:
        return None
    try:
        env = json.loads(raw)
        Sandbox(env=env)  # the same checks as create_sandbox()
    except ValueError as e:  # a bad JSON text is one too
        raise SystemExit(f'AGVISOR_ENV must be JSON like {{"MODE": "ci"}}: {e}') from None
    return env


def _mounts():
    """AGVISOR_MOUNTS as {host path: path inside}, or None. Checked when the server starts, not on the agent's first call."""
    raw = _env("MOUNTS")
    if not raw:
        return None
    mounts = {}
    for spec in raw.split(","):
        src, _, dest = spec.strip().rpartition(":")  # rpartition: the destination has no ':', a host path may
        if src in mounts:
            raise SystemExit(f"AGVISOR_MOUNTS lists {src!r} twice")
        mounts[src] = dest
    try:
        Sandbox(mounts=mounts)  # the same checks as create_sandbox()
    except ValueError as e:
        raise SystemExit(f"AGVISOR_MOUNTS must be HOST_DIR:/DEST, comma-separated: {e}") from None
    if not (_env("HOST") or os.environ.get("DOCKER_HOST")):  # docker is on this machine: a missing path is a mistake to report now
        missing = [src for src in mounts if not os.path.exists(src)]
        if missing:
            raise SystemExit(f"AGVISOR_MOUNTS: {', '.join(missing)} does not exist")
    return mounts


def _snapshots():
    """AGVISOR_SNAPSHOTS as an int >= 1: every snapshot is a full copy of /workspace, so the agent gets a fixed number."""
    try:
        n = int(_env("SNAPSHOTS", 3))
    except ValueError:
        n = 0
    if n < 1:
        raise SystemExit(f"AGVISOR_SNAPSHOTS must be a whole number >= 1, got {_env('SNAPSHOTS')!r}")
    return n


def _ports():
    """AGVISOR_PORTS as a list of ints, or None. Checked when the server starts, not on the agent's first call."""
    raw = _env("PORTS")
    if not raw:
        return None
    try:
        ports = [int(p) for p in raw.split(",")]
        Sandbox(ports=ports)  # the same checks as create_sandbox()
    except ValueError as e:
        raise SystemExit(f"AGVISOR_PORTS must be ports like 8000,3000: {e}") from None
    if _env("EGRESS") or _env("INJECT") or _env("NETWORK", "none") == "none":
        raise SystemExit("AGVISOR_PORTS needs AGVISOR_NETWORK=bridge or isolated, and no AGVISOR_EGRESS or AGVISOR_INJECT: "
                         "a port is reached through a network the host can see into")
    return ports


def _get():
    global _sb
    with _lock:
        if _sb is None:
            egress = [e.strip() for e in (_env("EGRESS") or "").split(",") if e.strip()]
            inject = _inject()
            net = {**({"egress": egress} if egress else {}), **({"inject": inject} if inject else {})}
            sb = create_sandbox(
                _env("IMAGE"), host=_env("HOST"), name=_env("NAME"),
                **(net or {"network": _env("NETWORK", "none")}), ports=_ports(), env=_envvars(), mounts=_mounts(),
                limits=Limits(cpus=float(_env("CPUS", 2)), memory=_env("MEMORY", "2g")),
                idle_ttl=int(_env("IDLE_TTL", 600)), lifetime=int(_env("LIFETIME", 3600)))
            try:
                put = _put()
                if put and sb.exec(f"[ -e {shlex.quote(put[1])} ]", timeout=_t(sb, 30), on_output=lambda _: None).returncode:
                    sb.put_dir(*put, timeout=_t(sb, 300))  # only when absent: a resumed volume keeps what the agent changed
            except BaseException:
                sb.stop()
                raise
            _sb = sb
        return _sb


def run(command: str, timeout: int = 120) -> str:
    """Run a shell command (`sh -c`) in an isolated gVisor sandbox. Use this, not the host shell, for code you did not
    write or cannot vouch for. Returns stdout and stderr merged (the last ~30 KB) and the exit code; 124 = timed out.

    The same sandbox is reused between calls (files, installed packages) until it has been idle for a while; after
    that a call reports "sandbox was reset" and the files are gone (with snapshot/restore tools, only /workspace
    is kept). It cannot see the user's files: write code in with
    write_file and print what you need back. Network access is whatever the user configured
    (off by default, or only a few hosts), so `pip install` may fail by design."""
    try:
        sb = _get()
        r = sb.exec(command, timeout=_t(sb, timeout), on_output=lambda _: None)  # streaming bounds memory
    except SandboxGone as e:
        return _gone(e)
    return f"{r.stdout[-_MAX:]}\n[exit {r.returncode}]"


def _gone(e):
    reset()  # no silent retry: the command may have half-run, and the agent should know what is gone
    kept = "Only /workspace is kept" if _env("NAME") else "Its files are gone"
    return f"sandbox was reset ({e}). {kept}; set up again and retry."


def write_file(path: str, content: str) -> str:
    """Write a text file (UTF-8) inside the sandbox: parent directories are created, an existing file is replaced.
    Use it instead of a heredoc, which breaks on quoting and on very long content. Use an absolute path
    (/tmp/a.py, or /workspace/a.py when the sandbox keeps /workspace)."""
    try:
        sb = _get()
        sb.put(path, content.encode(), timeout=_t(sb, 120))
    except SandboxGone as e:
        return _gone(e)
    return f"wrote {len(content.encode())} bytes to {path}"


def read_file(path: str) -> str:
    """Read a text file inside the sandbox (up to 100 KB; for more, use run() with head, tail or grep). Use an
    absolute path, or one relative to /workspace when the sandbox keeps /workspace."""
    try:
        sb = _get()
        data = sb.get(path, max_bytes=_READ, timeout=_t(sb, 120))
    except SandboxGone as e:
        return _gone(e)
    except RuntimeError as e:
        return f"cannot read {path}: {e}"
    except ValueError:
        return f"{path} is bigger than {_READ} bytes: use run() with head, tail or grep"
    return f"{path} is binary ({len(data)} bytes)" if b"\0" in data else data.decode(errors="replace")


def edit_file(path: str, old: str, new: str) -> str:
    """Replace one exact piece of text in a UTF-8 file inside the sandbox. Cheaper and safer than sending the whole
    file again with write_file(). `old` must appear exactly once: include the lines around it to make it unique. Use
    an absolute path, or one relative to /workspace when the sandbox keeps /workspace."""
    if not old:
        return "old must not be empty: use write_file() to create a file"
    try:
        sb = _get()
        text = sb.get(path, max_bytes=_EDIT, timeout=_t(sb, 120)).decode()
        n = text.count(old)
        if n != 1:
            return f"old text not found in {path}" if n == 0 else f"old text appears {n} times in {path}: add the lines around it"
        sb.put(path, text.replace(old, new).encode(), timeout=_t(sb, 120))
    except SandboxGone as e:
        return _gone(e)
    except RuntimeError as e:
        return f"cannot edit {path}: {e}"
    except UnicodeDecodeError:  # first: it is a ValueError
        return f"{path} is not UTF-8 text"
    except ValueError:
        return f"{path} is bigger than {_EDIT} bytes: edit it with run()"
    return f"edited {path}"


def python(code: str, timeout: int = 60, restart: bool = False, context: str = "") -> str:
    """Run Python in the sandbox in a session that keeps variables, imports and functions between calls, like notebook
    cells: load data once, then explore it with small calls. The value of the last expression is printed. Output is
    stdout and stderr merged (the last ~30 KB). At `timeout` seconds the call raises TimeoutError and the session
    survives; `restart=True` starts a fresh session first. The session is lost when the sandbox is reset or
    deleted. Needs python3 in the sandbox image.

    `context` (letters, digits, _ and -; default none) names a separate session with its own variables: use one per
    task when you work on several things at once. They share the sandbox's memory, so don't open many."""
    try:
        sb = _get()
        r = sb.python(code, timeout=max(1, _t(sb, timeout + 5) - 5), restart=restart, context=context or None)  # exec() gives up 5 s after it
    except SandboxGone as e:
        return _gone(e)
    except ValueError as e:  # a context name that isn't valid
        return str(e)
    if r.returncode == 124:  # stuck where the alarm can't reach (a C call): the session was killed
        return f"timed out after {timeout} s and the python session was killed: its variables are gone"
    return (r.stdout or "(no output)") + (f"\n[exit {r.returncode}]" if r.returncode else "")


def list_files(path: str = ".") -> str:
    """List a directory inside the sandbox: permissions, size, date, name (a trailing / marks a directory). Hidden files
    included. A relative path starts in /workspace when the sandbox keeps /workspace, else in /."""
    try:
        sb = _get()
        r = sb.exec(f"ls -lAp --time-style=long-iso -- {shlex.quote(path)}", timeout=_t(sb, 30), on_output=lambda _: None)
    except SandboxGone as e:
        return _gone(e)
    return f"{r.stdout[-_MAX:]}\n[exit {r.returncode}]"


def start_job(command: str, timeout: int | None = None) -> str:
    """Start a shell command in the background of the sandbox and return its job id at once: for what takes longer than
    run() allows (a long scan, a build, a server). Read its output with job_output(). `timeout` (seconds) kills it;
    default: runs until it ends or you kill_job() it. The sandbox is deleted after being idle for a while, and a
    running job goes with it: calls to job_output() count as activity, the job itself does not."""
    try:
        return _get().spawn(command, timeout=timeout).id
    except SandboxGone as e:
        return _gone(e)


def job_output(job_id: str, offset: int = 0) -> str:
    """New output of a background job (stdout and stderr merged) from byte `offset` on, then its state: `[running; next
    offset N]` or `[exit CODE; next offset N]` (124 = hit its timeout, 143 = killed). Pass the next offset back to get
    only what is new. When the state is exit and the output was cut at ~30 KB, call again with the next offset."""
    try:
        job = Job(_get(), job_id)
        rc = job.poll()  # first: once it has ended, the log is complete
        data = job.read(offset, _MAX)
    except SandboxGone as e:
        return _gone(e)
    except (LookupError, ValueError) as e:
        return f"{e}. Jobs are lost when the sandbox is reset."
    return f"{data.decode(errors='replace')}\n[{'running' if rc is None else f'exit {rc}'}; next offset {offset + len(data)}]"


def kill_job(job_id: str) -> str:
    """Stop a background job and what it started. Look at job_output() for how it ended."""
    try:
        Job(_get(), job_id).kill()
    except SandboxGone as e:
        return _gone(e)
    except (LookupError, ValueError) as e:
        return f"{e}. Jobs are lost when the sandbox is reset."
    return f"sent TERM to job {job_id}"


def port(container_port: int) -> str:
    """The address on the user's machine that reaches `container_port` of the sandbox (one the user allowed), for a
    server you started with start_job: give it to the user, who opens it in a browser. Inside the sandbox the same server
    is at localhost:<container_port>. It lives as long as the sandbox, which expires when idle: calls to run() keep it up."""
    try:
        sb = _get()
        return f"127.0.0.1:{sb.port(container_port)}" + (f" on {sb.host}" if sb.host else "")
    except RuntimeError as e:  # docker's own words: that port isn't published, or the container is gone
        return str(e)


def stats() -> str:
    """How much of its limits the sandbox is using right now: CPU, memory, processes. Look when a command died or was
    killed (out of memory?), or before something big. Takes about a second. Memory counts RAM and /tmp (RAM-backed here),
    not files on disk; the process count includes the sandbox's own threads (about 35 when idle)."""
    try:
        sb = _get()
        s = sb.stats()
    except SandboxGone as e:
        return _gone(e)
    mib = lambda n: round(n / 2**20)
    return (f"cpu {s['cpu']:.0f}% (limit {sb.limits.cpus * 100:.0f}%); memory {mib(s['memory'])} of {mib(s['memory_limit'])} MiB; "
            f"processes {s['pids']} (limit {sb.limits.pids})")


def egress_blocked() -> str:
    """The hosts this sandbox tried to reach that the user's network rules refused, as host:port, one per line, in the order
    it first tried. When a command fails on the network (a 403 from the proxy, a failed `pip install`), look here, then tell
    the user which host you need and why: you can't change the rules, they can. A host they have allowed since is not listed."""
    try:
        return "\n".join(_get().egress_denied()) or "nothing was refused"
    except SandboxGone as e:
        return _gone(e)


def reset() -> str:
    """Destroy the sandbox and everything in it. The next run() starts a fresh one. With snapshot/restore tools,
    /workspace survives: use restore() to roll that back."""
    global _sb
    with _lock:
        sb, _sb = _sb, None
    if sb:
        sb.stop()
    return "sandbox destroyed; the next run() starts a fresh one"


def snapshot(tag: str = _CHECKPOINT) -> str:
    """Save /workspace as snapshot `tag` (letters, digits, _ and -; default "checkpoint"), replacing a snapshot of that
    name. Each one costs as much disk as /workspace, and only a few can exist at once: delete_snapshot() frees a slot.
    Don't leave background processes writing to /workspace: a copy taken during writes can be inconsistent."""
    sb = _get()
    try:
        have = sb.list_snapshots()
        if tag not in have and len(have) >= _snapshots():
            return f"already {len(have)} snapshots ({', '.join(have)}): delete_snapshot() one, or reuse one of these tags"
        sb.snapshot(tag)
    except ValueError as e:  # a tag that isn't valid
        return str(e)
    return f"snapshot {tag} saved"


def restore(tag: str = _CHECKPOINT) -> str:
    """Put /workspace back to snapshot `tag`: everything in it now is erased, and the sandbox restarts (running
    processes and anything outside /workspace are lost). The snapshot stays. See list_snapshots() for the tags."""
    try:
        _get().restore(tag)
    except ValueError as e:  # an invalid tag, or none of that name
        return str(e)
    return f"restored /workspace to snapshot {tag}; the sandbox was restarted"


def list_snapshots() -> str:
    """The tags of the snapshots of /workspace, one per line."""
    return "\n".join(_get().list_snapshots()) or "no snapshots"


def delete_snapshot(tag: str) -> str:
    """Delete snapshot `tag` and free its disk. Not an error if there is none of that name."""
    try:
        _get().delete_snapshot(tag)
    except ValueError as e:
        return str(e)
    return f"snapshot {tag} deleted"


def main():
    _put()  # a bad AGVISOR_PUT_DIR, AGVISOR_SNAPSHOTS, AGVISOR_INJECT, AGVISOR_ENV or AGVISOR_MOUNTS stops the server here
    _snapshots()
    _inject()
    _envvars()
    _mounts()
    ports = _ports()
    try:
        from mcp.server.mcpserver import MCPServer  # mcp 2.x: v1's FastMCP was renamed
    except ImportError:
        raise SystemExit("agvisor-mcp needs the mcp extra (mcp>=2): pip install 'agvisor[mcp]'")
    app = MCPServer("agvisor")
    app.tool()(run)
    app.tool()(write_file)
    app.tool()(read_file)
    app.tool()(edit_file)
    app.tool()(python)
    app.tool()(list_files)
    app.tool()(start_job)
    app.tool()(job_output)
    app.tool()(kill_job)
    app.tool()(reset)
    app.tool()(stats)
    if _env("EGRESS") or _env("INJECT"):  # a sandbox with an allowlist: say what it refused
        app.tool()(egress_blocked)
    if ports:
        app.tool()(port)
    if _env("NAME"):  # snapshots need a named sandbox
        app.tool()(snapshot)
        app.tool()(restore)
        app.tool()(list_snapshots)
        app.tool()(delete_snapshot)
    atexit.register(reset)  # ponytail: skipped on SIGKILL; idle_ttl/lifetime delete the container anyway
    app.run()  # stdio
