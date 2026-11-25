# agvisor

[![PyPI Downloads](https://static.pepy.tech/personalized-badge/agvisor?period=total&units=INTERNATIONAL_SYSTEM&left_color=BLACK&right_color=GREEN&left_text=downloads)](https://pepy.tech/projects/agvisor)

gVisor (`runsc`) sandboxes for coding agents. Thin wrapper over the `docker` CLI: CPU/memory/pids/disk limits, named sandboxes with snapshots, an outbound allowlist that can also inject credentials, background jobs, files in and out, an asyncio interface, a warm pool, pause and resume, read-only mounts of host directories, an MCP server, and an optional GNOME desktop over RDP.

Two packages, versioned separately:

| package | what | install |
|---|---|---|
| [`agvisor`](https://pypi.org/project/agvisor/) (PyPI) | the Python library: `create_sandbox(...)`, limits, `list_sandboxes`, `agvisor.aio`, `Pool` | `pip install agvisor` |
| [`agvisor-desktop`](https://www.npmjs.com/package/agvisor-desktop) (npm) | the `<agvisor-desktop>` web component that shows a GUI sandbox in a web page ([details](#gui-in-a-web-page-react-nextjs-vue-plain-html)) | `npm install agvisor-desktop` |

> **Alpha, and not security-audited.** Read [Security](#security) before running code you don't trust.

## Host setup (Linux VM or server)

Install [gVisor](https://gvisor.dev/docs/user_guide/install/), then register the runtime in `/etc/docker/daemon.json` and `systemctl reload docker` (a *restart* bounces every running container unless `live-restore` is on).
The two `runtimeArgs` are only needed for GUI sandboxes (systemd needs cgroup v2, `sudo` needs suid). Register them as a **second** runtime, so headless sandboxes keep the strict one, and pass `runtime="runsc-gui"` with `gui=True`:

```json
{"runtimes": {"runsc": {"path": "/usr/local/bin/runsc"},
              "runsc-gui": {"path": "/usr/local/bin/runsc",
                            "runtimeArgs": ["--in-sandbox-cgroup=v2", "--allow-suid"]}}}
```

(Putting the args on `runsc` itself also works and needs no `runtime=`, but then every sandbox on the host gets them.)

**To cap `/workspace`** (`Limits(workspace="10g")`, named sandboxes), docker's volumes directory must be on xfs mounted with project quotas. Without it, `create_sandbox` raises rather than leave the volume uncapped. On a spare partition or a loop file, with docker stopped (anything already in `/var/lib/docker/volumes` is hidden by the mount: copy it over first):

```sh
sudo systemctl stop docker docker.socket
sudo mkfs.xfs /dev/sdX            # or: truncate -s 100G /xfs.img && mkfs.xfs /xfs.img
echo '/dev/sdX /var/lib/docker/volumes xfs defaults,pquota 0 0' | sudo tee -a /etc/fstab   # loop file: /xfs.img … xfs loop,pquota 0 0
sudo mount /var/lib/docker/volumes && sudo systemctl start docker
```

**On AWS/Azure/GCP VMs:** with `network="bridge"` a sandbox can reach the cloud metadata endpoint (`169.254.169.254`) and read the VM's instance credentials. Block it on the host (CI checks this against a fake metadata service, not a real instance): `sudo iptables -I DOCKER-USER -d 169.254.169.254 -j DROP`, or use `network="none"`.

GUI image (x86_64), from the [gVisor systemd post](https://gvisor.dev/blog/2026/09/17/systemd-in-gvisor/):

```sh
git clone https://github.com/google/gvisor.git && cd gvisor
docker build -t systemd-debian-gnome \
  -f images/systemd-user/systemd-debian-gnome/Dockerfile.x86_64 images/systemd-user/systemd-debian-gnome
```

The image is ~8 GB and the build takes ~30 min on 2 vCPUs (checked on a 2 vCPU / 4 GB VM: peak ~1.5 GB RAM). A desktop needs about 0.7 GB RAM idle, so give it `memory="1536m"` or more.

**Host firewall:** if yours drops traffic from `docker0` (e.g. `INPUT -i docker0 -j DROP`, a common way to keep sandboxes away from the host), the gateway can't reach a desktop on the default bridge: guacd hangs without an error. Create a network for desktops (`docker network create desktops`) and pass `network="desktops"`. Your egress rules don't apply to it, so don't run untrusted code there.

## Use

```python
from agvisor import Limits
from agvisor.sandbox import create_sandbox   # creates AND starts the sandbox

SMALL = Limits(cpus=1, memory="512m", pids=128)   # define once, reuse for any number of sandboxes

with create_sandbox(limits=SMALL, lifetime=3600) as sb:                      # headless
    r = sb.exec("python3 -c 'print(1+1)'", timeout=30)
    print(r.returncode, r.stdout)

with create_sandbox(gui=True, limits=Limits(cpus=4, memory="6g")) as sb:     # desktop
    print(f"RDP 127.0.0.1:{sb.rdp_port}  user=gnome  password={sb.password}")
    input("press enter to destroy")

sb = create_sandbox(host="ssh://user@my-vm")   # same thing, on a remote VM/server (e.g. EC2, Azure)
sb.stop()                                      # without `with`, you stop it yourself
```

| param | meaning |
|---|---|
| `image` | default `ubuntu:24.04`, or the GNOME image when `gui=True` |
| `gui` | GNOME desktop over RDP, with a random RDP password (not the image's `gnome:gnome`). `start()` waits for the desktop to boot: ~30-60 s |
| `runtime` | docker runtime, default `runsc`. With a separate GUI runtime: `"runsc-gui"`. Must be a gVisor runtime: plain `runc` is refused for `gui` (it runs `--privileged`, which is only simulated under gVisor) |
| `limits` | `Limits(cpus, memory, pids, shm, disk, workspace)`: default 2 cores, `2g`, 512 processes, docker's `/dev/shm` size, `5g` root disk, no cap on `/workspace`. Zero or negative values are rejected (docker and runsc would read 0 as unlimited) |
| `network` | `"bridge"` (internet, default), `"isolated"` (internet, but sandboxes can't connect to each other), `"none"` (headless only) or the name of a docker network you created |
| `egress` | an allowlist of hosts the sandbox may reach, instead of all or nothing: see [Limiting outbound traffic](#limiting-outbound-traffic). Headless only; don't combine with `network=` |
| `lifetime` | seconds until the sandbox deletes itself, even if your process dies (headless only) |
| `idle_ttl` | seconds without an `exec()` before the sandbox deletes itself, even if your process dies (headless only). Expiry can lag by up to ~5s. See below |
| `renewable` | with `lifetime`: `sb.renew(seconds)` can move the deadline later or earlier, see [Expiry and cleanup](#expiry-and-cleanup). Off by default, because it lets code inside extend its own life |
| `env` | environment variables for the sandbox, `{"MODE": "ci"}`: see below |
| `mounts` | host directories shown to the sandbox read-only, `{"/data/models": "/models"}`: see [Showing a host directory](#showing-a-host-directory-read-only) |
| `name` | makes the sandbox resumable, see below |
| `host` | `None` = docker on this machine (or `$DOCKER_HOST`); `"ssh://user@host"` = docker on a remote VM/server |

`sb.exec(cmd, input=None, timeout=120)` runs `sh -c cmd` inside and returns a `CompletedProcess`; on timeout the command is killed and `returncode` is 124. That relies on GNU `timeout`, so use Debian/Ubuntu-based images (`python:3.12-slim`, `ubuntu`). On Alpine (busybox) the code is 143 and only the shell is killed: the children of a compound command (`a; b`) keep running. An image's `ENTRYPOINT` is ignored: agvisor clears it for headless sandboxes, because it would take agvisor's own `sleep infinity` as arguments (`exec` never ran it anyway). If your entrypoint script did setup work, that no longer happens.

`env=` on `create_sandbox` sets variables for everything that runs in the sandbox, and `sb.exec(cmd, env={...}, cwd="/some/dir")` adds some for one command (`spawn()` takes both too). They reach docker through its stdin or a file only you can read, never a command line, so a secret does not show in `ps` on your machine. One line per value, names like shell variables. The sandbox *holds* what you give it: for a key it should only be able to use, see [`inject=`](#credentials-the-sandbox-can-use-but-not-read). Like `limits`, `env=` is fixed when the sandbox is made: re-attaching to a running named one keeps the environment of its first start.

`sb.stats()` says how much of the limits a sandbox is using right now: `{"cpu": 101.4, "memory": 254803968, "memory_limit": 2147483648, "pids": 74}` (CPU in percent of one core, memory in bytes; it takes about a second, since CPU needs two samples). Raises `SandboxGone` if the container is gone. `memory` counts RAM and `/tmp`, which is RAM-backed, but not files on the root filesystem or in `/workspace`; `pids` is what `--pids-limit` counts (below). `tests/test_gvisor_stats.py` checks each of these against real gVisor.

Under gVisor, the limits behave differently from plain docker (checked on real `runsc`):
- `pids` budgets the sandbox runtime's *host threads*, not guest processes: an idle sandbox already uses ~35. Keep the default (512): at 64–128, a fork storm made `fork` fail with `Out of memory` and, pushed harder, killed the sandbox.
- Going over `memory` or `pids` kills the **whole sandbox**, not one process, and the host doesn't notice. Afterwards `exec()` raises `SandboxGone` (it also does once `lifetime` or `idle_ttl` has deleted the container), so a dead sandbox isn't mistaken for a failed command: it recognises docker's and runsc's error text and only looks at exit codes 1 and 128. Call `create_sandbox(name=...)` again: it starts a fresh container on the same `/workspace` volume.
- `disk` caps the **root filesystem** (runsc's `overlay2` annotation, checked on release-20261026.0): past it, writes fail with `No space left on device` and the sandbox keeps running. Whole number + `k/m/g/t`: `1.5g` and `2gb` are rejected. It does **not** cover `/tmp`, which gVisor backs with RAM (counted in `memory`), nor the `/workspace` volume of a named sandbox: that has its own cap, `workspace` (below), which is off by default, and without it a runaway agent can fill the host's disk through the volume. `tests/test_gvisor_disk.py` checks the `disk` cap against real gVisor.
- `workspace` caps the data volume of a named sandbox (`/workspace`; the home for `gui=True`), as an xfs project quota that docker sets on the volume (`docker volume create --opt size=`): past it, writes fail with `No space left on device`, and the sandbox keeps running. Same syntax as `disk`. It needs the host setup under [Host setup](#host-setup-linux-vm-or-server); on a host without it `create_sandbox` raises. Like the owner, the cap belongs to the volume and is fixed when it is created: asking for a different one later raises `ValueError` (`delete()` the sandbox first, which erases its files), and a volume made without a cap can't be capped afterwards. Asking for no cap on a capped volume leaves the cap. A snapshot is a second volume with no cap of its own (at most as big as the capped volume it copies). `tests/test_gvisor_workspace.py` checks it against real gVisor on xfs.
- Inside, `uname -r` reports `4.19.0-gvisor` and `/sys/fs/cgroup` and `/sys/class/net` don't exist (use `/proc/net/dev`).

### Resuming a sandbox

```python
with create_sandbox("python:3.12", name="my-project") as sb:   # session 1
    sb.exec("git clone https://github.com/me/repo && python -m venv .venv && .venv/bin/pip install -r repo/requirements.txt")
# the container is gone (CPU/RAM freed), /workspace is kept

with create_sandbox("python:3.12", name="my-project") as sb:   # session 2, days later
    sb.exec("ls -a")                                            # -> repo/ .venv/   (cwd is /workspace)

create_sandbox(name="my-project").delete()          # forget it: erases the data too
```

To undo a bad install that landed in `/workspace`, save a known-good state and go back to it:

```python
with create_sandbox("python:3.12", name="my-project") as sb:
    sb.exec("python -m venv .venv && .venv/bin/pip install -r requirements.txt")
    sb.snapshot("clean")                            # copies /workspace into a second volume
    sb.exec(".venv/bin/pip install some-sketchy-package")
    sb.restore("clean")                             # /workspace is exactly as it was; new container, processes lost
```

- `snapshot(tag)` replaces a snapshot of the same tag; `restore(tag)` erases whatever is in the data dir now. `list_snapshots()` returns the tags, `delete_snapshot(tag)` frees one, and `delete()` removes them all with the sandbox. Tags: letters, digits, `_`, `-`.
- Named sandboxes only, and the owner check applies. The copy runs in a throwaway gVisor container from the sandbox's image (it needs `sh`, `rm`, `cp`), with no network.
- Each snapshot costs as much host disk as the data dir (so at most `workspace`, if you capped it). A running sandbox is paused while it is copied, so the snapshot is the data dir at one moment, not a mix of before and after a write: its processes stop for those few seconds, and an `exec()` meanwhile fails. An application that makes one change in several writes can still be caught between them.

- **Kept:** everything under `sb.data_dir` (`/workspace`; the `gnome` home for `gui=True`), in a docker volume `agvisor-<name>`.
- **Not kept:** processes, and anything outside `data_dir` (e.g. `apt install`): gVisor discards the rootfs overlay when the container is destroyed. Work in `/workspace` (venvs, repos, caches), or bake system packages into your image.
- **Keep everything:** don't call `stop()`. The container stays running and the next `create_sandbox(name=...)` re-attaches to it with processes and installed packages intact. (`limits`/`image` are then ignored: they were fixed at creation.)
- The volume lives on the docker host. On a cloud VM, keep `/var/lib/docker` on a persistent disk (EBS / managed disk), not instance-store, and resume on the same VM. Don't set `--overlay2=all:*` in the `runsc` args: it would stop volumes persisting.

For a remote host the RDP port is on that host's `127.0.0.1`: tunnel it with `ssh -L`.
`network="none"` cuts network access (headless only: GUI needs the published port).

`network="isolated"` keeps the internet but stops sandboxes from connecting to each other: they share one docker bridge, `agvisor-isolated`, created with inter-container traffic off. It does not touch `docker0`, so a host firewall written for `docker0` doesn't see these sandboxes: add rules for the new bridge (`br-…`, see `docker network inspect agvisor-isolated`). Docker's embedded DNS doesn't work under gVisor, so agvisor gives each isolated sandbox the same resolver servers a sandbox on `"bridge"` gets. Files were never shared between sandboxes; this is only about the network.

### Limiting outbound traffic

```python
sb = create_sandbox("python:3.12-slim", egress=["pypi.org", "*.pythonhosted.org", "10.0.0.0/24:8080"])
sb.exec("pip install requests")      # works: the host is on the list
sb.exec("curl https://example.com")  # 403 from the proxy: "example.com:443 is not in this sandbox's egress allowlist"
```

The sandbox gets a private docker network (`--internal`: no route out) and the only thing on it besides the sandbox is a small allowlist proxy, which also sits on the default bridge. `HTTP_PROXY`/`HTTPS_PROXY` point at it, so `pip`, `apt`, `curl` and Python's `urllib` just work; code that ignores the variables has no route anywhere. Sandboxes with `egress=` can't reach each other either: each has its own network.

- Entries are `host` (exact), `*.host` (subdomains, not `host` itself), an IPv4 address or an IPv4 network, with an optional `:port`. Ports default to 80 and 443; `host:*` is any port. There is no allow-everything entry: that is `network="bridge"`.
- `!entry` shuts a host out and wins over every allow: `egress=["*.example.com", "!admin.example.com"]`. Its port defaults to *every* port (an allow's is 80 and 443), so `!example.com` is all of it. At least one entry must be an allow.
- **A name only gets through if it resolves to public addresses**, so a name can't be used to reach `10.x`, the docker host or the cloud metadata endpoint (`169.254.169.254`). Reaching a private address takes an entry that says so (`10.0.0.5`, `10.0.0.0/24`). The proxy connects to the address it checked, so a second lookup can't swap it.
- It is an HTTP(S) proxy: raw sockets (SYN scans, ICMP, UDP) don't pass.
- A tunnel (HTTPS, or any `CONNECT`) that carries no byte in either direction for 15 minutes is closed, so a stalled connection doesn't hold a socket until the sandbox goes. A download the client says nothing during is not quiet.
- **The proxy runs under docker's default runtime (`runc`), not gVisor.** Under gVisor docker's embedded DNS (`127.0.0.11`, which every container on a user-defined network uses) doesn't work, so a proxy there can't resolve a single name. It is the one agvisor component outside gVisor, and it parses bytes the sandbox sends it: about 150 lines of Python, run as `nobody`, read-only, all capabilities dropped, 256 MB. The sandbox itself stays under gVisor. For the same reason the sandbox is given the proxy's IP, not a name, and can't resolve any name itself.
- The proxy image is `python:3.12-slim` (a constant in `sandbox.py`): it has to be on the docker host or pullable. It needs a password because the default bridge can reach it: the password goes to docker on stdin, not on a command line, and is in the sandbox's environment (`HTTPS_PROXY`), where it is useless to anything that can't reach the proxy. `docker logs agvisor_proxy_<name>` shows one `allow` / `deny` line per connection.
- A sandbox that expires (`lifetime`, `idle_ttl`) can't remove its proxy and network. The next `egress=` start does, and so do `agvisor prune` and `prune_egress()`: a proxy whose sandbox is gone is removed once it is 10 minutes old (its sandbox's image may still be downloading), then the networks nothing is attached to (those older than a minute). `stop()`, `delete()` and `agvisor rm` remove the proxy and network at once. Docker has ~30 default network address pools, so ~30 `egress=` sandboxes at once; widen `default-address-pools` in `daemon.json` for more.
- Starting costs about six more docker calls: on `host="ssh://..."` that is noticeable (enable `ControlMaster`). Fixed at creation, like `limits`: re-attaching to a running named sandbox ignores `egress=`.
- To give the sandbox a credential without handing it over, see [Credentials the sandbox can use but not read](#credentials-the-sandbox-can-use-but-not-read) below.
- Checked on real gVisor (runsc 20261102.0, docker 29.1.3, WSL2): HTTPS and plain HTTP to listed hosts, a 403 that names the rule for the rest, no route around the proxy, no name resolution inside, `pip install` through it, and the cleanup above. `tests/test_gvisor_egress.py` runs that against a docker host.

#### Changing the rules while it runs

```python
sb = create_sandbox("python:3.12-slim", egress=["pypi.org"])
sb.exec("pip install requests")      # fails: files.pythonhosted.org isn't on the list
sb.egress_denied()                   # ["files.pythonhosted.org:443"]: what it was refused, in the order it tried
sb.allow("*.pythonhosted.org")       # at once, no restart: the same sandbox, its processes and files untouched
sb.exec("pip install requests")      # works
sb.deny("pypi.org")                  # and back; tunnels that are open to it are closed
sb.egress_rules()                    # ["!pypi.org", "*.pythonhosted.org"]
```

The decision is yours, not the code's in the sandbox: it has no API to the rules (they are a file in the proxy's container, and the sandbox can only reach the proxy's port). `allow(*entries)` takes the same entries as `egress=` and un-denies one that was denied; `deny(*entries)` removes the entry and puts a `!entry` in its place, so it also carves a name out of a wildcard. `egress_denied()` leaves out what is allowed by now, so it is the list to show whoever approves. From a shell: `agvisor allow NAME host...`, `agvisor deny NAME host...`, `agvisor egress NAME [--denied]`. The MCP server's agent gets a read-only `egress_blocked()` tool (with `AGVISOR_EGRESS` or `AGVISOR_INJECT`), so it can tell you which host it needs; only you can allow it.

- Changes apply to new connections at once (the proxy reloads on a signal), and close the open tunnels that the new rules no longer let through, so a `deny()` also stops a download in progress. A bad rules file is ignored: the old rules stay.
- `egress_denied()` reads the proxy's log (one line per connection): fine for a long session, a few MB per million connections. Two callers changing rules at the same moment can lose one update: one controller per sandbox.
- Needs `egress=` or `inject=` at creation (`ValueError` otherwise), and `SandboxGone` once the sandbox has expired and its proxy is gone. `tests/test_gvisor_egress_live.py` runs all of this against real gVisor.

#### Credentials the sandbox can use but not read

```python
sb = create_sandbox("python:3.12-slim", inject={"api.example.com": {"Authorization": "Bearer sk-..."}})
sb.exec("curl https://api.example.com/v1/me")   # the request carries the header; the sandbox never held the key
```

`inject={host: {header: value}}` (needs `pip install 'agvisor[inject]'`) builds on `egress=`: the proxy ends the sandbox's HTTPS to that host, sets the headers on every request (replacing one of the same name that the sandbox sent), and sends it on over TLS of its own, with the host's real certificate verified. The host is allowed through the proxy by being named; `egress=` can add others (plain tunnels, as before).

- The sandbox trusts a certificate authority made for it on your machine. Its private key is dropped as soon as it has signed the one certificate the proxy needs (for the injected hosts), so nothing else can ever be signed with it. The proxy gets that certificate and its key through docker's stdin, and loads it from a tmpfs. The authority is valid for 30 days: make the sandbox again after that.
- The credential stays out of the sandbox: not in its environment, its files or its command lines. It sits in the proxy container's environment, so whoever can run `docker inspect` on the host can read it, as with any secret passed to a container. A sandbox can still *use* it, for whatever that host allows, and if the service echoes its headers back, it can read it that way.
- Plain host names only, no wildcards, addresses or ports. `https://` on port 443 only: `http://` to such a host is refused, not sent in the clear. Other ports of the same host are plain tunnels, with no injection.
- Before a request gets a credential, the proxy checks that it has a path (no absolute URL in the request line, which could steer a shared front end to another site), a single `Host` header naming the injected host, and no folded headers. A server whose certificate does not verify as that host never sees the credential.
- One request per connection (`Connection: close`, as for plain `http://` already), and no HTTP/2 or websockets to those hosts: clients fall back to HTTP/1.1 and reconnect. The sandbox's `SSL_CERT_FILE`, `REQUESTS_CA_BUNDLE`, `CURL_CA_BUNDLE` and `GIT_SSL_CAINFO` point at its own certificates plus the authority, and `NODE_EXTRA_CA_CERTS` at the authority: Python, `requests`, `curl`, `git`, Go and Node trust it. Java, and programs that pin certificates, do not. The image needs a CA bundle at `/etc/ssl/certs/ca-certificates.crt` or `/etc/pki/tls/certs/ca-bundle.crt`: without one, `create_sandbox` raises.
- `AGVISOR_INJECT` sets it for the MCP server (below), so the agent has the credential's use and not its value.
- `tests/test_args.py` runs the proxy end to end against fake servers (the credential is set, replaced, withheld from a server that doesn't verify, refused over http); `tests/test_inject_gvisor.py` runs it on real gVisor.

### Files in and out

```python
sb.put("/tmp/job/input.csv", open("input.csv", "rb").read())   # parent dirs are created, an existing file is replaced
data = sb.get("/tmp/job/result.json")                          # bytes; a relative path starts in /workspace for a named sandbox
```

Both go through `exec()` (base64 over stdin/stdout), so they need GNU coreutils in the image (Debian/Ubuntu), raise `SandboxGone` if the sandbox is dead and `RuntimeError` if the write or read fails. `get()` reads regular files only and raises `ValueError` past `max_bytes` (default 10 MB): the sandbox is untrusted, so what comes out of it is capped. `tests/test_files.py` runs both against a real container.

```python
sb.put_dir("./project", "/workspace/project")   # a whole directory in: created, same-named files replaced, others kept
sb.get_dir("/workspace/out", "./results")       # and out (max_bytes=50 MB of tar)
```

They are a tar through `put()`/`get()` (GNU tar in the image). `put_dir` copies links as links and never follows them, so a link on your side can't pull a host file into the sandbox, and resets owners. `get_dir` extracts what an untrusted sandbox sends with tarfile's `data` filter: nothing lands outside the destination, no device files, no setuid bits (`tarfile.FilterError` otherwise). Links that point outside the destination, every absolute one (a venv's `python`), are left out rather than followed or made an error: `get_dir` returns their names. From a shell: `agvisor put NAME ./file-or-dir /remote/path` and `agvisor get NAME /remote/path ./local`.

### Showing a host directory (read-only)

```python
sb = create_sandbox("python:3.12-slim", mounts={"/data/models": "/models", "/srv/ref.csv": "/etc/ref.csv"})
sb.exec("ls /models; wc -l /etc/ref.csv")      # the docker host's own files: nothing was copied
sb.exec("echo x > /models/new")                # Read-only file system: it can't change any of it, not even as root
```

For a big dataset or a codebase you would rather not tar through `put_dir()`: a read-only bind mount, so it costs nothing to start and changes on the host show up inside. A directory or a single file; absolute paths; a source that doesn't exist is an error (docker's `--volume` would have made an empty directory owned by root). With `host="ssh://..."` the path is on that machine. Fixed at creation.

- **The sandbox can read everything under the path**, hidden files included. Links inside it resolve in the sandbox's own filesystem, so a link to `/etc/passwd` shows the sandbox's file, not yours. Don't mount `~`, `/etc` or a directory with secrets in it.
- Refused: `/`, anything under `/proc`, `/sys`, `/dev`, a source under `/run` or `/var/run`, and any `*.sock` (a docker socket would be the host). That is a guard against mistakes by whoever chose the paths, not against them: they decide what is shared.
- Use `put_dir()` when the sandbox should be able to edit its copy. `tests/test_gvisor_mounts.py` checks all of this on real gVisor.

### Background jobs

```python
job = sb.spawn("python scan.py targets.txt", timeout=36000)   # returns at once; it outlives this exec() and your process
while (rc := job.poll()) is None:                              # or job.wait(timeout=...)
    chunk = job.read(offset); offset += len(chunk)             # bytes since `offset`, stdout and stderr merged
job.kill()                                                     # TERM to the command and what it started; kill(force=True) = KILL
Job(sb, job_id).wait()                                         # pick a job up again after your process restarted
```

`exec(timeout=)` has to stay under `idle_ttl`, so anything longer goes here. A job writes to `/var/tmp/.agvisor-jobs/<id>/` (the root filesystem, so `Limits.disk` caps its log). Exit codes: 124 = hit its `timeout`, 143 = killed (TERM), 137 = KILL. `wait()` makes one `exec()` per ~25 s. A running job does **not** keep an `idle_ttl` sandbox alive (background processes never did), but `poll()`, `read()` and `wait()` are execs, so a client that checks on it does; set `lifetime` for a hard budget. `Limits.disk` is the only cap on how much a job logs. `tests/test_gvisor_jobs.py` runs this against real gVisor.

### Reaching a server inside

```python
with create_sandbox("python:3.12-slim", ports=[8000], lifetime=3600) as sb:
    sb.spawn("python3 -m http.server 8000")                  # a dev server, or anything that listens
    print(f"http://127.0.0.1:{sb.port(8000)}")               # the same server, from this machine
```

`ports=` publishes each container port on a random free port of `127.0.0.1` on the docker host, never on its other addresses; `sb.port(8000)` says which. It is fixed when the sandbox is made, and a resumed sandbox keeps the ports of its first start. It needs a network the host can reach into: headless, `network="bridge"` or `"isolated"` (a sandbox on `"none"`, or behind `egress=` / `inject=`, has no way in). With `host="ssh://..."` the port is on that machine's loopback: `ssh -L 8000:127.0.0.1:<port> user@vm`. `idle_ttl` counts `exec()` calls, not connections, so give a sandbox you are browsing a `lifetime`. `tests/test_gvisor_ports.py` runs this against real gVisor.

### Python sessions

```python
sb.python("import csv; rows = list(csv.reader(open('/tmp/job/input.csv')))")   # an interpreter that stays up between calls
r = sb.python("len(rows)")                                                      # r.stdout: stdout and stderr merged, tail only
sb.python("rows[:3]", timeout=30, restart=False)                                # restart=True throws the variables away
```

Like notebook cells: variables, imports and functions survive between calls, and the value of a last expression is printed. `r` is a `CompletedProcess`: a Python exception is a traceback in `r.stdout`, not a return code. At `timeout` (default 60 s) the code gets a `TimeoutError` and the session survives; stuck where that can't reach it (a C call), the session is killed and `returncode` is 124. 127 means no `python3` in the image (`python:3.12-slim` has it). `max_bytes` (default 30 KB) cuts what comes back, inside the sandbox. The interpreter is a process in the container, so it goes with it: a new one starts (and says so) after a reset, `lifetime` or `idle_ttl`. Each call is one `exec()`, so `timeout + 5` must stay under `idle_ttl`. It is also an MCP tool, `python`. `tests/test_gvisor_mcp.py` runs it against real gVisor.

`sb.python(code, context="analysis")` is a session of its own: each name is a separate interpreter with its own variables, so two lines of work don't share globals, and `restart=True` or a timeout that kills one leaves the others. No `context` is the original session. Names are letters, digits, `_` and `-` (up to 32). Each costs the memory of an idle interpreter until the sandbox ends (all of it counts in `limits.memory`). The MCP tool takes `context` too. `tests/test_gvisor_pycontexts.py` runs it against real gVisor.

### Async

`agvisor.aio` has the same sandboxes for `asyncio` code: every blocking call runs in a worker thread, so the event loop stays free and calls can run side by side.

```python
import asyncio
from agvisor import aio

async def main():
    async with aio.AsyncSandbox("python:3.12-slim", lifetime=600) as sb:
        a, b = await asyncio.gather(sb.exec("sleep 2; echo a"), sb.exec("sleep 2; echo b"))   # ~2 s, not 4
        job = await sb.spawn("echo building; sleep 1; echo done")
        print(await job.wait(), (await job.read()).decode())

asyncio.run(main())
```

- `AsyncSandbox(...)` takes `Sandbox`'s arguments; `await aio.create_sandbox(...)` starts it without `async with`, and `aio.list_sandboxes()` returns `AsyncSandbox` handles. Methods, results and errors are the same as the synchronous ones, awaited. Attributes (`name`, `data_dir`, ...) are read as usual.
- Cancelling an await (`asyncio.wait_for`, a cancelled task) stops waiting, not the command: it runs on in the sandbox until its own `timeout=` ends it. Pass a `timeout`.
- `exec(on_output=)` can be a plain function (called in a worker thread) or an `async def` (awaited on your loop).

### Warm pool

`Pool` keeps sandboxes started and unused, so `acquire()` doesn't wait for `docker run` and what comes with it (the image check, and on a remote `host=ssh://...` the round trips of starting one): it costs one quick `exec`, a health check. Taking one starts a replacement in the background.

```python
from agvisor import Pool

with Pool(2, "python:3.12-slim", network="none", lifetime=600) as pool:   # two ready at all times
    with pool.acquire() as sb:                                            # at once; yours to stop()
        sb.exec("echo hi")
```

- Same arguments as `create_sandbox()`, shared by every sandbox of the pool; anonymous and headless only (no `name=`, `owner=`, `gui=`). `lifetime=` or `idle_ttl=` is required: it ends spares that a crashed process leaves behind, and it counts from when a spare was made, not from `acquire()`. A spare that expired while it waited is skipped.
- A spare is a running sandbox: it holds an idle sandbox's memory (tens of MB under gVisor) until acquired or `close()`d. When none is ready, `acquire()` makes one on the spot, so a pool never makes you wait longer than not having one.
- From `asyncio`: `sb = await asyncio.to_thread(pool.acquire)`.

### A browser inside

Chromium runs under gVisor, so a page you didn't write can be opened, read and screenshotted without it touching your machine. Use [Playwright's image](https://mcr.microsoft.com/en-us/artifact/mar/playwright/python/about), which has the browsers, and install the matching Python package in the sandbox ([`examples/08_browser.py`](https://github.com/cr-sbarbouche/agvisor/blob/main/examples/08_browser.py)):

```python
with create_sandbox("mcr.microsoft.com/playwright/python:v1.65.2-noble", network="isolated",
                    limits=Limits(memory="2g", shm="1g")) as sb:
    sb.exec("pip install -q playwright==1.65.2", timeout=300)
    sb.put("/tmp/browse.py", script)         # sync_playwright(): chromium.launch(), page.goto(), page.screenshot(...)
    sb.exec("python3 /tmp/browse.py https://example.com")
    png = sb.get("/tmp/page.png")
```

- Set `Limits(shm="1g")`: Chromium needs more than docker's 64 MB `/dev/shm`. Playwright's own launch flags are enough; the sandbox is gVisor's, not Chromium's.
- Needs internet for the page and for `pip`, so `network="bridge"` or `"isolated"` (the example uses `"isolated"`: other sandboxes stay out of reach, the host and cloud metadata do not).
- To let the browser reach only some hosts, use `egress=` and hand the proxy to Chromium, which ignores `HTTPS_PROXY`: parse that variable in the script and pass `chromium.launch(proxy={"server": ..., "username": ..., "password": ...})` ([`examples/09_browser_egress.py`](https://github.com/cr-sbarbouche/agvisor/blob/main/examples/09_browser_egress.py)). A host that is not listed fails with `net::ERR_TUNNEL_CONNECTION_FAILED`.
- Playwright's version must match the browsers in the image (the tag, `v1.65.2` above). The image is ~2 GB.
- CI runs both examples against real gVisor.

### Expiry and cleanup

```python
from agvisor import list_sandboxes

sb = create_sandbox(name="u42", idle_ttl=900, lifetime=4 * 3600)   # gone after 15 idle minutes, or 4h at most
for sb in list_sandboxes(host="ssh://user@my-vm"):                 # every agvisor sandbox on that docker host
    print(sb.name, sb.created); sb.stop()                          # exec(), stop() and delete() work on these handles
```

- `idle_ttl`: the clock restarts when each `exec()` starts and ends; background processes don't count. `exec(timeout=)` must be shorter than `idle_ttl` (else `ValueError`), so a command can't outlive the sandbox. It is fixed at creation, like `limits`.
- `renewable=True` (with `lifetime=`): `sb.renew(seconds)` makes it expire `seconds` from now instead, later or earlier, as often as you like (`agvisor renew NAME SECONDS`). It costs the hard cap: PID 1 reads the deadline from a file inside the sandbox, so **code running there can extend its own life** by writing it. A plain `lifetime` stays a budget it can't touch; use `renewable` when you decide what runs. The deadline is wall-clock, a pause counts against it, and `idle_ttl` still applies on top. `tests/test_gvisor_renew.py` runs it against real gVisor.
- `sb.pause()` freezes every process (no CPU; the memory stays held: it is a freeze, not a checkpoint to disk) until `sb.resume()`; `exec()` on a paused sandbox raises `SandboxPaused`, not an exit code. `agvisor pause NAME` / `resume NAME`, and `agvisor ls` shows `paused`. `idle_ttl` does not run out while it is paused (the watchdog notices a freeze and restarts its clock), but `lifetime` keeps counting: a sandbox paused past it is gone when you resume. `tests/test_gvisor_pause.py` checks the freeze, both clocks and the processes' state on real gVisor.
- A named sandbox that expires keeps its files: the next `create_sandbox(name=...)` makes a fresh container on the same volume. `delete()` is what erases them: run it from your own retention job.
- `list_sandboxes()` also finds anonymous sandboxes left behind by a crashed process. Don't `start()` a stopped handle: it would make a fresh default-image container.
- `sb.created` is a timezone-aware `datetime` (second precision) of when the container was made, so a retention job can stop what is older than it should be: `[sb.stop() for sb in list_sandboxes() if now - sb.created > max_age]`. Sandboxes from `create_sandbox()` have `created = None`.
- `owner=` tags a sandbox, e.g. with a tenant id: `create_sandbox(name="u42-scan7", owner="u42")`, and `list_sandboxes(owner="u42")` lists only theirs. See the `name=` note under Security for what it does and doesn't protect.
- `agvisor ls`, `agvisor rm`, `agvisor exec` do the same from a shell (add `--host ssh://user@vm` for a remote one, `--owner` to filter): `ls` prints name, state, owner and creation time; `rm NAME...` and/or `rm --older-than SECONDS` removes sandboxes (never everything at once: it needs one of the two), `--data` also erases the files and snapshots of named ones; `exec NAME cmd...` runs a command in a running sandbox and exits with its exit code; `put` and `get` copy files and directories (see Files in and out). A retention job is `agvisor rm --older-than 14400` in cron. `exec`, `put` and `get` don't count as activity for `idle_ttl`. `agvisor prune` removes what expired `egress=` sandboxes left behind. `agvisor pause`, `resume`, `renew`, `allow`, `deny` and `egress` are described above; `exec` takes `--env NAME[=VALUE]` and `--cwd`, and `run` takes `--env` and `--mount HOSTPATH:REMOTE` too.

## Examples

[`examples/`](https://github.com/cr-sbarbouche/agvisor/tree/main/examples) has eleven small, runnable scripts (quickstart, resuming and snapshots, limits, files and jobs, the egress allowlist, `network="isolated"`, the `/workspace` cap, a headless browser with and without `egress=`, Claude Code's CLI with its API key out of reach, and a one-sandbox-per-trial evaluation harness). CI runs all of them against real gVisor.

## Coding agents (skill + MCP server)

Two optional pieces for Claude Code, VS Code (Copilot agent mode) and OpenCode:

- **Skill**, [`.claude/skills/agvisor/SKILL.md`](https://github.com/cr-sbarbouche/agvisor/blob/main/.claude/skills/agvisor/SKILL.md): tells the agent when to sandbox, plus the gotchas above. Copy the `agvisor` folder to `.claude/skills/` (all three read it), or to `.github/skills/` / `.agents/skills/`.
- **MCP server**, `agvisor-mcp`: `run(command, timeout)`, `write_file(path, content)`, `read_file(path)`, `edit_file(path, old, new)` (one exact, unique piece of text replaced), `list_files(path)`, `python(code, timeout, restart, context)` (a session that keeps variables between calls, like notebook cells: needs `python3` in `AGVISOR_IMAGE`; the interpreter is lost when the sandbox is reset), `start_job(command)`/`job_output(job_id, offset)`/`kill_job(job_id)` for what outlives `run()`, `stats()` (CPU, memory and processes against their limits: what to look at after a command died), and `reset()` (plus the snapshot tools, opt-in, below), so the agent runs code in the sandbox, not on your host. It makes one sandbox on first use and keeps it until it has been idle for `AGVISOR_IDLE_TTL`. A skill only advises; to make the sandbox the *only* way the agent runs code, also turn off its host shell tool (if your client lets you).

Settings are environment variables, so the agent can't pick its own image, network or limits:

| variable | default |
|---|---|
| `AGVISOR_HOST` | local docker / `$DOCKER_HOST`; `ssh://user@vm` for a remote Linux host |
| `AGVISOR_NETWORK` | `none` (no internet: `pip install` fails). `bridge` for internet |
| `AGVISOR_EGRESS` | unset. Comma-separated hosts the sandbox may reach, e.g. `pypi.org,files.pythonhosted.org`: [`egress=`](#limiting-outbound-traffic), instead of `AGVISOR_NETWORK` |
| `AGVISOR_INJECT` | unset. Credentials the sandbox can use but not read, as JSON: `{"api.example.com": {"Authorization": "Bearer sk-..."}}` ([`inject=`](#credentials-the-sandbox-can-use-but-not-read), needs `agvisor[inject]`). Those hosts are reachable; it replaces `AGVISOR_NETWORK` like `AGVISOR_EGRESS`, and can be combined with it |
| `AGVISOR_PORTS` | unset. Comma-separated container ports to reach from your machine, e.g. `8000,3000` ([`ports=`](#reaching-a-server-inside)); adds a `port(container_port)` tool that returns the address. Needs `AGVISOR_NETWORK=bridge` or `isolated`, without `AGVISOR_EGRESS` / `AGVISOR_INJECT` |
| `AGVISOR_ENV` | unset. Environment variables for the sandbox as JSON: `{"MODE": "ci"}` ([`env=`](#use)). The sandbox holds them: for a credential it should only use, `AGVISOR_INJECT` |
| `AGVISOR_MOUNTS` | unset. `HOST_DIR:/DEST[,HOST_DIR:/DEST...]`: directories of the docker host shown to the sandbox read-only, e.g. `/data/models:/models` ([`mounts=`](#showing-a-host-directory-read-only)). Nothing is copied, and the agent can't change them |
| `AGVISOR_IMAGE` | `ubuntu:24.04` |
| `AGVISOR_CPUS` / `AGVISOR_MEMORY` | `2` / `2g` |
| `AGVISOR_IDLE_TTL` / `AGVISOR_LIFETIME` | `600` / `3600` seconds |
| `AGVISOR_PUT_DIR` | unset. `HOST_DIR:/DEST`: a directory of yours copied into the sandbox when it is made (`put_dir`), e.g. `/home/me/proj:/workspace/proj`. Skipped when `/DEST` already exists, so a resumed named sandbox keeps its edits; an anonymous one gets the host's copy again after a reset. The whole directory goes through one tar in memory: keep it small (no `node_modules`, `.git`). Take results back with `agvisor get NAME /DEST ./out` |
| `AGVISOR_NAME` | unset. Set it and `/workspace` is kept in a docker volume, and four more tools appear (below) |
| `AGVISOR_SNAPSHOTS` | `3`. With `AGVISOR_NAME`: how many snapshots may exist at once |

By default the sandbox is anonymous: no `/workspace` volume, so nothing is shared between projects, and the sandbox can't see your files (the agent writes code in with `write_file`).

With `AGVISOR_NAME=my-project` the agent also gets `snapshot(tag)`, `restore(tag)`, `list_snapshots()` and `delete_snapshot(tag)`: copies of `/workspace` it can save and roll back to, e.g. before a risky `pip install` (no tag means `checkpoint`). `reset()` then keeps `/workspace` (use `restore()` to roll that back). Mind the cost: `/workspace` is a volume with no size cap, shared by every server process that uses that name, and each snapshot is a full copy of it, up to `AGVISOR_SNAPSHOTS` of them (a fourth is refused until the agent deletes one). Claude Code:

```sh
claude mcp add agvisor -e AGVISOR_HOST=ssh://user@my-vm -- uvx --from 'agvisor[mcp]' agvisor-mcp
```

VS Code, `.vscode/mcp.json`:

```json
{"servers": {"agvisor": {"type": "stdio", "command": "uvx", "args": ["--from", "agvisor[mcp]", "agvisor-mcp"],
                         "env": {"AGVISOR_HOST": "ssh://user@my-vm"}}}}
```

OpenCode, `opencode.json`:

```json
{"mcp": {"servers": {"agvisor": {"type": "local", "command": ["uvx", "--from", "agvisor[mcp]", "agvisor-mcp"],
                                 "environment": {"AGVISOR_HOST": "ssh://user@my-vm"}}}}}
```

## GUI in a web page (React, Next.js, Vue, plain HTML)

Browsers can't speak RDP, so there is a gateway in between: `browser <-wss-> gateway/gateway.js <-> guacd <-RDP-> sandbox`.
Start both on the docker host with the compose file (`pip install 'agvisor[web]'` for the token helper):

```sh
export AGVISOR_KEY=$(openssl rand -hex 32)   # shared by the gateway and your backend; compose refuses to start without it
docker compose up -d --build                 # guacd + gateway, both on 127.0.0.1; gateway on :8090 (GATEWAY_PORT to change)
```

The compose file pins both images by digest, binds `guacd` to loopback (its image defaults to `0.0.0.0`, an open RDP proxy on host networking), and runs the gateway as a non-root, read-only container. Linux only: both use the host network. Without Docker: `cd gateway && npm install && npm start`, plus `guacd` bound to `127.0.0.1`.

Put the gateway behind your TLS reverse proxy (so the page uses `wss://`) and your auth. In your backend, after the user is authorised:

```python
sb = create_sandbox(gui=True)
token = sb.desktop_token(bytes.fromhex(os.environ["AGVISOR_KEY"]), ttl=60)  # server-side only: it holds the RDP password
# return {"gateway": "wss://your.host/desktop", "token": token} to the page
```

In the page, give the element a size and pass both values. The component is the npm package [`agvisor-desktop`](https://www.npmjs.com/package/agvisor-desktop) (source in [`web/`](https://github.com/cr-sbarbouche/agvisor/tree/main/web); its only dependency is `guacamole-common-js`, installed with it):

```sh
npm install agvisor-desktop
```

```jsx
import "agvisor-desktop";   // React / Next.js ('use client'): safe to import during SSR, it does nothing on the server
<agvisor-desktop gateway={gateway} token={token} style={{ width: "100%", height: 600 }} />
```

```html
<!-- Vue: compilerOptions.isCustomElement = (tag) => tag === "agvisor-desktop"; plain HTML: <script type="module"> -->
<agvisor-desktop :gateway="gateway" :token="token" style="width:100%;height:600px"></agvisor-desktop>
```

A new `token` reconnects. The element fires an `error` event (`e.detail` = message) when the connection fails or drops, e.g. a rejected or expired token: mint a fresh one and set it. TypeScript needs a JSX `IntrinsicElements` entry for the tag.
A token only has to be valid when the session *opens*, but anyone holding it can open one until `ttl` runs out.
In `web/`: `npm test` checks the token round trip against the real gateway with a fake guacd; `npm run test:browser` runs the component in real Chrome (render, mouse, keyboard, resize, errors, cleanup).

## GitHub Action

Run an agent, a test suite or any untrusted step in a gVisor sandbox on a GitHub-hosted `ubuntu-latest` runner. The action installs `runsc`, copies the job's files into `/workspace`, runs your script there and fails the step with the script's exit code.

```yaml
- uses: actions/checkout@v7
- uses: cr-sbarbouche/agvisor@v0.1.9
  env:
    ANTHROPIC_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
  with:
    image: python:3.12-slim
    egress: |
      pypi.org
      files.pythonhosted.org
    inject: api.anthropic.com:x-api-key=ANTHROPIC_KEY   # the script can call the API; it never holds the key
    output: /workspace/out                              # copied to ./agvisor-out, even if the script fails
    run: |
      pip install -r requirements.txt
      python agent.py
- uses: actions/upload-artifact@v5
  if: always()
  with: {name: agent-out, path: agvisor-out}
```

Inputs: `image` and `run` (required), `workspace` (default `.`, `""` for none), `output`, `egress`, `inject`, `cpus` (2), `memory` (2g), `timeout` (600 s). With no `egress` the network is open, as with `create_sandbox()`. Under the hood it is `agvisor run IMAGE --egress ... --inject HOST:HEADER=ENVVAR --put ... --get ... -- COMMAND`, which also works on any machine with docker and `runsc`.

- The secret goes in the calling step's `env:` and `inject:` names the variable, so it never appears on a command line and the sandbox never holds it (see [credentials the sandbox can use but not read](#credentials-the-sandbox-can-use-but-not-read)).
- The `workspace` copy is a tar through `docker exec`: a repository with large binaries makes it slow. The image must be Debian/Ubuntu-based (GNU `timeout` and `tar`).
- Headless only: no GNOME desktop (the image is 7.6 GB). A headless browser works ([A browser inside](#a-browser-inside)).
- `ubuntu-latest` only: the runner needs `sudo` and docker. `runsc` is pinned in `action.yml`.
- `.github/workflows/ci.yml` runs the action on every PR (job `action`): the allowlist, the injected credential, the file copy in and out.

### On your own machine instead of GitHub's

Any repository can use the action (this one is public); the sandbox just has to run on a machine that has docker and `runsc` set up as in [Host setup](#host-setup-linux-vm-or-server). Two ways:

**1. A self-hosted runner on that machine** (simplest). [Register the VM as a runner](https://docs.github.com/en/actions/hosting-your-own-runners) with a label, put its user in the `docker` group, and point the job at it:

```yaml
jobs:
  agent:
    runs-on: [self-hosted, gvisor]
    steps:
      - uses: actions/checkout@v7
      - uses: cr-sbarbouche/agvisor@v0.1.9
        with: {image: "python:3.12-slim", run: "python agent.py"}
```

The `runsc` install step is skipped when docker already has the runtime. The runner also needs `python3-venv`. Only the `run:` script is sandboxed: checkout and the other steps run on the machine itself, so don't take pull requests from forks on a persistent runner (GitHub's own advice for self-hosted runners).

**2. A GitHub-hosted (or any) runner that drives the machine over ssh**, with `host:`. Only the sandbox runs on your VM, and the runner stays disposable:

```yaml
    steps:
      - uses: actions/checkout@v7
      - run: |
          install -m 700 -d ~/.ssh
          echo "$SSH_KEY" > ~/.ssh/id_ed25519 && chmod 600 ~/.ssh/id_ed25519
          echo "$SSH_KNOWN_HOSTS" > ~/.ssh/known_hosts
        env: {SSH_KEY: "${{ secrets.VM_SSH_KEY }}", SSH_KNOWN_HOSTS: "${{ vars.VM_KNOWN_HOSTS }}"}
      - uses: cr-sbarbouche/agvisor@v0.1.9
        with: {host: "ssh://ci@vm.example.com", image: "python:3.12-slim", run: "python agent.py"}
```

The machine must be reachable from the runner. **Whoever holds that ssh key can run docker on the VM, which is root there**: give it a VM of its own and a key used for nothing else, and keep ssh off the open internet (GitHub's runner address ranges are large and public, so a VPN or tailnet that the job joins first is the safer way in).

Either way: the sandbox removes itself `timeout` + 5 minutes after it started even if the runner dies mid-job (`agvisor prune` clears a leftover egress proxy), every sandbox gets its own network when `egress`/`inject` is set (about 30 at once, see `egress=`), and nothing the job leaves behind is on the machine except docker's cached images. Don't use a machine that serves something else: a sandbox on `bridge` (no `egress`) can reach whatever the host's network can.

## Security

**This is alpha software and nobody has audited it.** gVisor shrinks the part of the kernel that sandboxed code can reach; it is not a guarantee, and this library's own code has not been reviewed for flaws. Don't use it as the boundary for hostile multi-tenant workloads without a review of your own, and keep `runsc` up to date (tested with release-20261026.0). Known gaps:

- **Whoever can run `docker` on the host controls every sandbox**, and is effectively root on that host. agvisor adds no authentication or authorization: put your own in front of it.
- **`name=` has no ownership check unless you pass `owner=`.** `create_sandbox(name="x")` re-attaches to whatever running container is called `agvisor-x`, removes a stopped one of that name, and `list_sandboxes()` returns every sandbox on the host. If a tenant can choose the name, they can attach to, run commands in, stop and `delete()` another tenant's sandbox. With `create_sandbox(name="x", owner="tenant-a")`, the first owner claims the sandbox's data volume, and any other `owner=` (or none) gets a `PermissionError` before anything is re-attached, removed or deleted. That is a guard against mixed-up names in your own orchestrator, not authentication: `owner` is whatever string the caller passes, and anyone who can run `docker` or build a `Sandbox` by hand and call `delete()` gets around it. Build the names and owners yourself, from your own user ids, never from tenant input. A named sandbox created without `owner=` (including any made before 0.1.2) stays unowned: Docker can't add a label to an existing volume, so a call with `owner=` is refused, and `delete()` it first (that erases its files) to put it under an owner.
- **`/workspace` has no size cap unless you set one.** `Limits(disk=)` only caps the root filesystem. The `/workspace` volume of a named sandbox is a plain host directory, so a runaway or hostile process can fill the host's disk through it. `Limits(workspace="10g")` caps it, but needs a host with `/var/lib/docker/volumes` on xfs with `pquota` (see Host setup): without it the cap isn't available, and by default there is none. Anonymous sandboxes have no volume: their writes fall under `disk` (and RAM-backed `/tmp` under `memory`). Also put `/var/lib/docker` on a disk of its own, so a full disk doesn't take other services down with it.
- **Sandboxes on one docker network can reach each other.** That is Docker's default: every sandbox on `"bridge"` (the default) or on a network you share between tenants can connect to the others' open ports. Use `network="isolated"` (one shared bridge with inter-container traffic off, tested in CI on real runsc), `network="none"`, or a separate network per tenant or run.
- **`network="bridge"` also reaches the outside:** the internet, services listening on the host itself (through the bridge gateway), and on a cloud VM the metadata endpoint that serves instance credentials (see Host setup). Block what you don't want on the host's firewall, or use `network="none"`.
- **`egress=` limits what a sandbox can connect to, not what it can say.** Allowed hosts can receive anything (data can ride in a URL to an allowed host), and an allowed name that an attacker controls the DNS of (a `*.` entry on a domain someone else can add subdomains to) can serve anything. On the one setup I tried (docker 29.1.3, runsc, WSL2) the sandbox couldn't resolve any name itself, so DNS is not a side channel there; other docker or gVisor versions are untested. The proxy container runs under `runc`, outside gVisor (see above). The proxy and the network are plain docker objects: whoever can run `docker` can read the proxy password and the allowlist.
- **`inject=` puts a TLS-ending proxy in the path of the sandbox's requests.** The proxy parses TLS and HTTP bytes that the sandbox sends it, under docker's `runc` rather than gVisor (see `egress=`), and holds the private key of the certificate the sandbox is told to trust, for the injected hosts only: whoever takes over the proxy can impersonate those hosts to that one sandbox and read the credentials in its environment. The credentials are in the proxy container's environment, so whoever can run `docker inspect` on the host can read them. A sandbox can use a credential for anything its host accepts, and read it back if the service echoes request headers. Inject the narrowest credential the task needs.
- **`mounts=` shows host files to code you don't trust.** It is read-only, but read-only is all it is: everything under the path can be read, including secrets in it, and a kernel or runsc bug that gives write access gives it to the host's files. Mount the narrowest directory that does the job. Source paths under `/run`, `/var/run`, `/proc`, `/sys`, `/dev` and `*.sock` are refused as a guard against mistakes, not as a boundary.
- **`renewable=True` lets the sandbox extend its own `lifetime`** (the deadline is a file inside it). Leave it off when `lifetime` is the budget for hostile code. `env=` values are in the sandbox's environment: it holds them (unlike `inject=`).
- **`sb.allow()` and the CLI's `agvisor allow` widen what a sandbox can reach.** There is no way to call them from inside the sandbox, and the MCP server doesn't expose them to the agent; whoever can run docker on the host can change any proxy's rules anyway.
- **`gui=True` runs `--privileged`.** Under gVisor that only grants simulated capabilities; under plain `runc` it would be real, so agvisor refuses `runc` for GUI sandboxes. The RDP password is random per sandbox and the gateway token is encrypted and authenticated, but anyone holding a token can open a session until it expires.

To report a vulnerability, email sadokbarbouche1@outlook.com instead of opening a public issue (details in `SECURITY.md`).

## CI and dependency updates

- `.github/workflows/ci.yml` runs on every PR, every push to `main`, and weekly (new advisories appear without commits):
  `test` (Python tests, the gateway token round trip, and the component in real Chrome), `audit` (`pip-audit` on the locked Python deps, `npm audit` + registry-signature check on `web/`), `semgrep` (code scan of the Python, the JS and the workflows), `snyk` (the same dependency check against Snyk's database; only on `main` and the weekly run, to stay inside the free tier's test quota; needs the `SNYK_TOKEN` repo secret).
- `.github/dependabot.yml` opens weekly update PRs for `uv`, `npm` and the GitHub Actions themselves (minor+patch grouped, 7-day cooldown). Those PRs run the same checks.
- `.github/workflows/publish-npm.yml` publishes `web/` (`agvisor-desktop`) to npm with Trusted Publishing: push a tag `web-vX.Y.Z` that equals the version in `web/package.json` and is on `main`. Run by hand, it only builds, tests and lists the package. Setup steps are at the top of the file.
- Not configurable from a workflow, set these in the repo settings: make `test`, `audit` and `semgrep` required checks on `main`, and turn on Dependabot alerts.
- Run it locally: `uv run python tests/test_args.py`, then in `web/`: `npm test` and `npm run test:browser` (after `npm ci` in both `web/` and `gateway/`), plus `uvx pip-audit …`, `uvx semgrep …` (exact commands in `ci.yml`).
