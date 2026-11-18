"""A browser that can reach only the hosts you list: egress= with Playwright's Chromium. A page can pull in anything it likes,
and the allowlist decides what is reachable; everything else comes back as a refused tunnel.

    uv run python examples/09_browser_egress.py
    AGVISOR_HOST=ssh://user@my-vm uv run python examples/09_browser_egress.py     # docker on another machine

Chromium ignores HTTPS_PROXY, so the script hands the proxy that agvisor set up to Playwright's `proxy=`, password included.
pip needs pypi.org and files.pythonhosted.org for the Playwright package, so those are on the list too.
"""
import os

from agvisor import Limits, create_sandbox

HOST = os.environ.get("AGVISOR_HOST")  # None: docker on this machine
VERSION = "1.65.2"
IMAGE = f"mcr.microsoft.com/playwright/python:v{VERSION}-noble"
ALLOWED, BLOCKED = "example.com", "example.org"

SCRIPT = """
import os
from urllib.parse import urlsplit
from playwright.sync_api import sync_playwright

u = urlsplit(os.environ["HTTPS_PROXY"])  # http://agvisor:<password>@<proxy ip>:3128
proxy = {"server": f"http://{u.hostname}:{u.port}", "username": u.username, "password": u.password}
with sync_playwright() as p:
    page = p.chromium.launch(proxy=proxy).new_page()
    for url in ("https://example.com", "https://example.org"):
        try:
            page.goto(url, timeout=30000)
            print("reached", url, "-", page.title())
        except Exception as e:
            print("refused", url, "-", str(e).splitlines()[0])
"""

with create_sandbox(IMAGE, host=HOST, egress=["pypi.org", "files.pythonhosted.org", ALLOWED], limits=Limits(memory="2g", shm="1g"), lifetime=900) as sb:
    r = sb.exec(f"pip install -q --root-user-action=ignore playwright=={VERSION}", timeout=300)
    assert r.returncode == 0, r.stderr
    sb.put("/tmp/browse.py", SCRIPT.encode())
    r = sb.exec("python3 /tmp/browse.py", timeout=120)
    print(r.stdout or r.stderr)
    assert f"reached https://{ALLOWED}" in r.stdout and f"refused https://{BLOCKED}" in r.stdout, r.stdout + r.stderr
