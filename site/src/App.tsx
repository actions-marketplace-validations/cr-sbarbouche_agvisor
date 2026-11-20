import { Fragment, useEffect, useState, type ReactNode } from 'react';
import { Link, Route, Router, Switch, useLocation } from 'wouter';
import { useHashLocation } from 'wouter/use-hash-location';
import { ArrowRight, ArrowUpRight, Check, ChevronDown, Code2, Copy, Github, Linkedin, SquareTerminal, Terminal } from 'lucide-react';
import logo from './assets/logo.jpeg';
import introVideo from './assets/intro.mp4';
import videoPoster from './assets/video-poster.jpg';
import { groups, pages, repository, type Block } from './docs';
import { CiMerge, FigureDefs, HeroScene, Lifecycle, NetworkIsolation, OrbitMap, SnapshotSplit, WorkspaceFlow } from './figures';

const linkedinPost = 'https://www.linkedin.com/feed/update/urn:li:activity:7524033443815641088/';
const author = 'https://github.com/cr-sbarbouche';
const homeTitle = document.title;

// `code`, **bold**, *italic* and [text](url) inside a string.
function inline(s: string): ReactNode[] {
  return s.split(/(`[^`]+`|\*\*[^*]+\*\*|\*[^*]+\*|\[[^\]]+\]\([^)]+\))/).map((t, i) => {
    if (t.startsWith('`')) return <code key={i}>{t.slice(1, -1)}</code>;
    if (t.startsWith('**')) return <strong key={i}>{inline(t.slice(2, -2))}</strong>;
    if (t.startsWith('*')) return <em key={i}>{t.slice(1, -1)}</em>;
    const link = t.match(/^\[([^\]]+)\]\(([^)]+)\)$/);
    if (link) return <a key={i} href={link[2]} target="_blank" rel="noreferrer">{link[1]}</a>;
    return t;
  });
}

// ponytail: no syntax highlighter. "$ " lines are commands, "#" and "//" lines are notes. Add shiki if that stops being enough.
function Code({ code, file, tabs }: { code: string; file?: string; tabs?: ReactNode }) {
  const [copied, setCopied] = useState(false);
  let cont = false;
  const lines = code.split('\n').map((text) => {
    const prompt = text.startsWith('$ ');
    const cmd = prompt || cont;
    cont = cmd && text.trimEnd().endsWith('\\');
    return { text, prompt, cmd, note: !cmd && /^\s*(#|\/\/|<!--)/.test(text) };
  });
  const copy = () =>
    navigator.clipboard?.writeText(code.replace(/^\$ /gm, '')).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    });
  return (
    <div className="code">
      <div className="code-top">
        {tabs ?? (file && <span>{file}</span>)}
        <button type="button" className="copy" onClick={copy} aria-label="Copy code">
          {copied ? <Check size={13} aria-hidden="true" /> : <Copy size={13} aria-hidden="true" />}
          {copied ? 'Copied' : 'Copy'}
        </button>
      </div>
      <pre>
        {lines.map((l, i) => (
          <span key={i}>
            {l.prompt && <span className="c-prompt">$ </span>}
            <span className={l.cmd ? 'c-cmd' : l.note ? 'c-note' : undefined}>{l.prompt ? l.text.slice(2) : l.text}</span>
          </span>
        ))}
      </pre>
    </div>
  );
}

type Tab = { id: string; label: string; code: string };

function CodeTabs({ tabs, name }: { tabs: Tab[]; name: string }) {
  const [active, setActive] = useState(0);
  return (
    <Code
      code={tabs[active].code}
      tabs={
        <div className="tabs" role="tablist" aria-label={name}>
          {tabs.map((t, i) => (
            <button
              key={t.id}
              type="button"
              role="tab"
              aria-selected={i === active}
              className={i === active ? 'is-active' : undefined}
              onClick={() => setActive(i)}
              data-testid={`tab-${name}-${t.id}`}
            >
              {t.label}
            </button>
          ))}
        </div>
      }
    />
  );
}

const start: Tab[] = [
  {
    id: 'shell',
    label: 'Shell',
    code: `$ pip install agvisor
# needs docker and runsc on the machine, or --host ssh://user@vm
$ agvisor run python:3.12-slim -- python3 -c 'print(1+1)'`,
  },
  {
    id: 'python',
    label: 'Python',
    code: `from agvisor import Limits
from agvisor.sandbox import create_sandbox

SMALL = Limits(cpus=1, memory="512m", pids=128)

with create_sandbox(limits=SMALL, lifetime=3600) as sb:
    r = sb.exec("python3 -c 'print(1+1)'", timeout=30)
    print(r.returncode, r.stdout)
# leaving the block deletes the sandbox`,
  },
];

const agentSetups: Tab[] = [
  {
    id: 'claude',
    label: 'Claude Code',
    code: `$ claude mcp add agvisor -e AGVISOR_HOST=ssh://user@my-vm \\
    -- uvx --from 'agvisor[mcp]' agvisor-mcp
# the skill: copy the folder from the repo
$ cp -r .claude/skills/agvisor ~/.claude/skills/`,
  },
  {
    id: 'vscode',
    label: 'VS Code',
    code: `// .vscode/mcp.json
{"servers": {"agvisor": {
  "type": "stdio", "command": "uvx",
  "args": ["--from", "agvisor[mcp]", "agvisor-mcp"],
  "env": {"AGVISOR_HOST": "ssh://user@my-vm"}}}}
// the skill: .github/skills/agvisor/ or .claude/skills/agvisor/`,
  },
  {
    id: 'opencode',
    label: 'OpenCode',
    code: `// opencode.json
{"mcp": {"servers": {"agvisor": {
  "type": "local",
  "command": ["uvx", "--from", "agvisor[mcp]", "agvisor-mcp"],
  "environment": {"AGVISOR_HOST": "ssh://user@my-vm"}}}}}
// the skill: .opencode/skills/agvisor/ or .claude/skills/agvisor/`,
  },
];

const taskCode = `from agvisor import Limits
from agvisor.sandbox import create_sandbox

with create_sandbox(name="task-142", lifetime=7200,
                    limits=Limits(cpus=2, memory="2g"),
                    egress=["pypi.org"]) as sb:
    sb.exec("make -C api test", timeout=600)

# the container is gone; /workspace stays until you delete it
create_sandbox(name="task-142").delete()`;

const more = [
  ['Files in and out', 'files', '`put`, `get`, `put_dir` and `get_dir` move files and whole directories. What comes out of an untrusted sandbox is size-capped and extracted safely.'],
  ['Background jobs', 'jobs', '`spawn()` returns at once and the job outlives `exec()` and your process. Poll it, read its output, kill it, or pick it up again after a restart.'],
  ['Reach a server inside', 'ports', '`ports=` publishes a container port on your machine’s loopback, so you can open the dev server an agent started.'],
  ['Python sessions', 'python-sessions', 'An interpreter that stays up between calls, like notebook cells: variables and imports survive. Also an MCP tool.'],
  ['Async and a warm pool', 'async', '`agvisor.aio` for asyncio code, and `Pool` to keep sandboxes started so `acquire()` doesn’t wait for `docker run`.'],
  ['A browser inside', 'browser', 'Chromium runs under gVisor, so a page you didn’t write can be opened, read and screenshotted without touching your machine.'],
  ['Expiry and cleanup', 'expiry', '`lifetime` and `idle_ttl` delete a sandbox even if your process dies. `agvisor ls`, `rm` and `prune` clean up from a shell.'],
  ['Desktop over RDP', 'gui', 'A GNOME desktop for workflows that need a graphical session, and a web component to show it in a page.'],
  ['Credentials it can’t read', 'inject', 'The proxy adds a header to requests for a host, so the code can call the API and never holds the key.'],
] as const;

function Header() {
  const [loc] = useLocation();
  const [scrolled, setScrolled] = useState(false);
  useEffect(() => {
    const f = () => setScrolled(scrollY > 40);
    f();
    addEventListener('scroll', f, { passive: true });
    return () => removeEventListener('scroll', f);
  }, []);
  const inDocs = loc.startsWith('/docs');
  return (
    <header className={scrolled || inDocs ? 'site-header solid' : 'site-header'}>
      <div className="bar">
        <Link className="brand" href="/" aria-label="Agvisor home" data-testid="link-home">
          <img src={logo} alt="" />
          <span>agvisor</span>
        </Link>
        <nav className="nav" aria-label="Main">
          <div className="menu">
            <button type="button" aria-haspopup="true">Product <ChevronDown size={15} aria-hidden="true" /></button>
            <div className="pop">
              <Link href="/features" data-testid="link-features">Features</Link>
              <Link href="/library">Library</Link>
              <Link href="/agents" data-testid="link-agents">For agents</Link>
              <Link href="/action">GitHub Action</Link>
              <Link href="/security">Security</Link>
            </div>
          </div>
          <Link href="/docs" className={inDocs ? 'is-active' : undefined} data-testid="link-docs">Docs</Link>
          <div className="menu">
            <button type="button" aria-haspopup="true">Resources <ChevronDown size={15} aria-hidden="true" /></button>
            <div className="pop">
              <a href={repository} target="_blank" rel="noreferrer" data-testid="link-github-nav">GitHub</a>
              <a href="https://pypi.org/project/agvisor/" target="_blank" rel="noreferrer">PyPI</a>
              <a href="https://www.npmjs.com/package/agvisor-desktop" target="_blank" rel="noreferrer" data-testid="link-desktop-package">npm: agvisor-desktop</a>
              <a href={linkedinPost} target="_blank" rel="noreferrer" data-testid="link-linkedin">LinkedIn</a>
            </div>
          </div>
        </nav>
        <div className="bar-actions">
          <Link className="btn btn-gold btn-sm" href="/docs" data-testid="link-docs-nav">Read the docs <ArrowRight size={15} aria-hidden="true" /></Link>
        </div>
      </div>
    </header>
  );
}

function Footer() {
  return (
    <footer className="footer">
      <div className="wrap">
        <div className="footer-grid">
          <div>
            <Link className="brand" href="/"><img src={logo} alt="" /><span>agvisor</span></Link>
            <p>gVisor sandboxes for coding agents. Alpha, and not security-audited.</p>
            <div className="footer-social">
              <a href={repository} target="_blank" rel="noreferrer" aria-label="GitHub" data-testid="link-footer-repository"><Github size={18} aria-hidden="true" /></a>
              <a href={linkedinPost} target="_blank" rel="noreferrer" aria-label="LinkedIn"><Linkedin size={18} aria-hidden="true" /></a>
            </div>
            <a href="https://pepy.tech/projects/agvisor" target="_blank" rel="noreferrer" aria-label="Total PyPI downloads" data-testid="link-pypi-downloads" style={{ display: 'inline-block', marginTop: 20 }}>
              <img
                src="https://static.pepy.tech/personalized-badge/agvisor?period=total&units=INTERNATIONAL_SYSTEM&left_color=BLACK&right_color=GREEN&left_text=downloads"
                alt="Agvisor total downloads on PyPI"
                width={100}
                height={20}
              />
            </a>
          </div>
          <div>
            <h4>Product</h4>
            <ul>
              <li><Link href="/features">Features</Link></li>
              <li><Link href="/library">Library</Link></li>
              <li><Link href="/agents">For agents</Link></li>
              <li><Link href="/action">GitHub Action</Link></li>
              <li><Link href="/security">Security</Link></li>
            </ul>
          </div>
          <div>
            <h4>Docs</h4>
            <ul>
              <li><Link href="/docs/overview">Overview</Link></li>
              <li><Link href="/docs/host-setup">Host setup</Link></li>
              <li><Link href="/docs/quickstart">Quickstart</Link></li>
              <li><Link href="/docs/agents">Coding agents</Link></li>
              <li><Link href="/docs/security">Security notes</Link></li>
            </ul>
          </div>
          <div>
            <h4>Resources</h4>
            <ul>
              <li><a href={repository} target="_blank" rel="noreferrer">GitHub</a></li>
              <li><a href={`${repository}#readme`} target="_blank" rel="noreferrer" data-testid="link-footer-docs">README</a></li>
              <li><a href="https://pypi.org/project/agvisor/" target="_blank" rel="noreferrer">PyPI</a></li>
              <li><a href="https://www.npmjs.com/package/agvisor-desktop" target="_blank" rel="noreferrer">npm</a></li>
            </ul>
          </div>
        </div>
        <div className="footer-bottom">
          <nav aria-label="Legal">
            <a href={`${repository}/blob/main/LICENSE`} target="_blank" rel="noreferrer">License</a>
            <a href={`${repository}/blob/main/SECURITY.md`} target="_blank" rel="noreferrer">Security policy</a>
            <span>© {new Date().getFullYear()} Agvisor</span>
          </nav>
          <span className="sig">
            <span>Designed &amp; built by</span>
            <a href={author} target="_blank" rel="noreferrer" data-testid="link-signature">cr-sbarbouche</a>
          </span>
        </div>
      </div>
    </footer>
  );
}

const arrow = <ArrowRight size={17} aria-hidden="true" />;

function Home({ section }: { section?: string }) {
  useEffect(() => {
    document.title = homeTitle;
    if (section) document.getElementById(section)?.scrollIntoView();
    else window.scrollTo(0, 0);
  }, [section]);

  return (
    <>
      <section className="hero" aria-labelledby="hero-title">
        <HeroScene />
        <div className="hero-inner">
          <div>
            <div className="reveal"><span className="pill"><i /> Python · Docker · gVisor</span></div>
            <h1 id="hero-title" className="reveal d1">Run agent code in<br /><em>bounded sandboxes.</em></h1>
            <p className="reveal d2">
              Agvisor is a small Python library for creating and managing Docker-backed sandboxes, using gVisor’s runsc runtime. It also ships an MCP server and an agent skill for Claude Code, VS Code and OpenCode.
            </p>
            <div className="row reveal d2">
              <Link className="btn btn-gold" href="/docs" data-testid="link-install-hero">Read the docs {arrow}</Link>
              <a className="btn btn-glass" href={repository} target="_blank" rel="noreferrer" data-testid="link-github-hero"><Github size={17} aria-hidden="true" /> View on GitHub</a>
            </div>
          </div>
          <div className="reveal d3" id="quickstart"><CodeTabs tabs={start} name="start" /></div>
        </div>
      </section>

      <section className="fade" id="features" aria-labelledby="features-title">
        <div className="wrap">
          <div className="feat sec">
            <div>
              <span className="kicker">Images</span>
              <h2 className="d" id="features-title">Bring any image</h2>
              <p className="lead">Pick a Docker image and create a sandbox from Python or a shell. Set CPU, memory and process limits, an outbound allowlist and published ports as you create it. The code runs under gVisor’s <code>runsc</code>, not directly on your host kernel.</p>
              <p className="lead">Debian and Ubuntu based images work best. <Link className="link" href="/docs/quickstart" data-testid="link-readme">Read the quickstart</Link>.</p>
            </div>
            <div className="fig fig-tall"><OrbitMap /></div>
          </div>
          <hr className="rule" />
          <div className="feat sub">
            <div>
              <span className="kicker">Persistence</span>
              <h3 className="d">A workspace that outlives the container</h3>
              <p className="lead">Name a sandbox and its <code>/workspace</code> survives. Resume it days later: processes are gone, but files, virtual environments and checkouts are not.</p>
              <p className="lead">Everything under <code>/workspace</code> is kept in a docker volume on the host. <Link className="link" href="/docs/resume">Resuming and snapshots</Link>.</p>
            </div>
            <div className="fig"><WorkspaceFlow /></div>
          </div>
        </div>
      </section>

      <section id="action" aria-labelledby="action-title" style={{ borderTop: '1px solid var(--line)' }}>
        <div className="wrap sec pair">
          <div>
            <span className="kicker">Pipelines</span>
            <h2 className="d" id="action-title">Take it into your pipeline</h2>
            <p className="lead">The GitHub Action installs <code>runsc</code> on a hosted runner, copies the job’s files into <code>/workspace</code> and runs your script there. <code>agvisor run</code> does the same on any machine with docker and <code>runsc</code>.</p>
            <p className="lead"><Link className="link" href="/docs/github-action">GitHub Action docs</Link></p>
            <div className="fig"><CiMerge /></div>
          </div>
          <div>
            <span className="kicker">Snapshots</span>
            <h2 className="d">Checkpoint before you risk it</h2>
            <p className="lead">Save <code>/workspace</code> before a risky install and go back to it. A snapshot is a copy in a second volume; restoring replaces the data and starts a fresh container.</p>
            <p className="lead"><Link className="link" href="/docs/resume">How snapshots work</Link></p>
            <div className="fig"><SnapshotSplit /></div>
          </div>
        </div>
      </section>

      <section id="library" aria-labelledby="library-title" style={{ borderTop: '1px solid var(--line)' }}>
        <div className="wrap sec">
          <span className="kicker">Around the run</span>
          <h2 className="d" id="library-title">Made to fit around your agent</h2>
          <p className="lead">Use the library as one component in your own coding-agent loop. Agvisor handles sandbox operations; your application decides what to run and how to review the result.</p>
          <div className="items">
            {more.map(([title, slug, text]) => (
              <div className="item" key={slug}>
                <h3>{title}</h3>
                <p>{inline(text)}</p>
                <Link href={`/docs/${slug}`}>Read more</Link>
              </div>
            ))}
          </div>
        </div>
      </section>

      <section style={{ borderTop: '1px solid var(--line)' }} aria-labelledby="tasks-title">
        <div className="wrap">
          <div className="feat sec">
            <div>
              <span className="kicker">Agent tasks</span>
              <h2 className="d" id="tasks-title">One sandbox for every task</h2>
              <p className="lead">Give each task its own sandbox, with its own limits, lifetime and network rules. Agents can install packages, change code and run tests inside it.</p>
            </div>
            <div className="fig" style={{ maxWidth: 640 }}>
              <div className="code">
                <div className="code-top"><span>one sandbox per task</span></div>
                <pre>
                  {taskCode.split('\n').map((l, i) => (
                    <span key={i}><span className={l.startsWith('#') ? 'c-note' : 'c-cmd'}>{l}</span></span>
                  ))}
                </pre>
              </div>
            </div>
          </div>
          <hr className="rule" />
          <div className="cols sub" style={{ ['--n' as string]: 2 }}>
            <div>
              <h3>Contained by gVisor</h3>
              <p>Each sandbox runs under gVisor, which shrinks the part of the host kernel the code can reach. It is one layer, not a guarantee.</p>
            </div>
            <div>
              <h3>Warm for the next run</h3>
              <p>Named sandboxes keep <code>/workspace</code>, so installs, caches and checkouts are there for the next run.</p>
            </div>
          </div>
        </div>
      </section>

      <section className="dark" id="agents" aria-labelledby="agents-title">
        <div className="wrap">
          <div className="feat sec">
            <div>
              <span className="kicker">For coding agents</span>
              <h2 className="d" id="agents-title" style={{ maxWidth: '16ch' }}>Give your agent a sandbox, not your shell</h2>
              <p className="lead">A skill tells the agent when to sandbox and what trips people up. An MCP server gives it the tools, with the host, network and limits set by you. Turn off the agent’s own shell tool and it has nowhere else to run code.</p>
              <p className="lead"><Link className="link" href="/docs/agents">Agent setup in the docs</Link></p>
            </div>
            <div className="fig" style={{ maxWidth: 640 }}><CodeTabs tabs={agentSetups} name="setup" /></div>
          </div>
          <hr className="rule" />
          <div className="cols sub" style={{ ['--n' as string]: 3 }}>
            <div>
              <h3>Guardrails it can’t change</h3>
              <p>Image, network and limits come from environment variables you set, so the agent can’t ask for more. Network is off by default.</p>
            </div>
            <div>
              <h3>A skill for the when and how</h3>
              <p>One <code>SKILL.md</code> covers the gotchas: which images work, why <code>pids</code> stays at 512, what <code>SandboxGone</code> means.</p>
            </div>
            <div>
              <h3>Disposable by default</h3>
              <p>The sandbox is anonymous and deletes itself after 10 idle minutes or an hour. It can’t see your files, and nothing is shared between projects.</p>
            </div>
          </div>
          <hr className="rule" />
          <div className="works" style={{ paddingBottom: 96 }}>
            Pairs with
            <span><Terminal size={18} aria-hidden="true" /> Claude Code</span>
            <span><Code2 size={18} aria-hidden="true" /> VS Code</span>
            <span><SquareTerminal size={18} aria-hidden="true" /> OpenCode</span>
            <span><Github size={18} aria-hidden="true" /> GitHub Actions</span>
          </div>
        </div>
      </section>

      <section aria-labelledby="limits-title">
        <div className="wrap">
          <div className="sec" style={{ paddingBottom: 72 }}>
            <span className="kicker">Limits</span>
            <h2 className="d" id="limits-title">Every sandbox gets a budget</h2>
            <p className="lead">Each sandbox has its own CPU, memory, process and disk limits, a lifetime and an idle expiry. You set them where you create it.</p>
          </div>
          <hr className="rule" />
          <div className="feat sub">
            <div>
              <h3 className="d">Deleted on a timer</h3>
              <p className="lead">Delete a sandbox when the work is done, or let <code>lifetime</code> and <code>idle_ttl</code> end it, even if your process dies. <code>agvisor ls</code>, <code>rm</code> and <code>prune</code> clean up from a shell.</p>
              <p className="lead"><Link className="link" href="/docs/limits">How limits behave</Link></p>
            </div>
            <div className="fig"><Lifecycle /></div>
          </div>
          <hr className="rule" />
          <div className="feat sub" style={{ paddingBottom: 120 }}>
            <div>
              <h3 className="d">Its own network, its own rules</h3>
              <p className="lead">With <code>egress=</code>, each sandbox gets its own internal network and a small allowlist proxy. It reaches only the hosts you name, and never another sandbox.</p>
              <p className="lead"><Link className="link" href="/docs/egress">Outbound traffic</Link></p>
            </div>
            <div className="fig"><NetworkIsolation /></div>
          </div>
        </div>
      </section>

      <section style={{ borderTop: '1px solid var(--line)' }} aria-labelledby="video-title">
        <div className="wrap sec">
          <span className="kicker">A short introduction</span>
          <h2 className="d" id="video-title">Why agent code needs a sandbox</h2>
          <p className="lead">A quick overview of the problem Agvisor is built for and where a resource-bounded sandbox fits in a coding-agent workflow.</p>
          <div className="video">
            <video controls playsInline preload="metadata" poster={videoPoster} aria-label="Agvisor introduction video from LinkedIn" data-testid="video-agvisor-intro">
              <source src={introVideo} type="video/mp4" />
              Your browser does not support the video element.
            </video>
          </div>
          <div className="caption">
            <span>Agvisor introduction · 27 seconds</span>
            <a href={linkedinPost} target="_blank" rel="noreferrer" data-testid="link-video-linkedin-post">
              View the LinkedIn post <ArrowUpRight size={14} aria-hidden="true" />
            </a>
          </div>
          <aside className="notice" id="security" aria-label="Security status">
            <b aria-hidden="true">!</b>
            <div>
              <h3>Alpha software. Review your threat model.</h3>
              <p>Agvisor has not been security-audited. gVisor is one layer in a broader isolation setup: it does not make arbitrary code fully secure, and it cannot replace application-level controls or careful host configuration. Consider cloud metadata access and network exposure in your environment, and treat sandboxing as defense in depth.</p>
              <p style={{ marginTop: 14 }}><Link className="link" href="/docs/security">Read the security notes</Link></p>
            </div>
          </aside>
        </div>
      </section>

      <section className="final" aria-labelledby="cta-title">
        <div className="wrap">
          <h2 id="cta-title">Run the next task in a sandbox</h2>
          <div className="row">
            <Link className="btn btn-gold" href="/docs">Read the docs {arrow}</Link>
            <a className="btn btn-glass" href={repository} target="_blank" rel="noreferrer" data-testid="link-github-footer"><Github size={17} aria-hidden="true" /> View on GitHub</a>
          </div>
        </div>
      </section>
    </>
  );
}

function Doc({ block }: { block: Block }) {
  if ('p' in block) return <p>{inline(block.p)}</p>;
  if ('h' in block) return <h2>{inline(block.h)}</h2>;
  if ('code' in block) return <Code code={block.code} file={block.file} />;
  if ('ul' in block) return <ul>{block.ul.map((li, i) => <li key={i}>{inline(li)}</li>)}</ul>;
  if ('table' in block) {
    const [head, ...rows] = block.table;
    return (
      <div className="table-wrap">
        <table>
          <thead><tr>{head.map((c, i) => <th key={i}>{c}</th>)}</tr></thead>
          <tbody>{rows.map((r, i) => <tr key={i}>{r.map((c, j) => <td key={j}>{inline(c)}</td>)}</tr>)}</tbody>
        </table>
      </div>
    );
  }
  return <div className={block.warn ? 'note warn' : 'note'} role="note">{inline(block.note)}</div>;
}

function Docs({ slug }: { slug?: string }) {
  const i = Math.max(0, pages.findIndex((p) => p.slug === slug)); // unknown slug -> overview
  const page = pages[i];
  const prev = pages[i - 1];
  const next = pages[i + 1];
  useEffect(() => {
    document.title = `${page.title} · Agvisor docs`;
    window.scrollTo(0, 0);
  }, [page.slug, page.title]);

  return (
    <div className="wrap docs">
      <nav className="docs-nav" aria-label="Documentation">
        <div>
          {groups.map((g) => (
            <Fragment key={g}>
              <h4>{g}</h4>
              {pages.filter((p) => p.group === g).map((p) => (
                <Link key={p.slug} href={`/docs/${p.slug}`} className={p.slug === page.slug ? 'is-active' : undefined} aria-current={p.slug === page.slug ? 'page' : undefined}>
                  {p.title}
                </Link>
              ))}
            </Fragment>
          ))}
        </div>
      </nav>
      <article className="doc">
        <div className="crumb"><span>Docs</span><span aria-hidden="true">/</span><span>{page.group}</span></div>
        <h1>{page.title}</h1>
        <p className="blurb">{page.blurb}</p>
        <div className="doc-body">
          {page.body.map((b, k) => <Doc key={k} block={b} />)}
        </div>
        <div className="doc-foot">
          <div className="readme-link">
            Every detail and edge case is in <a href={`${repository}${page.readme}`} target="_blank" rel="noreferrer">the README</a>.
          </div>
          <div className="pager">
            {prev && <Link href={`/docs/${prev.slug}`}><small>Previous</small>{prev.title}</Link>}
            {next && <Link className="next" href={`/docs/${next.slug}`}><small>Next</small>{next.title}</Link>}
          </div>
        </div>
      </article>
    </div>
  );
}

// ponytail: hash routing, so any static host serves it with no rewrite rules. Switch to path routing if the host does SPA fallback.
function Shell() {
  const [loc] = useLocation();
  return (
    <>
      <a className="sr-only focus:not-sr-only focus:fixed focus:left-3 focus:top-3 focus:z-50 focus:rounded-lg focus:bg-white focus:px-3 focus:py-2 focus:text-sm" href="#main" onClick={(e) => { e.preventDefault(); document.getElementById('main')?.focus(); }}>Skip to content</a>
      <FigureDefs />
      <Header />
      <main className={loc.startsWith('/docs') ? 'site-main offset' : 'site-main'} id="main" tabIndex={-1}>
        <Switch>
          <Route path="/docs/:slug?">{(p) => <Docs slug={p.slug} />}</Route>
          <Route path="/:section?">{(p) => <Home section={p.section} />}</Route>
        </Switch>
      </main>
      <Footer />
    </>
  );
}

function App() {
  return <Router hook={useHashLocation}><Shell /></Router>;
}

export default App;
