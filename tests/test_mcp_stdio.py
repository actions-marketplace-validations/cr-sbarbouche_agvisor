"""Needs a docker host with real gVisor (the `gvisor` job in ci.yml has it): agvisor-mcp started as a subprocess and driven
over stdio by the real mcp client, the way an agent reaches it. test_gvisor_mcp.py calls the tool functions directly instead.

    AGVISOR_HOST=ssh://user@vm AGVISOR_IMAGE=python:3.12-slim uv run --extra mcp python tests/test_mcp_stdio.py

AGVISOR_HOST unset -> docker on this machine. AGVISOR_IMAGE: Debian/Ubuntu-based, with python3.
"""
import asyncio
import os
import subprocess
import uuid

from mcp import Client, StdioServerParameters

from agvisor import Sandbox

TOOLS = {"run", "write_file", "read_file", "edit_file", "python", "list_files", "start_job", "job_output", "kill_job", "reset", "stats"}
host = os.environ.get("AGVISOR_HOST")
# the stdio client hands the server only a few variables by default: pass everything, plus a throwaway-friendly config
env = {**os.environ, "AGVISOR_NETWORK": "none", "AGVISOR_IDLE_TTL": "120", "AGVISOR_LIFETIME": "600"}
env.pop("AGVISOR_NAME", None)  # anonymous sandbox: the snapshot tools only exist for named ones


def leftovers():  # sandboxes agvisor made and nobody deleted
    docker = ["docker", *(["-H", host] if host else [])]
    return subprocess.run([*docker, "ps", "-a", "-q", "--filter", "label=agvisor=1"], capture_output=True, text=True).stdout.split()


async def text(c, tool, **args):
    r = await c.call_tool(tool, args)
    return "".join(b.text for b in r.content)


async def named(name):
    """AGVISOR_NAME: /workspace is a volume, and snapshots with tags appear; at most AGVISOR_SNAPSHOTS of them."""
    params = StdioServerParameters(command="agvisor-mcp", env={**env, "AGVISOR_NAME": name, "AGVISOR_SNAPSHOTS": "2"})
    async with Client(params) as c:
        assert {t.name for t in (await c.list_tools()).tools} == TOOLS | {"snapshot", "restore", "list_snapshots", "delete_snapshot"}
        await text(c, "write_file", path="/workspace/a.txt", content="one")
        assert "saved" in await text(c, "snapshot", tag="first")
        await text(c, "write_file", path="/workspace/a.txt", content="two")
        assert "saved" in await text(c, "snapshot")  # no tag: "checkpoint"
        assert (await text(c, "list_snapshots")).split() == ["checkpoint", "first"]
        assert "already 2" in await text(c, "snapshot", tag="third")  # AGVISOR_SNAPSHOTS=2
        assert "restored" in await text(c, "restore", tag="first")
        assert await text(c, "read_file", path="/workspace/a.txt") == "one"
        assert "no snapshot" in await text(c, "restore", tag="nope")
        assert "deleted" in await text(c, "delete_snapshot", tag="first")
        assert (await text(c, "list_snapshots")).split() == ["checkpoint"]
        assert "saved" in await text(c, "snapshot", tag="third")  # a slot is free again
        assert "restored" in await text(c, "restore")
        assert await text(c, "read_file", path="/workspace/a.txt") == "two"  # "checkpoint" was taken after the second write


async def main():
    before = leftovers()
    name = f"mcptest-{uuid.uuid4().hex[:8]}"  # unique: never meets someone's sandbox
    try:
        await named(name)
    finally:
        Sandbox(name=name, host=host).delete()  # the volume and its snapshots outlive the server, even after a failed assert
    async with Client(StdioServerParameters(command="agvisor-mcp", env=env)) as c:
        assert {t.name for t in (await c.list_tools()).tools} == TOOLS

        out = await text(c, "run", command="uname -r")
        assert "gvisor" in out and "[exit 0]" in out, out  # really runsc, not a plain container

        assert "wrote" in await text(c, "write_file", path="/tmp/a.txt", content="héllo\n")
        assert "héllo" in await text(c, "read_file", path="/tmp/a.txt")
        assert "a.txt" in await text(c, "list_files", path="/tmp")

        await text(c, "python", code="x = 41")
        assert "42" in await text(c, "python", code="print(x + 1)")  # the session kept x

        job = (await text(c, "start_job", command="echo job-ran")).strip()
        for _ in range(30):
            if "job-ran" in (log := await text(c, "job_output", job_id=job)):
                break
            await asyncio.sleep(1)
        assert "job-ran" in log, log

        out = await text(c, "stats")
        assert "memory" in out and "MiB" in out and "processes" in out, out  # a line for the agent: cpu, memory, processes, each with its limit

        assert "cannot" in await text(c, "read_file", path="/tmp/missing")  # a failure comes back as text, the server lives
        assert "[exit 0]" in await text(c, "run", command="true")
    for _ in range(30):  # the server's atexit reset() runs once stdin closes
        if set(leftovers()) <= set(before):
            break
        await asyncio.sleep(1)
    assert set(leftovers()) <= set(before), "the sandbox outlived the server"


asyncio.run(main())
print("ok")
