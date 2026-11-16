"""A pool of sandboxes that are already started, so acquire() doesn't wait for `docker run` (and, on a remote host, for
the round trips of starting one)."""
import collections
import threading

from .sandbox import SandboxGone, create_sandbox


class Pool:
    """`size` sandboxes kept started and unused. acquire() hands one over at once and starts a replacement in the
    background; when none is ready it makes one on the spot, as create_sandbox() would.

        with Pool(2, "python:3.12-slim", network="none", lifetime=600) as pool:
            with pool.acquire() as sb:
                sb.exec("echo hi")

    Takes create_sandbox()'s arguments, which every sandbox of the pool shares. Headless and anonymous only: no `name=`,
    `owner=` or `gui=`. `lifetime=` or `idle_ttl=` is required: it bounds the spares a crashed process leaves behind, and
    it counts from when a spare was made, not from acquire(). A spare that expired meanwhile is skipped. Each spare is a
    running sandbox, so it holds the memory of an idle one (tens of MB under gVisor) until it is acquired or close()d.
    You own what acquire() returns: stop() it, or use it in `with`. Threads may share a pool.
    `error` is the last exception from making a spare in the background (docker down, image missing): acquire() then
    makes its own sandbox, which raises the same thing again if it still fails."""

    def __init__(self, size=2, image=None, **kw):
        if size < 1:
            raise ValueError("size must be >= 1")
        if kw.get("name") or kw.get("owner") or kw.get("gui"):
            raise ValueError("a pool makes anonymous headless sandboxes: no name=, owner= or gui=")
        if not (kw.get("lifetime") or kw.get("idle_ttl")):
            raise ValueError("pass lifetime= or idle_ttl=: spares that nobody acquires must expire on their own")
        self.size, self.error, self._kw = size, None, {"image": image, **kw}
        self._spares, self._making, self._closed, self._lock = collections.deque(), 0, False, threading.Lock()
        self._fill()

    @property
    def spares(self):
        """How many sandboxes are ready now."""
        return len(self._spares)

    def _fill(self):
        with self._lock:
            n = 0 if self._closed else max(0, self.size - len(self._spares) - self._making)
            self._making += n
        for _ in range(n):
            threading.Thread(target=self._make, daemon=True).start()

    def _make(self):
        sb = None
        try:
            sb = create_sandbox(**self._kw)
        except Exception as e:  # nobody is waiting on this thread: leave it to acquire() and `error`
            self.error = e
        with self._lock:
            self._making -= 1
            if sb and not self._closed:
                self._spares.append(sb)
                return
        if sb:  # close() came while it was being made
            sb.stop()

    def acquire(self):
        """A started sandbox of its own; stop() it when done."""
        while True:
            with self._lock:
                if self._closed:
                    raise RuntimeError("the pool is closed")
                sb = self._spares.popleft() if self._spares else None
            self._fill()
            if sb is None:
                return create_sandbox(**self._kw)
            try:  # alive? (with idle_ttl, this also counts as activity)
                ok = sb.exec("true", timeout=sb._short()).returncode == 0
            except SandboxGone:
                ok = False  # it expired or was killed while it waited
            if ok:
                return sb
            sb.stop()  # whatever is left of it

    def close(self):
        """Stop every spare. Sandboxes you acquired are yours and stay."""
        with self._lock:
            self._closed = True
            spares, self._spares = list(self._spares), collections.deque()
        for sb in spares:
            sb.stop()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
