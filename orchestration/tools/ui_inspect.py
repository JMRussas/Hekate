#!/usr/bin/env python3
#  Orchestration - UI Inspector
#
#  Loads a page in headless Chromium via Playwright and dumps the
#  accessibility tree as structured text.  Designed for AI consumption:
#  the output lets an LLM spot layout bugs, overlapping elements,
#  missing content, and broken navigation without a screenshot.
#
#  Depends on: playwright (pip install playwright && playwright install chromium)
#  Used by:    AI agents during UI development / debugging

"""
Usage:
    python tools/ui_inspect.py [URL] [OPTIONS]

    URL defaults to http://localhost:5173 (Vite dev server).

Options:
    --wait SELECTOR     Wait for a CSS selector before dumping (e.g. ".task-card")
    --timeout MS        Navigation timeout in milliseconds (default: 10000)
    --viewport WxH      Viewport size (default: 1280x720)
    --full              Dump full accessibility tree (verbose)
    --cookies FILE      Load cookies from a JSON file (for authenticated pages)
    --auth USER:PASS    Auto-login via /api/auth/login before navigating

Examples:
    python tools/ui_inspect.py
    python tools/ui_inspect.py http://localhost:5173/projects/1 --wait ".task-card"
    python tools/ui_inspect.py http://localhost:5173/admin --auth admin@test.com:password
    python tools/ui_inspect.py --full --viewport 1920x1080
"""

import argparse
import json
import sys
from urllib.parse import urlparse


def parse_args():
    parser = argparse.ArgumentParser(description="Dump page accessibility tree for AI inspection")
    parser.add_argument("url", nargs="?", default="http://localhost:5173", help="URL to inspect")
    parser.add_argument("--wait", metavar="SELECTOR", help="Wait for CSS selector before dump")
    parser.add_argument("--timeout", type=int, default=10000, help="Navigation timeout (ms)")
    parser.add_argument("--viewport", default="1280x720", help="Viewport WxH")
    parser.add_argument("--full", action="store_true", help="Full accessibility tree (verbose)")
    parser.add_argument("--cookies", metavar="FILE", help="Cookie JSON file")
    parser.add_argument("--auth", metavar="USER:PASS", help="Auto-login via /api/auth/login")
    return parser.parse_args()


def _login(page, base_url: str, credentials: str):
    """POST to /api/auth/login and set the JWT as a cookie."""
    user, password = credentials.split(":", 1)
    resp = page.request.post(
        f"{base_url}/api/auth/login",
        data=json.dumps({"email": user, "password": password}),
        headers={"Content-Type": "application/json"},
    )
    if resp.status != 200:
        print(f"ERROR: Login failed ({resp.status}): {resp.text()}", file=sys.stderr)
        sys.exit(1)
    body = resp.json()
    token = body.get("access_token") or body.get("token")
    if not token:
        print(f"ERROR: No token in login response: {body}", file=sys.stderr)
        sys.exit(1)
    # Store token in localStorage so the React app picks it up
    page.evaluate("(t) => localStorage.setItem('token', t)", token)


def _walk_tree(node, depth=0, full=False):
    """Recursively walk the accessibility tree and yield formatted lines."""
    role = node.get("role", "")
    name = node.get("name", "")

    # Skip generic/empty nodes unless they have interesting children
    skip_roles = {"none", "generic", "LineBreak"}
    children = node.get("children", [])

    if not full and role in skip_roles and not name:
        for child in children:
            yield from _walk_tree(child, depth, full)
        return

    indent = "  " * depth
    parts = [f"{indent}[{role}]"]

    if name:
        # Truncate long names
        display_name = name if len(name) <= 80 else name[:77] + "..."
        parts.append(f'"{display_name}"')

    # Key properties
    props = []
    if node.get("disabled"):
        props.append("disabled")
    if node.get("checked") is not None:
        props.append(f"checked={node['checked']}")
    if node.get("expanded") is not None:
        props.append(f"expanded={node['expanded']}")
    if node.get("selected"):
        props.append("selected")
    if node.get("level"):
        props.append(f"level={node['level']}")
    if node.get("valuetext"):
        props.append(f"value={node['valuetext']}")

    if props:
        parts.append(f"({', '.join(props)})")

    yield " ".join(parts)

    for child in children:
        yield from _walk_tree(child, depth + 1, full)


def main():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("ERROR: playwright not installed.", file=sys.stderr)
        print("  pip install playwright && playwright install chromium", file=sys.stderr)
        sys.exit(1)

    args = parse_args()

    width, height = (int(x) for x in args.viewport.split("x"))

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": width, "height": height})

        # Load cookies if provided
        if args.cookies:
            with open(args.cookies, encoding="utf-8") as f:
                cookies = json.load(f)
            context.add_cookies(cookies)

        page = context.new_page()

        # Auto-login if credentials provided
        if args.auth:
            parsed = urlparse(args.url)
            base_url = f"{parsed.scheme}://{parsed.netloc}"
            _login(page, base_url, args.auth)

        # Navigate
        print(f"Loading {args.url} ({width}x{height})...", file=sys.stderr)
        try:
            page.goto(args.url, timeout=args.timeout, wait_until="networkidle")
        except Exception as e:
            print(f"ERROR: Navigation failed: {e}", file=sys.stderr)
            sys.exit(1)

        # Wait for specific element if requested
        if args.wait:
            try:
                page.wait_for_selector(args.wait, timeout=args.timeout)
            except Exception:
                print(f"WARNING: Selector '{args.wait}' not found within timeout", file=sys.stderr)

        # Get accessibility snapshot
        snapshot = page.accessibility.snapshot()
        if not snapshot:
            print("ERROR: No accessibility tree returned (page may be empty)", file=sys.stderr)
            sys.exit(1)

        # Print page title and URL
        print(f"Page: {page.title()}")
        print(f"URL:  {page.url}")
        print(f"Viewport: {width}x{height}")
        print("---")

        # Print tree
        for line in _walk_tree(snapshot, full=args.full):
            print(line)

        browser.close()


if __name__ == "__main__":
    main()
