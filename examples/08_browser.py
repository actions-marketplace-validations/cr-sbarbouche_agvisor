"""A headless Chromium in a sandbox: open a page, read it, take a screenshot. A web page is code you didn't write, and the
browser that runs it is a big attack surface: here it runs behind gVisor, with a memory limit and no way to reach other sandboxes.

    uv run python examples/08_browser.py [URL]
    AGVISOR_HOST=ssh://user@my-vm uv run python examples/08_browser.py     # docker on another machine

The image is Playwright's: it has the browsers, not the Python package, so that is installed first (its version has to match).
"""
import os
import sys

from agvisor import Limits, create_sandbox

HOST = os.environ.get("AGVISOR_HOST")  # None: docker on this machine
URL = sys.argv[1] if len(sys.argv) > 1 else "https://example.com"
VERSION = "1.65.2"
IMAGE = f"mcr.microsoft.com/playwright/python:v{VERSION}-noble"

SCRIPT = """
import sys
from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    browser = p.chromium.launch()  # Playwright's flags are enough: the sandbox is gVisor's, not Chromium's own
    page = browser.new_page()
    page.goto(sys.argv[1], timeout=30000)
    print(page.title())
    print(page.inner_text("body")[:200])
    page.screenshot(path="/tmp/page.png")
    browser.close()
"""

# shm: Chromium needs more than docker's 64 MB /dev/shm. isolated: the internet, but not the other sandboxes.
with create_sandbox(IMAGE, host=HOST, network="isolated", limits=Limits(memory="2g", shm="1g"), lifetime=900) as sb:
    r = sb.exec(f"pip install -q --root-user-action=ignore playwright=={VERSION}", timeout=300)
    assert r.returncode == 0, r.stderr
    sb.put("/tmp/browse.py", SCRIPT.encode())
    r = sb.exec(f"python3 /tmp/browse.py {URL}", timeout=120)
    print(r.stdout or r.stderr)
    png = sb.get("/tmp/page.png")  # the screenshot comes out as bytes
    assert png[:4] == b"\x89PNG", png[:20]
    print(f"screenshot: {len(png)} bytes")
