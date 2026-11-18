"""asyncio interface: the same sandboxes, with every blocking call run in a worker thread so the event loop stays free.

    async with AsyncSandbox("python:3.12-slim", lifetime=600) as sb:
        r = await sb.exec("echo hi")
        results = await asyncio.gather(sb.exec("sleep 2"), sb.exec("sleep 2"))   # run side by side

    sb = await create_sandbox("python:3.12-slim")     # the same, without `async with`; await sb.stop() when done

Same arguments, results and errors as Sandbox. Two differences:
- Cancelling an await (a timeout from asyncio.wait_for, a cancelled task) stops waiting, not the command: it runs on in
  the sandbox until its own `timeout=` ends it. Give exec() and the others a `timeout`.
- exec(on_output=) may be a plain function (called in a worker thread) or an `async def` (awaited on your loop)."""
import asyncio
import functools

from .sandbox import Job, Sandbox, list_sandboxes as _list_sandboxes, prune_egress as _prune_egress


def _call(cls, name, after=None):
    """An `async def` that runs cls.<name> in a thread. `after` turns the result into what the caller gets."""
    fn = getattr(cls, name)

    async def call(self, *args, **kw):
        if callable(kw.get("on_output")) and asyncio.iscoroutinefunction(kw["on_output"]):
            loop, cb = asyncio.get_running_loop(), kw["on_output"]
            kw["on_output"] = lambda text: asyncio.run_coroutine_threadsafe(cb(text), loop).result()
        result = await asyncio.to_thread(fn, self._obj, *args, **kw)
        return after(self, result) if after else result

    # ponytail: copies the name, docstring and signature for help(); type checkers don't see these methods
    return functools.update_wrapper(call, fn)


class _Wrapper:
    """Attributes and methods that aren't awaited here come from the wrapped object."""

    def __getattr__(self, name):
        if name == "_obj":  # not set yet (copy, pickle): looking it up must not recurse
            raise AttributeError(name)
        return getattr(self._obj, name)


class AsyncJob(_Wrapper):
    """A Job whose poll(), wait(), read() and kill() are awaited. `id` and `returncode` are plain attributes."""

    def __init__(self, job):
        self._obj = job

    poll, wait, read, kill = (_call(Job, n) for n in ("poll", "wait", "read", "kill"))


class AsyncSandbox(_Wrapper):
    """Sandbox(...) with awaited methods. Takes Sandbox's arguments; `async with` starts it and stops it at the end.
    Plain attributes and the cheap synchronous methods (name, data_dir, rdp_port, desktop_token, ...) come from the
    Sandbox itself."""

    def __init__(self, image=None, **kw):
        self._obj = Sandbox(image, **kw)

    @classmethod
    def of(cls, sandbox):
        """Wrap a Sandbox you already have, e.g. one from list_sandboxes()."""
        self = object.__new__(cls)
        self._obj = sandbox
        return self

    async def __aenter__(self):
        return await self.start()

    async def __aexit__(self, *_):
        await self.stop()

    start = _call(Sandbox, "start", lambda self, _: self)
    restore = _call(Sandbox, "restore", lambda self, _: self)
    spawn = _call(Sandbox, "spawn", lambda self, job: AsyncJob(job))
    exec, python, put, put_dir, get, get_dir, port, stop, delete, snapshot, list_snapshots, delete_snapshot = (
        _call(Sandbox, n) for n in ("exec", "python", "put", "put_dir", "get", "get_dir", "port", "stop", "delete",
                                    "snapshot", "list_snapshots", "delete_snapshot"))


async def create_sandbox(image=None, **kw) -> AsyncSandbox:
    """create_sandbox() with the same arguments, awaited: returns the started AsyncSandbox."""
    return await AsyncSandbox(image, **kw).start()


async def list_sandboxes(host=None, owner=None) -> list[AsyncSandbox]:
    """list_sandboxes(), awaited: the handles come back as AsyncSandbox."""
    return [AsyncSandbox.of(sb) for sb in await asyncio.to_thread(_list_sandboxes, host, owner)]


async def prune_egress(host=None):
    """prune_egress(), awaited."""
    await asyncio.to_thread(_prune_egress, host)
