"""Needs a docker host with real gVisor (the `gvisor` job in ci.yml has it): the asyncio interface, agvisor.aio.

    AGVISOR_HOST=ssh://user@vm AGVISOR_IMAGE=python:3.12-slim uv run python tests/test_gvisor_aio.py

AGVISOR_HOST unset -> docker on this machine. AGVISOR_IMAGE: Debian/Ubuntu-based, with python3, already on that host.
"""
import asyncio
import os
import time
import uuid

from agvisor import aio

host, image = os.environ.get("AGVISOR_HOST"), os.environ.get("AGVISOR_IMAGE")


async def main():
    async with aio.AsyncSandbox(image, host=host, network="none", lifetime=300) as sb:
        t0 = time.monotonic()
        rs = await asyncio.gather(*(sb.exec("sleep 3; echo done") for _ in range(4)))
        took = time.monotonic() - t0
        assert [r.stdout for r in rs] == ["done\n"] * 4 and took < 9, took  # one after the other it would be 12 s and more

        got = []

        async def on_output(text):
            got.append(text)

        r = await sb.exec("echo one; sleep 1; echo two", on_output=on_output)
        assert r.returncode == 0 and "".join(got) == "one\ntwo\n" and len(got) >= 2, got  # as it is produced, not at the end

        await sb.put("/tmp/a.txt", b"x\x00y")
        assert await sb.get("/tmp/a.txt") == b"x\x00y"
        await sb.python("n = 41")
        assert (await sb.python("n + 1")).stdout.strip() == "42"
        job = await sb.spawn("echo bg; exit 3")
        assert await job.wait(timeout=30) == 3 and await job.read() == b"bg\n"
        assert any(b.name == sb.name for b in await aio.list_sandboxes(host))

    boxes = await asyncio.gather(*(aio.create_sandbox(image, host=host, network="none", lifetime=300) for _ in range(3)))
    try:  # three at once: three different containers
        outs = await asyncio.gather(*(b.exec("hostname") for b in boxes))
        assert len({o.stdout for o in outs}) == 3, outs
    finally:
        await asyncio.gather(*(b.stop() for b in boxes))

    sb = await aio.create_sandbox(image, host=host, name=f"aiotest-{uuid.uuid4().hex[:8]}", network="none")
    try:
        await sb.put("/workspace/f", b"1")
        await sb.snapshot("s")
        await sb.put("/workspace/f", b"2")
        assert await sb.list_snapshots() == ["s"]
        assert await sb.restore("s") is sb and await sb.get("/workspace/f") == b"1"
    finally:
        await sb.delete()  # volumes too, even when an assert failed


asyncio.run(main())
print("ok")
