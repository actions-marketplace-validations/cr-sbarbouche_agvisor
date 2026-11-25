"""Needs a docker host with real gVisor (the `gvisor` job in ci.yml has it): Sandbox.python(context=) against a real sandbox, and the MCP tool.

    AGVISOR_HOST=ssh://user@vm AGVISOR_IMAGE=python:3.12-slim uv run python tests/test_gvisor_pycontexts.py

AGVISOR_HOST unset -> docker on this machine. AGVISOR_IMAGE: python 3.12+ on a Debian base, already on that host.
"""
import os

from agvisor import create_sandbox

host, image = os.environ.get("AGVISOR_HOST"), os.environ.get("AGVISOR_IMAGE")


def servers(sb):  # the interpreters that are up: each has a pid file in its own directory
    r = sb.exec("for f in /var/tmp/.agvisor-py*/pid; do [ -f $f ] && kill -0 $(cat $f) 2>/dev/null && echo up; done | wc -l", timeout=30)  # under idle_ttl
    return int(r.stdout)


with create_sandbox(image, host=host, idle_ttl=120, lifetime=600) as sb:
    assert servers(sb) == 0
    sb.python("x = 'default'")
    sb.python("x = 'a'", context="a")
    sb.python("x = 'b'", context="b")
    assert [sb.python("x", context=c).stdout for c in (None, "a", "b")] == ["'default'\n", "'a'\n", "'b'\n"]
    assert servers(sb) == 3, servers(sb)  # three interpreters, alive between the calls

    sb.python("hidden = 1", context="a")
    assert "NameError" in sb.python("hidden", context="b").stdout

    # restart and timeouts are per context
    assert sb.python("x", context="a", restart=True).stdout.startswith("[new python session")
    assert "NameError" in sb.python("x", context="a").stdout
    assert sb.python("x", context="b").stdout == "'b'\n"
    assert "TimeoutError" in sb.python("import time\nwhile True: time.sleep(0.1)", timeout=2, context="b").stdout
    assert sb.python("x", context="b").stdout == "'b'\n"  # the alarm reached the loop, the variables stayed
    r = sb.python("import signal; signal.signal(signal.SIGALRM, signal.SIG_IGN)\nwhile True: pass", timeout=2, context="b")
    assert r.returncode == 124  # stuck: that context's interpreter was killed...
    assert sb.python("x").stdout == "'default'\n"  # ...and no other
    assert servers(sb) == 2, servers(sb)  # default and a (b was killed and has not been asked for since)

    # a context name is checked before it reaches a shell
    for bad in ("a b", "../x", "$(touch /tmp/pwned)", "a;touch /tmp/pwned"):
        try:
            sb.python("1", context=bad)
        except ValueError:
            continue
        raise AssertionError(f"accepted {bad!r}")
    assert sb.exec("test ! -e /tmp/pwned").returncode == 0

# the MCP tool, with a context
os.environ.update(AGVISOR_NETWORK="none", AGVISOR_IDLE_TTL="120", AGVISOR_LIFETIME="600")
from agvisor import mcp_server as m  # noqa: E402

try:
    m.python("v = 1", context="one")
    m.python("v = 2", context="two")
    assert m.python("v", context="one") == "1\n" and m.python("v", context="two") == "2\n"
    assert "NameError" in m.python("v")  # no context: the original session, which has no v
    assert m.python("1", context="no good").startswith("invalid python context")
finally:
    m.reset()
print("ok")
