#!/usr/bin/env python3
"""
Simple Playwright-based MCP server.

Implements the minimum subset of the Model Context Protocol required by DiffVisionAgent:
- tools/list
- tools/call (playwright.capture)
- resources/list (for inspection/debugging)
"""

import asyncio
import base64
import json
import os
import subprocess
import sys
import traceback
from typing import Any, Dict, List, Optional


def _ensure_playwright_ready():
    try:
        import playwright  # noqa: F401
    except ImportError:
        subprocess.run([sys.executable, "-m", "pip", "install", "playwright"], check=True)
    subprocess.run(["playwright", "install", "chromium", "--with-deps"], check=False)


_ensure_playwright_ready()
from playwright.async_api import async_playwright  # noqa: E402


TOOLS = [
    {
        "name": "playwright.capture",
        "description": "Capture full-page and focus screenshots via Playwright.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "url": {"type": "string"},
                "selector": {"type": ["string", "null"]},
                "steps": {"type": "array", "items": {}},
                "waitAfterNavigateMs": {"type": "integer"},
                "label": {"type": "string"},
            },
            "required": ["url"],
            "additionalProperties": True,
        },
    }
]


def _respond(payload: Dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload) + "\n")
    sys.stdout.flush()


async def _run_steps(page, steps: List[Any]):
    for step in steps or []:
        action = None
        selector = None
        value = None
        if isinstance(step, dict):
            action = step.get("action")
            selector = step.get("selector")
            value = step.get("value")
        elif isinstance(step, str):
            # naive parsing: "click:.cta"
            parts = step.split(":", 1)
            if len(parts) == 2:
                action, selector = parts
        if not action:
            continue
        try:
            if action.lower() == "click" and selector:
                await page.click(selector)
            elif action.lower() in {"fill", "type"} and selector and value is not None:
                await page.fill(selector, str(value))
            elif action.lower() in {"wait", "pause"}:
                await page.wait_for_timeout(int(value) if value else 1000)
        except Exception:
            continue


async def _capture(arguments: Dict[str, Any]) -> Dict[str, Any]:
    url = arguments.get("url")
    if not url:
        raise ValueError("url is required")
    selector = arguments.get("selector")
    wait_ms = max(0, int(arguments.get("waitAfterNavigateMs") or os.getenv("PLAYWRIGHT_WAIT_AFTER_MS", "2000")))
    steps = arguments.get("steps") or []
    meta = {"url": url, "selector": selector, "waitMs": wait_ms}

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page(viewport={"width": 1280, "height": 720})
        try:
            await page.goto(url, wait_until="networkidle", timeout=30000)
            if wait_ms:
                await page.wait_for_timeout(wait_ms)
            await _run_steps(page, steps)
            full_png = await page.screenshot(full_page=True)
            focus_png = None
            if selector:
                locator = page.locator(selector)
                if await locator.count() > 0:
                    focus_png = await locator.first.screenshot()
            await browser.close()
        except Exception as exc:
            await browser.close()
            meta["error"] = str(exc)
            raise

    content = [
        {"type": "image/png", "data": base64.b64encode(full_png).decode("utf-8")},
        {"type": "text", "text": json.dumps(meta)},
    ]
    if focus_png:
        content.append({"type": "image/png", "data": base64.b64encode(focus_png).decode("utf-8")})
    return {"content": content}


def _handle_tools_list(request: Dict[str, Any]) -> None:
    _respond({"jsonrpc": "2.0", "id": request.get("id"), "result": {"tools": TOOLS}})


def _handle_resources_list(request: Dict[str, Any]) -> None:
    base_url = os.getenv("PLAYWRIGHT_BASE_URL", "http://localhost:5173")
    resources = [
        {
            "uri": "playwright://sandbox",
            "name": "Playwright Sandbox",
            "metadata": {"baseUrl": base_url},
        }
    ]
    _respond({"jsonrpc": "2.0", "id": request.get("id"), "result": {"resources": resources}})


def _handle_tools_call(request: Dict[str, Any]) -> None:
    params = request.get("params", {})
    name = params.get("name")
    if name != "playwright.capture":
        _respond(
            {
                "jsonrpc": "2.0",
                "id": request.get("id"),
                "error": {"code": -32601, "message": f"Unknown tool: {name}"},
            }
        )
        return
    arguments = params.get("arguments", {})
    try:
        result = asyncio.run(_capture(arguments))
        _respond({"jsonrpc": "2.0", "id": request.get("id"), "result": result})
    except Exception as exc:
        _respond(
            {
                "jsonrpc": "2.0",
                "id": request.get("id"),
                "error": {"code": -32000, "message": str(exc), "data": traceback.format_exc()},
            }
        )


def main():
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            continue
        method = request.get("method")
        if method == "tools/list":
            _handle_tools_list(request)
        elif method == "tools/call":
            _handle_tools_call(request)
        elif method == "resources/list":
            _handle_resources_list(request)
        else:
            _respond(
                {
                    "jsonrpc": "2.0",
                    "id": request.get("id"),
                    "error": {"code": -32601, "message": f"Unsupported method: {method}"},
                }
            )


if __name__ == "__main__":
    main()
