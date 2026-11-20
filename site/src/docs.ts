// Docs content, condensed from the README. Each page links to its README section for the full text.
// Inline: `code` and **bold**. In code strings: "\\" is one backslash and "\${" is a literal "${".
export type Block =
  | { p: string }
  | { h: string }
  | { code: string; file?: string }
  | { ul: string[] }
  | { table: string[][] } // first row is the header
  | { note: string; warn?: boolean };

export type Page = { slug: string; group: string; title: string; blurb: string; readme: string; body: Block[] };

export const repository = 'https://github.com/cr-sbarbouche/agvisor';

export const pages: Page[] = [
  {
    slug: 'overview',
    group: 'Getting started',
    title: 'Overview',
    blurb: 'gVisor sandboxes for coding agents, as a thin wrapper over the docker CLI.',
    readme: '#readme',
    body: [
      { p: 'agvisor runs code in gVisor (`runsc`) sandboxes through the `docker` CLI. Use it from Python, from a shell, from a coding agent over MCP, or from a GitHub workflow.' },
      { note: 'Alpha, and not security-audited. Read Security before you run code you don’t trust.', warn: true },
      { h: 'What you get' },
      {
        ul: [
          'CPU, memory, process and disk limits per sandbox.',
          'Named sandboxes whose `/workspace` survives, with snapshots.',
          'An outbound allowlist that can also inject credentials the sandbox can use but not read.',
          'Background jobs, files in and out, a Python session, published ports.',
          'An `asyncio` interface, a warm pool, an MCP server and a GitHub Action.',
          'An optional GNOME desktop over RDP.',
        ],
      },
      { h: 'Install' },
      { code: "$ pip install agvisor\n$ pip install 'agvisor[mcp]'   # adds agvisor-mcp, the MCP server", file: 'terminal' },
      { p: 'A sandbox needs a Linux host with Docker and gVisor: macOS can’t run `runsc`. Set the host up first, then point agvisor at it with `host="ssh://user@vm"` if it is not the machine you are on.' },
      { h: 'Two packages, versioned separately' },
      {
        table: [
          ['package', 'what', 'install'],
          ['`agvisor` (PyPI)', 'The Python library: `create_sandbox(...)`, limits, `list_sandboxes`, `agvisor.aio`, `Pool`', '`pip install agvisor`'],
          ['`agvisor-desktop` (npm)', 'The `<agvisor-desktop>` web component that shows a GUI sandbox in a web page', '`npm install agvisor-desktop`'],
        ],
      },
    ],
  },
  {
    slug: 'host-setup',
    group: 'Getting started',
    title: 'Host setup',
    blurb: 'Install gVisor, register the runtime, and prepare the docker host.',
    readme: '#host-setup-linux-vm-or-server',
    body: [
      { p: 'Install [gVisor](https://gvisor.dev/docs/user_guide/install/), then register the runtime in `/etc/docker/daemon.json` and run `systemctl reload docker`. A *restart* bounces every running container unless `live-restore` is on.' },
      { p: 'The two `runtimeArgs` are only for GUI sandboxes (systemd needs cgroup v2, `sudo` needs suid). Register them as a **second** runtime so headless sandboxes keep the strict one, and pass `runtime="runsc-gui"` with `gui=True`.' },
      {
        code: `{"runtimes": {"runsc": {"path": "/usr/local/bin/runsc"},
              "runsc-gui": {"path": "/usr/local/bin/runsc",
                            "runtimeArgs": ["--in-sandbox-cgroup=v2", "--allow-suid"]}}}`,
        file: '/etc/docker/daemon.json',
      },
      { h: 'Cap /workspace' },
      { p: '`Limits(workspace="10g")` needs docker’s volumes directory on xfs mounted with project quotas. Without it, `create_sandbox` raises rather than leave the volume uncapped. On a spare partition or a loop file, with docker stopped (copy anything already in `/var/lib/docker/volumes` over first, because the mount hides it):' },
      {
        code: `$ sudo systemctl stop docker docker.socket
$ sudo mkfs.xfs /dev/sdX            # or: truncate -s 100G /xfs.img && mkfs.xfs /xfs.img
$ echo '/dev/sdX /var/lib/docker/volumes xfs defaults,pquota 0 0' | sudo tee -a /etc/fstab
$ sudo mount /var/lib/docker/volumes && sudo systemctl start docker`,
        file: 'terminal',
      },
      { h: 'AWS, Azure and GCP VMs' },
      { p: 'With `network="bridge"` a sandbox can reach the cloud metadata endpoint (`169.254.169.254`) and read the VM’s instance credentials. Block it on the host, or use `network="none"`.' },
      { code: '$ sudo iptables -I DOCKER-USER -d 169.254.169.254 -j DROP', file: 'terminal' },
      { h: 'GUI image' },
      { p: 'x86_64, built from the gVisor repository. The image is about 8 GB and the build takes about 30 minutes on 2 vCPUs. A desktop needs about 0.7 GB of RAM idle, so give it `memory="1536m"` or more.' },
      {
        code: `$ git clone https://github.com/google/gvisor.git && cd gvisor
$ docker build -t systemd-debian-gnome \\
    -f images/systemd-user/systemd-debian-gnome/Dockerfile.x86_64 images/systemd-user/systemd-debian-gnome`,
        file: 'terminal',
      },
      { note: 'If your host firewall drops traffic from `docker0`, the gateway can’t reach a desktop on the default bridge and guacd hangs without an error. Create a network for desktops (`docker network create desktops`) and pass `network="desktops"`. Your egress rules don’t apply to it, so don’t run untrusted code there.' },
    ],
  },
  {
    slug: 'quickstart',
    group: 'Getting started',
    title: 'Quickstart',
    blurb: 'Create a sandbox, run a command, and know what the parameters do.',
    readme: '#use',
    body: [
      {
        code: `from agvisor import Limits
from agvisor.sandbox import create_sandbox   # creates AND starts the sandbox

SMALL = Limits(cpus=1, memory="512m", pids=128)   # define once, reuse for any number of sandboxes

with create_sandbox(limits=SMALL, lifetime=3600) as sb:                      # headless
    r = sb.exec("python3 -c 'print(1+1)'", timeout=30)
    print(r.returncode, r.stdout)

with create_sandbox(gui=True, limits=Limits(cpus=4, memory="6g")) as sb:     # desktop
    print(f"RDP 127.0.0.1:{sb.rdp_port}  user=gnome  password={sb.password}")
    input("press enter to destroy")

sb = create_sandbox(host="ssh://user@my-vm")   # same thing, on a remote VM/server (e.g. EC2, Azure)
sb.stop()                                      # without \`with\`, you stop it yourself`,
        file: 'quickstart.py',
      },
      { h: 'Parameters' },
      {
        table: [
          ['param', 'meaning'],
          ['`image`', 'Default `ubuntu:24.04`, or the GNOME image when `gui=True`.'],
          ['`gui`', 'GNOME desktop over RDP with a random RDP password. `start()` waits for the desktop to boot: about 30–60 s.'],
          ['`runtime`', 'Docker runtime, default `runsc`. It must be a gVisor runtime: plain `runc` is refused for `gui`.'],
          ['`limits`', '`Limits(cpus, memory, pids, shm, disk, workspace)`. Default: 2 cores, `2g`, 512 processes, `5g` root disk, no cap on `/workspace`. Zero or negative values are rejected.'],
          ['`network`', '`"bridge"` (internet, default), `"isolated"` (internet, but sandboxes can’t connect to each other), `"none"` (headless only) or the name of a docker network you created.'],
          ['`egress`', 'An allowlist of hosts the sandbox may reach, instead of all or nothing. Headless only; don’t combine with `network=`.'],
          ['`lifetime`', 'Seconds until the sandbox deletes itself, even if your process dies (headless only).'],
          ['`idle_ttl`', 'Seconds without an `exec()` before the sandbox deletes itself (headless only). Expiry can lag by about 5 s.'],
          ['`name`', 'Makes the sandbox resumable.'],
          ['`host`', '`None` = docker on this machine (or `$DOCKER_HOST`); `"ssh://user@host"` = docker on a remote VM or server.'],
        ],
      },
      { h: 'exec' },
      { p: '`sb.exec(cmd, input=None, timeout=120)` runs `sh -c cmd` inside and returns a `CompletedProcess`. On timeout the command is killed and `returncode` is 124. That relies on GNU `timeout`, so use Debian or Ubuntu based images (`python:3.12-slim`, `ubuntu`). On Alpine the code is 143 and only the shell is killed.' },
      { p: 'An image’s `ENTRYPOINT` is ignored: agvisor clears it for headless sandboxes, because it would take agvisor’s own `sleep infinity` as arguments.' },
      { h: 'stats' },
      { p: '`sb.stats()` says how much of the limits a sandbox is using right now. CPU is in percent of one core, memory in bytes, and it takes about a second because CPU needs two samples. It raises `SandboxGone` if the container is gone.' },
      { code: '{"cpu": 101.4, "memory": 254803968, "memory_limit": 2147483648, "pids": 74}', file: 'sb.stats()' },
    ],
  },
  {
    slug: 'limits',
    group: 'Guides',
    title: 'Limits',
    blurb: 'What each limit caps, and how gVisor differs from plain docker.',
    readme: '#use',
    body: [
      { p: 'Define limits once with `Limits(cpus, memory, pids, shm, disk, workspace)` and reuse them for any number of sandboxes. They are fixed when the sandbox is created.' },
      { code: 'from agvisor import Limits\n\nLimits(cpus=1, memory="512m", pids=128, disk="5g", workspace="10g")', file: 'limits.py' },
      { p: 'Under gVisor the limits behave differently from plain docker (checked on real `runsc`):' },
      {
        ul: [
          '**`pids`** budgets the runtime’s *host threads*, not guest processes: an idle sandbox already uses about 35. Keep the default (512). At 64–128, a fork storm made `fork` fail with `Out of memory` and, pushed harder, killed the sandbox.',
          '**Going over `memory` or `pids` kills the whole sandbox**, not one process, and the host doesn’t notice. Afterwards `exec()` raises `SandboxGone`. Call `create_sandbox(name=...)` again for a fresh container on the same `/workspace` volume.',
          '**`disk`** caps the root filesystem. Past it, writes fail with `No space left on device` and the sandbox keeps running. Whole number plus `k/m/g/t`: `1.5g` and `2gb` are rejected. It does not cover `/tmp` (RAM-backed, counted in `memory`) or the `/workspace` volume.',
          '**`workspace`** caps the data volume of a named sandbox as an xfs project quota. It needs the host setup, and raises on a host without it. The cap belongs to the volume and is fixed when it is created.',
          'Inside, `uname -r` reports `4.19.0-gvisor`, and `/sys/fs/cgroup` and `/sys/class/net` don’t exist (use `/proc/net/dev`).',
        ],
      },
      { note: 'Without a `workspace` cap, a runaway agent can fill the host’s disk through the volume of a named sandbox. Cap it, and put `/var/lib/docker` on a disk of its own.', warn: true },
    ],
  },
  {
    slug: 'resume',
    group: 'Guides',
    title: 'Resuming and snapshots',
    blurb: 'Keep /workspace between sessions, and roll it back.',
    readme: '#resuming-a-sandbox',
    body: [
      { p: 'Give a sandbox a `name` and `/workspace` is kept in a docker volume, `agvisor-<name>`, after the container is gone.' },
      {
        code: `with create_sandbox("python:3.12", name="my-project") as sb:   # session 1
    sb.exec("git clone https://github.com/me/repo && python -m venv .venv && .venv/bin/pip install -r repo/requirements.txt")
# the container is gone (CPU/RAM freed), /workspace is kept

with create_sandbox("python:3.12", name="my-project") as sb:   # session 2, days later
    sb.exec("ls -a")                                            # -> repo/ .venv/   (cwd is /workspace)

create_sandbox(name="my-project").delete()          # forget it: erases the data too`,
        file: 'resume.py',
      },
      { h: 'Snapshots' },
      { p: 'To undo a bad install that landed in `/workspace`, save a known-good state and go back to it.' },
      {
        code: `with create_sandbox("python:3.12", name="my-project") as sb:
    sb.exec("python -m venv .venv && .venv/bin/pip install -r requirements.txt")
    sb.snapshot("clean")                            # copies /workspace into a second volume
    sb.exec(".venv/bin/pip install some-sketchy-package")
    sb.restore("clean")                             # /workspace is exactly as it was; new container, processes lost`,
        file: 'snapshot.py',
      },
      {
        ul: [
          '`snapshot(tag)` replaces a snapshot of the same tag; `restore(tag)` erases whatever is in the data dir now. `list_snapshots()` returns the tags, `delete_snapshot(tag)` frees one.',
          'Named sandboxes only. Each snapshot costs as much host disk as the data dir.',
          'A running sandbox is paused while it is copied, so an `exec()` meanwhile fails.',
        ],
      },
      { h: 'What is kept' },
      {
        ul: [
          '**Kept:** everything under `sb.data_dir` (`/workspace`; the `gnome` home for `gui=True`).',
          '**Not kept:** processes, and anything outside `data_dir` such as `apt install`. Work in `/workspace` (venvs, repos, caches) or bake system packages into your image.',
          '**Keep everything:** don’t call `stop()`. The container stays running and the next `create_sandbox(name=...)` re-attaches with processes and packages intact.',
          'The volume lives on the docker host. On a cloud VM keep `/var/lib/docker` on a persistent disk, and resume on the same VM.',
        ],
      },
    ],
  },
  {
    slug: 'egress',
    group: 'Guides',
    title: 'Outbound traffic',
    blurb: 'Let a sandbox reach only the hosts you name.',
    readme: '#limiting-outbound-traffic',
    body: [
      {
        code: `sb = create_sandbox("python:3.12-slim", egress=["pypi.org", "*.pythonhosted.org", "10.0.0.0/24:8080"])
sb.exec("pip install requests")      # works: the host is on the list
sb.exec("curl https://example.com")  # 403 from the proxy: "example.com:443 is not in this sandbox's egress allowlist"`,
        file: 'egress.py',
      },
      { p: 'The sandbox gets a private docker network (`--internal`: no route out). The only other thing on it is a small allowlist proxy. `HTTP_PROXY` and `HTTPS_PROXY` point at it, so `pip`, `apt`, `curl` and Python’s `urllib` just work, and code that ignores the variables has no route anywhere.' },
      { h: 'Entries' },
      {
        ul: [
          '`host` (exact), `*.host` (subdomains, not `host` itself), an IPv4 address or an IPv4 network, with an optional `:port`.',
          'Ports default to 80 and 443; `host:*` is any port. There is no allow-everything entry: that is `network="bridge"`.',
          'A name only gets through if it resolves to public addresses, so a name can’t be used to reach `10.x`, the docker host or the cloud metadata endpoint. A private address needs an entry that says so.',
          'It is an HTTP(S) proxy: raw sockets (SYN scans, ICMP, UDP) don’t pass.',
        ],
      },
      { h: 'Good to know' },
      {
        ul: [
          '**The proxy runs under `runc`, not gVisor.** Docker’s embedded DNS doesn’t work under gVisor. It is the one agvisor component outside gVisor: about 150 lines of Python, run as `nobody`, read-only, all capabilities dropped, 256 MB.',
          '`docker logs agvisor_proxy_<name>` shows one `allow` or `deny` line per connection.',
          'An expired sandbox can’t remove its proxy and network. The next `egress=` start does, and so do `agvisor prune` and `prune_egress()`.',
          'Docker has about 30 default network address pools, so about 30 `egress=` sandboxes at once. Widen `default-address-pools` for more.',
          'Fixed at creation, like `limits`: re-attaching to a running named sandbox ignores `egress=`.',
        ],
      },
      { note: '`egress=` limits what a sandbox can connect to, not what it can say. Data can ride in a URL to an allowed host.', warn: true },
    ],
  },
  {
    slug: 'inject',
    group: 'Guides',
    title: 'Credentials',
    blurb: 'Give a sandbox a credential it can use but not read.',
    readme: '#credentials-the-sandbox-can-use-but-not-read',
    body: [
      {
        code: `sb = create_sandbox("python:3.12-slim", inject={"api.example.com": {"Authorization": "Bearer sk-..."}})
sb.exec("curl https://api.example.com/v1/me")   # the request carries the header; the sandbox never held the key`,
        file: 'inject.py',
      },
      { p: '`inject={host: {header: value}}` needs `pip install \'agvisor[inject]\'` and builds on `egress=`. The proxy ends the sandbox’s HTTPS to that host, sets the headers on every request (replacing one of the same name), and sends it on over TLS of its own, with the host’s real certificate verified.' },
      {
        ul: [
          'The sandbox trusts a certificate authority made for it on your machine. Its private key is dropped as soon as it has signed the one certificate the proxy needs, and the authority is valid for 30 days.',
          'The credential is not in the sandbox’s environment, files or command lines. It sits in the proxy container’s environment, so whoever can run `docker inspect` on the host can read it.',
          'Plain host names only: no wildcards, addresses or ports. `https://` on port 443 only; `http://` to such a host is refused, not sent in the clear.',
          'One request per connection, and no HTTP/2 or websockets to those hosts. Python, `requests`, `curl`, `git`, Go and Node trust the authority. Java, and programs that pin certificates, do not.',
          '`AGVISOR_INJECT` sets it for the MCP server, so the agent has the credential’s use and not its value.',
        ],
      },
      { note: 'A sandbox can use the credential for anything its host accepts, and read it back if the service echoes request headers. Inject the narrowest credential the task needs.', warn: true },
    ],
  },
  {
    slug: 'files',
    group: 'Guides',
    title: 'Files in and out',
    blurb: 'Move files and directories across the sandbox boundary.',
    readme: '#files-in-and-out',
    body: [
      {
        code: `sb.put("/tmp/job/input.csv", open("input.csv", "rb").read())   # parent dirs are created, an existing file is replaced
data = sb.get("/tmp/job/result.json")                          # bytes; a relative path starts in /workspace for a named sandbox`,
        file: 'files.py',
      },
      { p: 'Both go through `exec()` (base64 over stdin and stdout), so they need GNU coreutils in the image. They raise `SandboxGone` if the sandbox is dead and `RuntimeError` if the write or read fails. `get()` reads regular files only and raises `ValueError` past `max_bytes` (default 10 MB): the sandbox is untrusted, so what comes out of it is capped.' },
      { h: 'Directories' },
      {
        code: `sb.put_dir("./project", "/workspace/project")   # a whole directory in: created, same-named files replaced, others kept
sb.get_dir("/workspace/out", "./results")       # and out (max_bytes=50 MB of tar)`,
        file: 'directories.py',
      },
      {
        ul: [
          '`put_dir` copies links as links and never follows them, so a link on your side can’t pull a host file into the sandbox.',
          '`get_dir` extracts with tarfile’s `data` filter: nothing lands outside the destination, no device files, no setuid bits. Links that point outside the destination are left out, and `get_dir` returns their names.',
        ],
      },
      { code: '$ agvisor put NAME ./file-or-dir /remote/path\n$ agvisor get NAME /remote/path ./local', file: 'terminal' },
    ],
  },
  {
    slug: 'jobs',
    group: 'Guides',
    title: 'Background jobs',
    blurb: 'Run what has to outlive exec().',
    readme: '#background-jobs',
    body: [
      {
        code: `job = sb.spawn("python scan.py targets.txt", timeout=36000)   # returns at once; it outlives this exec() and your process
while (rc := job.poll()) is None:                              # or job.wait(timeout=...)
    chunk = job.read(offset); offset += len(chunk)             # bytes since \`offset\`, stdout and stderr merged
job.kill()                                                     # TERM to the command and what it started; kill(force=True) = KILL
Job(sb, job_id).wait()                                         # pick a job up again after your process restarted`,
        file: 'jobs.py',
      },
      { p: '`exec(timeout=)` has to stay under `idle_ttl`, so anything longer goes here. A job writes to `/var/tmp/.agvisor-jobs/<id>/` on the root filesystem, so `Limits.disk` caps its log.' },
      {
        ul: [
          'Exit codes: 124 = hit its `timeout`, 143 = killed (TERM), 137 = KILL.',
          'A running job does **not** keep an `idle_ttl` sandbox alive, but `poll()`, `read()` and `wait()` are execs, so a client that checks on it does. Set `lifetime` for a hard budget.',
        ],
      },
    ],
  },
  {
    slug: 'ports',
    group: 'Guides',
    title: 'Reaching a server inside',
    blurb: 'Publish a sandbox port on your machine’s loopback.',
    readme: '#reaching-a-server-inside',
    body: [
      {
        code: `with create_sandbox("python:3.12-slim", ports=[8000], lifetime=3600) as sb:
    sb.spawn("python3 -m http.server 8000")                  # a dev server, or anything that listens
    print(f"http://127.0.0.1:{sb.port(8000)}")               # the same server, from this machine`,
        file: 'ports.py',
      },
      { p: '`ports=` publishes each container port on a random free port of `127.0.0.1` on the docker host, never on its other addresses. `sb.port(8000)` says which. It is fixed when the sandbox is made, and a resumed sandbox keeps the ports of its first start.' },
      {
        ul: [
          'Needs a network the host can reach into: headless, `network="bridge"` or `"isolated"`. A sandbox on `"none"`, or behind `egress=` or `inject=`, has no way in.',
          'With `host="ssh://..."` the port is on that machine’s loopback: `ssh -L 8000:127.0.0.1:<port> user@vm`.',
          '`idle_ttl` counts `exec()` calls, not connections, so give a sandbox you are browsing a `lifetime`.',
        ],
      },
    ],
  },
  {
    slug: 'python-sessions',
    group: 'Guides',
    title: 'Python sessions',
    blurb: 'An interpreter that stays up between calls, like notebook cells.',
    readme: '#python-sessions',
    body: [
      {
        code: `sb.python("import csv; rows = list(csv.reader(open('/tmp/job/input.csv')))")   # an interpreter that stays up between calls
r = sb.python("len(rows)")                                                      # r.stdout: stdout and stderr merged, tail only
sb.python("rows[:3]", timeout=30, restart=False)                                # restart=True throws the variables away`,
        file: 'session.py',
      },
      { p: 'Variables, imports and functions survive between calls, and the value of a last expression is printed. A Python exception is a traceback in `r.stdout`, not a return code.' },
      {
        ul: [
          'At `timeout` (default 60 s) the code gets a `TimeoutError` and the session survives. Stuck where that can’t reach it (a C call), the session is killed and `returncode` is 124.',
          '127 means no `python3` in the image (`python:3.12-slim` has it). `max_bytes` (default 30 KB) cuts what comes back.',
          'The interpreter is a process in the container, so a reset, `lifetime` or `idle_ttl` starts a new one. Each call is one `exec()`, so `timeout + 5` must stay under `idle_ttl`.',
          'It is also an MCP tool, `python`.',
        ],
      },
    ],
  },
  {
    slug: 'async',
    group: 'Guides',
    title: 'Async',
    blurb: 'The same sandboxes for asyncio code.',
    readme: '#async',
    body: [
      { p: '`agvisor.aio` has the same sandboxes for `asyncio` code. Every blocking call runs in a worker thread, so the event loop stays free and calls can run side by side.' },
      {
        code: `import asyncio
from agvisor import aio

async def main():
    async with aio.AsyncSandbox("python:3.12-slim", lifetime=600) as sb:
        a, b = await asyncio.gather(sb.exec("sleep 2; echo a"), sb.exec("sleep 2; echo b"))   # ~2 s, not 4
        job = await sb.spawn("echo building; sleep 1; echo done")
        print(await job.wait(), (await job.read()).decode())

asyncio.run(main())`,
        file: 'async.py',
      },
      {
        ul: [
          '`AsyncSandbox(...)` takes `Sandbox`’s arguments. `await aio.create_sandbox(...)` starts it without `async with`, and `aio.list_sandboxes()` returns `AsyncSandbox` handles.',
          'Cancelling an await stops waiting, not the command: it runs on in the sandbox until its own `timeout=` ends it. Pass a `timeout`.',
          '`exec(on_output=)` can be a plain function (called in a worker thread) or an `async def`.',
        ],
      },
    ],
  },
  {
    slug: 'pool',
    group: 'Guides',
    title: 'Warm pool',
    blurb: 'Keep sandboxes started so acquire() doesn’t wait for docker run.',
    readme: '#warm-pool',
    body: [
      { p: '`Pool` keeps sandboxes started and unused, so `acquire()` costs one quick `exec` (a health check) instead of a `docker run`. Taking one starts a replacement in the background.' },
      {
        code: `from agvisor import Pool

with Pool(2, "python:3.12-slim", network="none", lifetime=600) as pool:   # two ready at all times
    with pool.acquire() as sb:                                            # at once; yours to stop()
        sb.exec("echo hi")`,
        file: 'pool.py',
      },
      {
        ul: [
          'Same arguments as `create_sandbox()`, shared by every sandbox of the pool. Anonymous and headless only: no `name=`, `owner=` or `gui=`.',
          '`lifetime=` or `idle_ttl=` is required: it ends spares that a crashed process leaves behind, counted from when a spare was made.',
          'A spare is a running sandbox and holds an idle sandbox’s memory (tens of MB under gVisor) until acquired or `close()`d. When none is ready, `acquire()` makes one on the spot.',
          'From `asyncio`: `sb = await asyncio.to_thread(pool.acquire)`.',
        ],
      },
    ],
  },
  {
    slug: 'browser',
    group: 'Guides',
    title: 'A browser inside',
    blurb: 'Open a page you didn’t write without it touching your machine.',
    readme: '#a-browser-inside',
    body: [
      { p: 'Chromium runs under gVisor. Use [Playwright’s image](https://mcr.microsoft.com/en-us/artifact/mar/playwright/python/about), which has the browsers, and install the matching Python package in the sandbox.' },
      {
        code: `with create_sandbox("mcr.microsoft.com/playwright/python:v1.65.2-noble", network="isolated",
                    limits=Limits(memory="2g", shm="1g")) as sb:
    sb.exec("pip install -q playwright==1.65.2", timeout=300)
    sb.put("/tmp/browse.py", script)         # sync_playwright(): chromium.launch(), page.goto(), page.screenshot(...)
    sb.exec("python3 /tmp/browse.py https://example.com")
    png = sb.get("/tmp/page.png")`,
        file: 'browser.py',
      },
      {
        ul: [
          'Set `Limits(shm="1g")`: Chromium needs more than docker’s 64 MB `/dev/shm`.',
          'It needs the internet for the page and for `pip`, so `network="bridge"` or `"isolated"`.',
          'To let the browser reach only some hosts, use `egress=` and hand the proxy to Chromium, which ignores `HTTPS_PROXY`: parse that variable in the script and pass `chromium.launch(proxy=...)`. See `examples/09_browser_egress.py`.',
          'Playwright’s version must match the browsers in the image. The image is about 2 GB.',
        ],
      },
    ],
  },
  {
    slug: 'expiry',
    group: 'Guides',
    title: 'Expiry, cleanup and the CLI',
    blurb: 'Let sandboxes expire, find the ones left behind, and clean up from a shell.',
    readme: '#expiry-and-cleanup',
    body: [
      {
        code: `from agvisor import list_sandboxes

sb = create_sandbox(name="u42", idle_ttl=900, lifetime=4 * 3600)   # gone after 15 idle minutes, or 4h at most
for sb in list_sandboxes(host="ssh://user@my-vm"):                 # every agvisor sandbox on that docker host
    print(sb.name, sb.created); sb.stop()                          # exec(), stop() and delete() work on these handles`,
        file: 'expiry.py',
      },
      {
        ul: [
          '`idle_ttl`: the clock restarts when each `exec()` starts and ends; background processes don’t count. `exec(timeout=)` must be shorter than `idle_ttl`, else `ValueError`.',
          'A named sandbox that expires keeps its files. `delete()` is what erases them: run it from your own retention job.',
          '`list_sandboxes()` also finds anonymous sandboxes left behind by a crashed process. `sb.created` is a timezone-aware `datetime`, so a retention job can stop what is older than it should be.',
          '`owner=` tags a sandbox, for example with a tenant id, and `list_sandboxes(owner="u42")` lists only theirs.',
        ],
      },
      { h: 'From a shell' },
      {
        code: `$ agvisor ls                          # name, state, owner and creation time
$ agvisor rm NAME...                  # --data also erases files and snapshots
$ agvisor rm --older-than 14400       # a retention job, in cron
$ agvisor exec NAME cmd...            # exits with the command's exit code
$ agvisor put NAME ./local /remote    # and: agvisor get NAME /remote ./local
$ agvisor prune                       # removes what expired egress= sandboxes left behind
$ agvisor run IMAGE -- COMMAND        # make a sandbox, run one command, remove it`,
        file: 'terminal',
      },
      { p: 'Add `--host ssh://user@vm` to any of them for a remote host, and `--owner` to filter. `rm` never removes everything at once: it needs names or `--older-than`.' },
    ],
  },
  {
    slug: 'agents',
    group: 'Integrations',
    title: 'Coding agents',
    blurb: 'A skill and an MCP server for Claude Code, VS Code and OpenCode.',
    readme: '#coding-agents-skill--mcp-server',
    body: [
      { p: 'Two optional pieces for Claude Code, VS Code (Copilot agent mode) and OpenCode:' },
      {
        ul: [
          '**Skill**, `.claude/skills/agvisor/SKILL.md`: tells the agent when to sandbox, plus the gotchas. Copy the `agvisor` folder to `.claude/skills/` (all three read it), or to `.github/skills/` or `.agents/skills/`.',
          '**MCP server**, `agvisor-mcp`: `run`, `write_file`, `read_file`, `edit_file`, `list_files`, `python`, `start_job`, `job_output`, `kill_job`, `stats` and `reset`, so the agent runs code in the sandbox, not on your host.',
        ],
      },
      { p: 'The server makes one sandbox on first use and keeps it until it has been idle for `AGVISOR_IDLE_TTL`. A skill only advises: to make the sandbox the *only* way the agent runs code, also turn off its host shell tool.' },
      { h: 'Add it' },
      { code: "$ claude mcp add agvisor -e AGVISOR_HOST=ssh://user@my-vm -- uvx --from 'agvisor[mcp]' agvisor-mcp", file: 'Claude Code' },
      { code: '{"servers": {"agvisor": {"type": "stdio", "command": "uvx", "args": ["--from", "agvisor[mcp]", "agvisor-mcp"],\n                         "env": {"AGVISOR_HOST": "ssh://user@my-vm"}}}}', file: '.vscode/mcp.json' },
      { code: '{"mcp": {"servers": {"agvisor": {"type": "local", "command": ["uvx", "--from", "agvisor[mcp]", "agvisor-mcp"],\n                                 "environment": {"AGVISOR_HOST": "ssh://user@my-vm"}}}}}', file: 'opencode.json' },
      { h: 'Settings' },
      { p: 'Settings are environment variables, so the agent can’t pick its own image, network or limits.' },
      {
        table: [
          ['variable', 'default'],
          ['`AGVISOR_HOST`', 'Local docker or `$DOCKER_HOST`; `ssh://user@vm` for a remote Linux host.'],
          ['`AGVISOR_NETWORK`', '`none` (no internet: `pip install` fails). `bridge` for internet.'],
          ['`AGVISOR_EGRESS`', 'Unset. Comma-separated hosts the sandbox may reach, instead of `AGVISOR_NETWORK`.'],
          ['`AGVISOR_INJECT`', 'Unset. Credentials the sandbox can use but not read, as JSON. Needs `agvisor[inject]`.'],
          ['`AGVISOR_PORTS`', 'Unset. Comma-separated container ports to reach from your machine; adds a `port` tool.'],
          ['`AGVISOR_IMAGE`', '`ubuntu:24.04`'],
          ['`AGVISOR_CPUS` / `AGVISOR_MEMORY`', '`2` / `2g`'],
          ['`AGVISOR_IDLE_TTL` / `AGVISOR_LIFETIME`', '`600` / `3600` seconds'],
          ['`AGVISOR_PUT_DIR`', 'Unset. `HOST_DIR:/DEST`: a directory of yours copied into the sandbox when it is made. Keep it small.'],
          ['`AGVISOR_NAME`', 'Unset. Set it and `/workspace` is kept in a docker volume, and the snapshot tools appear.'],
          ['`AGVISOR_SNAPSHOTS`', '`3`. With `AGVISOR_NAME`: how many snapshots may exist at once.'],
        ],
      },
      { p: 'By default the sandbox is anonymous: no `/workspace` volume, so nothing is shared between projects, and the sandbox can’t see your files.' },
    ],
  },
  {
    slug: 'gui',
    group: 'Integrations',
    title: 'GUI in a web page',
    blurb: 'Show a desktop sandbox in React, Next.js, Vue or plain HTML.',
    readme: '#gui-in-a-web-page-react-nextjs-vue-plain-html',
    body: [
      { p: 'Browsers can’t speak RDP, so a gateway sits in between: `browser <-wss-> gateway <-> guacd <-RDP-> sandbox`. Start both on the docker host with the compose file (`pip install \'agvisor[web]\'` for the token helper).' },
      {
        code: `$ export AGVISOR_KEY=$(openssl rand -hex 32)   # shared by the gateway and your backend; compose refuses to start without it
$ docker compose up -d --build                 # guacd + gateway, both on 127.0.0.1; gateway on :8090 (GATEWAY_PORT to change)`,
        file: 'terminal',
      },
      { p: 'Put the gateway behind your TLS reverse proxy (so the page uses `wss://`) and your auth. Linux only: both containers use the host network. In your backend, after the user is authorised:' },
      {
        code: `sb = create_sandbox(gui=True)
token = sb.desktop_token(bytes.fromhex(os.environ["AGVISOR_KEY"]), ttl=60)  # server-side only: it holds the RDP password
# return {"gateway": "wss://your.host/desktop", "token": token} to the page`,
        file: 'backend.py',
      },
      { p: 'In the page, give the element a size and pass both values. The component is the npm package `agvisor-desktop`.' },
      { code: '$ npm install agvisor-desktop', file: 'terminal' },
      {
        code: `import "agvisor-desktop";   // React / Next.js ('use client'): safe to import during SSR
<agvisor-desktop gateway={gateway} token={token} style={{ width: "100%", height: 600 }} />`,
        file: 'React',
      },
      { code: '<!-- Vue: compilerOptions.isCustomElement = (tag) => tag === "agvisor-desktop"; plain HTML: <script type="module"> -->\n<agvisor-desktop :gateway="gateway" :token="token" style="width:100%;height:600px"></agvisor-desktop>', file: 'Vue / HTML' },
      { p: 'A new `token` reconnects. The element fires an `error` event (`e.detail` is the message) when the connection fails or drops, for example a rejected or expired token: mint a fresh one and set it. A token only has to be valid when the session opens, but anyone holding it can open one until `ttl` runs out.' },
    ],
  },
  {
    slug: 'github-action',
    group: 'Integrations',
    title: 'GitHub Action',
    blurb: 'Run an agent, a test suite or any untrusted step in a gVisor sandbox.',
    readme: '#github-action',
    body: [
      { p: 'The action installs `runsc` on a GitHub-hosted `ubuntu-latest` runner, copies the job’s files into `/workspace`, runs your script there and fails the step with the script’s exit code.' },
      {
        code: `- uses: actions/checkout@v7
- uses: cr-sbarbouche/agvisor@v0.1.9
  env:
    ANTHROPIC_KEY: \${{ secrets.ANTHROPIC_API_KEY }}
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
  with: {name: agent-out, path: agvisor-out}`,
        file: '.github/workflows/agent.yml',
      },
      { p: 'Inputs: `image` and `run` (required), `workspace` (default `.`, `""` for none), `output`, `egress`, `inject`, `cpus` (2), `memory` (2g), `timeout` (600 s). With no `egress` the network is open, as with `create_sandbox()`. Under the hood it is `agvisor run IMAGE --egress ... --inject HOST:HEADER=ENVVAR --put ... --get ... -- COMMAND`, which also works on any machine with docker and `runsc`.' },
      {
        ul: [
          'The secret goes in the calling step’s `env:` and `inject:` names the variable, so it never appears on a command line and the sandbox never holds it.',
          'The `workspace` copy is a tar through `docker exec`: a repository with large binaries makes it slow. The image must be Debian or Ubuntu based.',
          'Headless only. A headless browser works.',
          '`ubuntu-latest` only: the runner needs `sudo` and docker.',
        ],
      },
      { h: 'On your own machine' },
      { p: 'The sandbox just has to run on a machine that has docker and `runsc` set up as in Host setup. Either register it as a **self-hosted runner** with a label and put its user in the `docker` group:' },
      {
        code: `jobs:
  agent:
    runs-on: [self-hosted, gvisor]
    steps:
      - uses: actions/checkout@v7
      - uses: cr-sbarbouche/agvisor@v0.1.9
        with: {image: "python:3.12-slim", run: "python agent.py"}`,
        file: '.github/workflows/agent.yml',
      },
      { p: 'Or drive the machine over ssh from a disposable runner with `host:`, so only the sandbox runs on your VM:' },
      { code: '- uses: cr-sbarbouche/agvisor@v0.1.9\n  with: {host: "ssh://ci@vm.example.com", image: "python:3.12-slim", run: "python agent.py"}', file: '.github/workflows/agent.yml' },
      { note: 'Whoever holds that ssh key can run docker on the VM, which is root there. Give it a VM of its own and a key used for nothing else, and keep ssh off the open internet. On a persistent self-hosted runner, don’t take pull requests from forks.', warn: true },
    ],
  },
  {
    slug: 'security',
    group: 'Reference',
    title: 'Security',
    blurb: 'Known gaps. Read this before running code you don’t trust.',
    readme: '#security',
    body: [
      { note: 'This is alpha software and nobody has audited it. gVisor shrinks the part of the kernel that sandboxed code can reach; it is not a guarantee, and this library’s own code has not been reviewed for flaws. Don’t use it as the boundary for hostile multi-tenant workloads without a review of your own, and keep `runsc` up to date.', warn: true },
      { h: 'Known gaps' },
      {
        ul: [
          '**Whoever can run `docker` on the host controls every sandbox**, and is effectively root on that host. agvisor adds no authentication or authorization: put your own in front of it.',
          '**`name=` has no ownership check unless you pass `owner=`.** If a tenant can choose the name, they can attach to, run commands in, stop and `delete()` another tenant’s sandbox. `owner=` is a guard against mixed-up names in your own orchestrator, not authentication. Build names and owners from your own user ids, never from tenant input.',
          '**`/workspace` has no size cap unless you set one.** `Limits(disk=)` only caps the root filesystem. Use `Limits(workspace="10g")` on a host prepared for it.',
          '**Sandboxes on one docker network can reach each other.** Use `network="isolated"`, `network="none"`, or a separate network per tenant or run.',
          '**`network="bridge"` also reaches the outside:** the internet, services on the host itself, and on a cloud VM the metadata endpoint that serves instance credentials.',
          '**`egress=` limits what a sandbox can connect to, not what it can say.** The proxy container runs under `runc`, outside gVisor.',
          '**`inject=` puts a TLS-ending proxy in the path of the sandbox’s requests.** Whoever takes over the proxy can impersonate the injected hosts to that one sandbox.',
          '**`gui=True` runs `--privileged`.** Under gVisor that only grants simulated capabilities; agvisor refuses `runc` for GUI sandboxes.',
        ],
      },
      { p: 'To report a vulnerability, follow `SECURITY.md` in the repository instead of opening a public issue.' },
    ],
  },
];

export const groups = [...new Set(pages.map((p) => p.group))];
