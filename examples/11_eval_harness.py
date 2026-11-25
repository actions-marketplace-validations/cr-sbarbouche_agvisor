"""One sandbox per trial: the shape of an agent evaluation. A warm Pool hands each task a fresh sandbox, tasks run side by side under
asyncio, and a script inside grades the result. A second part resets one workspace between attempts with snapshot() and restore().

    uv run python examples/11_eval_harness.py

The "agent" here is a scripted function so that it runs anywhere with no API key and always gives the same answer; in a real harness
it is your model's loop calling sb.exec(), sb.put() and sb.get(). No network: nothing a trial runs can reach out.
"""
import asyncio
import os
import time

from agvisor import Pool, aio, create_sandbox

HOST = os.environ.get("AGVISOR_HOST")  # None: docker on this machine
IMAGE = "python:3.12-slim"

# each task: files that go into the sandbox, and a check that exits 0 when the work is right
TASKS = {
    "add": {"solution.py": "def add(a, b):\n    return 0\n", "check.py": "from solution import add\nassert add(2, 3) == 5\n"},
    "reverse": {"solution.py": "def rev(s):\n    return s\n", "check.py": "from solution import rev\nassert rev('abc') == 'cba'\n"},
    "fizz": {"solution.py": "def fizz(n):\n    return str(n)\n", "check.py": "from solution import fizz\nassert fizz(3) == 'Fizz' and fizz(5) == 'Buzz' and fizz(7) == '7'\n"},
}
# what the scripted agent writes; "fizz" is wrong on purpose, so the table has a failure in it
FIXES = {
    "add": "def add(a, b):\n    return a + b\n",
    "reverse": "def rev(s):\n    return s[::-1]\n",
    "fizz": "def fizz(n):\n    return 'Fizz' if n % 3 == 0 else str(n)\n",
}


async def agent(sb, name):
    await sb.put("/workspace/solution.py", FIXES[name].encode())  # a model would read the files, run things, edit


async def trial(pool, name, files):
    t0 = time.monotonic()
    sb = aio.AsyncSandbox.of(await asyncio.to_thread(pool.acquire))  # at once: a spare was already started
    try:
        for path, text in files.items():
            await sb.put(f"/workspace/{path}", text.encode())
        await agent(sb, name)
        r = await sb.exec("cd /workspace && python3 check.py", timeout=60)  # the grade: the exit code of the check
        return name, r.returncode == 0, time.monotonic() - t0
    finally:
        await sb.stop()  # one sandbox per trial: nothing carries over to the next


async def main():
    with Pool(len(TASKS), IMAGE, host=HOST, network="none", lifetime=600) as pool:
        t0 = time.monotonic()
        results = await asyncio.gather(*(trial(pool, name, files) for name, files in TASKS.items()))
        print(f"{len(results)} trials in {time.monotonic() - t0:.1f} s, side by side")
    for name, passed, seconds in results:
        print(f"  {name:8} {'pass' if passed else 'FAIL'}  {seconds:.1f} s")
    assert [p for _, p, _ in results] == [True, True, False]


asyncio.run(main())

# Retries from a clean slate: one named sandbox, its workspace saved once and put back before every attempt.
files = TASKS["fizz"]
with create_sandbox(IMAGE, host=HOST, name="eval-retry", network="none", lifetime=900) as sb:
    try:
        for path, text in files.items():
            sb.put(f"/workspace/{path}", text.encode())
        sb.snapshot("clean")

        sb.put("/workspace/solution.py", b"def fizz(n):\n    return 'wrong'\n")  # attempt 1: gets it wrong, and litters the workspace
        sb.exec("echo junk > /workspace/notes.txt")
        print("attempt 1:", "pass" if sb.exec("cd /workspace && python3 check.py").returncode == 0 else "FAIL")

        sb = sb.restore("clean")  # the workspace is exactly as it was before attempt 1; the sandbox is a fresh container
        assert sb.exec("ls /workspace").stdout.split() == ["check.py", "solution.py"]
        assert sb.get("/workspace/solution.py").decode() == files["solution.py"]

        sb.put("/workspace/solution.py", FIXES["fizz"].encode())  # attempt 2
        print("attempt 2:", "pass" if sb.exec("cd /workspace && python3 check.py").returncode == 0 else "FAIL")
    finally:
        sb.delete()  # also erases the snapshot
