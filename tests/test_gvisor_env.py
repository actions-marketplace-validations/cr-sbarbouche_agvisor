"""Needs a docker host with real gVisor (the `gvisor` job in ci.yml has it): create_sandbox(env=), exec(env=, cwd=), spawn(env=, cwd=).

    AGVISOR_HOST=ssh://user@vm AGVISOR_IMAGE=python:3.12-slim uv run python tests/test_gvisor_env.py

AGVISOR_HOST unset -> docker on this machine. AGVISOR_IMAGE: a Debian-based image, already on that host (python:3.12-slim
must be on it too: the egress case runs the allowlist proxy).
"""
import os

from agvisor import create_sandbox

host, image = os.environ.get("AGVISOR_HOST"), os.environ.get("AGVISOR_IMAGE")
AWKWARD = "it's \"quoted\" $HOME `id` a  b\\n =x"  # nothing here may be interpreted by a shell or the env-file parser

with create_sandbox(image, host=host, env={"MODE": "ci", "AWKWARD": AWKWARD, "EMPTY": ""}, lifetime=300) as sb:
    assert sb.exec("printenv MODE").stdout == "ci\n"
    assert sb.exec("printenv AWKWARD").stdout == AWKWARD + "\n"  # byte for byte
    assert sb.exec("printenv EMPTY").stdout == "\n"  # set, and empty
    # the value reached docker's stdin, not a command line: the container's config has it, nothing else did
    assert sb.exec("env | grep -c '^MODE=ci$'").stdout.strip() == "1"

    # exec(env=): for that command only, on top of the sandbox's own
    r = sb.exec("echo $ONLY:$MODE", env={"ONLY": "here"})
    assert r.stdout == "here:ci\n", r
    assert sb.exec("echo [$ONLY]").stdout == "[]\n"  # gone again
    assert sb.exec("printenv MODE", env={"MODE": "override"}).stdout == "override\n"  # the command's own wins
    assert sb.exec("printenv AWKWARD", env={"AWKWARD": AWKWARD}).stdout == AWKWARD + "\n"
    seen = []
    sb.exec("echo $ONLY", env={"ONLY": "streamed"}, on_output=seen.append)  # the streaming path
    assert "".join(seen) == "streamed\n", seen
    assert sb.exec("cat", env={"X": "1"}, input="from stdin").stdout == "from stdin"  # env and stdin together

    # cwd
    assert sb.exec("pwd", cwd="/tmp").stdout == "/tmp\n"
    assert sb.exec("pwd", cwd="/var/tmp", env={"A": "1"}).stdout == "/var/tmp\n"
    assert sb.exec("true", cwd="/does/not/exist").returncode != 0  # docker's own error, not a hang

    # spawn(): the job starts with both
    job = sb.spawn('echo "$JOB_VAR in $(pwd)"', env={"JOB_VAR": "job"}, cwd="/var/tmp")
    assert job.wait(timeout=30) == 0
    assert job.read().decode() == "job in /var/tmp\n"

# a resumed named sandbox keeps the environment of its first start (like limits): that is documented, so check it
with create_sandbox(image, host=host, name="env-test", env={"MODE": "first"}) as sb:
    assert sb.exec("printenv MODE").stdout == "first\n"
    again = create_sandbox(image, host=host, name="env-test", env={"MODE": "second"})  # re-attaches: processes and env intact
    assert again.exec("printenv MODE").stdout == "first\n"
    sb.delete()

# env= and egress= both ride on docker run's stdin: neither may push the other out
with create_sandbox(image, host=host, egress=["pypi.org"], env={"MODE": "ci", "HTTPS_PROXY": "http://elsewhere:1"}, lifetime=300) as sb:
    assert sb.exec("printenv MODE").stdout == "ci\n"
    proxy = sb.exec("printenv HTTPS_PROXY").stdout
    assert proxy.startswith("http://agvisor:") and "elsewhere" not in proxy, proxy  # the proxy's own value wins
    r = sb.exec("python3 -c \"import urllib.request as u; print(u.urlopen('https://pypi.org/simple/six/', timeout=20).status)\"", timeout=60)
    assert r.stdout.strip() == "200", (r.stdout, r.stderr)
print("ok")
