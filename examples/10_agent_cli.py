"""A coding agent's own CLI inside the sandbox, with its API key out of reach. Claude Code runs under gVisor and can reach npm and
api.anthropic.com and nothing else; the proxy adds the real key to its requests, so neither the agent nor any code it runs ever holds it.

    ANTHROPIC_API_KEY=sk-ant-... uv run --extra inject python examples/10_agent_cli.py
    AGVISOR_HOST=ssh://user@my-vm ANTHROPIC_API_KEY=... uv run --extra inject python examples/10_agent_cli.py   # docker on another machine

Without ANTHROPIC_API_KEY it installs the CLI and shows the same setup with a placeholder key: the API refuses that, which still shows a
request got through. Needs the node:22 and python:3.12-slim images on the docker host (the second runs the egress proxy; the first has to bring
a CA bundle, which inject= needs: node:22-slim has none), and
`pip install 'agvisor[inject]'` (this script runs under `uv run --extra inject`).
"""
import os

from agvisor import Limits, create_sandbox

HOST = os.environ.get("AGVISOR_HOST")  # None: docker on this machine
KEY = os.environ.get("ANTHROPIC_API_KEY")
PLACEHOLDER = "sk-ant-placeholder-the-proxy-sets-the-real-one"

with create_sandbox(
    "node:22", host=HOST, limits=Limits(memory="3g"), lifetime=1800,
    # the only hosts it can reach: npm for the install, and the API, which gets the key added on the way
    egress=["registry.npmjs.org"],
    inject={"api.anthropic.com": {"x-api-key": KEY or "placeholder-not-a-real-key"}},
    # the CLI wants a key to start: it gets a placeholder, and the proxy replaces it on every request to the API
    env={"ANTHROPIC_API_KEY": PLACEHOLDER, "DISABLE_TELEMETRY": "1", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"},
) as sb:
    r = sb.exec("npm install -g --no-fund --no-audit @anthropic-ai/claude-code 2>&1 | tail -3", timeout=600)
    print("npm install:", r.stdout.strip() or r.stderr.strip())
    r = sb.exec("claude --version", timeout=60)
    assert r.returncode == 0, (r.stdout, r.stderr)
    print("claude --version:", r.stdout.strip())

    # what the agent, or code it runs, can see: the placeholder, never the key
    seen = sb.exec("env; cat /proc/1/environ | tr '\\0' '\\n'; ls /usr/local/share/agvisor").stdout
    assert PLACEHOLDER in seen and (not KEY or KEY not in seen)
    print("the key is not in the sandbox's environment" if KEY else "(placeholder key: nothing to hide)")

    prompt = "Reply with the single word: pong"
    r = sb.exec(f"claude -p '{prompt}' --max-turns 1 2>&1", timeout=180)
    print("claude -p:", r.stdout.strip()[-300:] or r.stderr.strip()[-300:])
    if not KEY:
        print("(that is the API refusing the placeholder: set ANTHROPIC_API_KEY to get an answer)")

    # and nothing else is reachable: a page the agent shouldn't touch is refused by the proxy
    r = sb.exec("node -e \"require('https').get('https://example.com', {agent: false}, r => console.log(r.statusCode)).on('error', e => console.log('no route:', e.code))\"", timeout=60)
    print("direct connection to example.com:", r.stdout.strip())
