"""Unit test for the MCP bridge: tool discovery, selection, and calls.

Runs a tiny streamable-HTTP MCP server in-process and a fake `Session` whose
`http()` forwards to it, then checks that the bridge

  * connects and lists tools from the catalog,
  * registers only the tools that match the task text,
  * calls a tool and flattens its content,
  * adds a tool later through `mcp_search`.

    python tests/test_mcp.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import urllib.request  # noqa: E402

import mcpbridge  # noqa: E402

TOOLS = [
    {
        "name": "documents_search",
        "description": "Search the project document library for a phrase and return matching documents.",
        "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
    },
    {
        "name": "spreadsheet_read_range",
        "description": "Read a cell range from a project spreadsheet and return its values.",
        "inputSchema": {
            "type": "object",
            "properties": {"sheet": {"type": "string"}, "range": {"type": "string"}},
            "required": ["sheet", "range"],
        },
    },
    {
        "name": "email_send",
        "description": "Send an email message from the project mailbox.",
        "inputSchema": {"type": "object", "properties": {"to": {"type": "string"}, "body": {"type": "string"}}, "required": ["to", "body"]},
    },
    {
        "name": "cad_measure",
        "description": "Measure a distance between two points on a CAD drawing sheet.",
        "inputSchema": {"type": "object", "properties": {"sheet": {"type": "string"}}, "required": ["sheet"]},
    },
]

SESSION_ID = "sess-1"


class MCP(BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        method = body.get("method")
        rid = body.get("id")
        if method == "initialize":
            payload = {
                "jsonrpc": "2.0",
                "id": rid,
                "result": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "apps", "version": "1"},
                },
            }
            self._send(payload, session=True)
        elif method == "notifications/initialized":
            self.send_response(202)
            self.send_header("Content-Length", "0")
            self.end_headers()
        elif method == "tools/list":
            self._send({"jsonrpc": "2.0", "id": rid, "result": {"tools": TOOLS}})
        elif method == "tools/call":
            name = body["params"]["name"]
            args = body["params"].get("arguments") or {}
            text = f"{name} -> {json.dumps(args, sort_keys=True)}"
            self._send(
                {"jsonrpc": "2.0", "id": rid, "result": {"content": [{"type": "text", "text": text}]}}
            )
        else:
            self.send_error(400)

    def _send(self, payload, session=False):
        data = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        if session:
            self.send_header("Mcp-Session-Id", SESSION_ID)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass


class FakeSession:
    def __init__(self, url):
        self.workdir = "/app"
        self.mcp_servers = [{"name": "apps", "url": url}]
        self.final_message = ""
        self.logs = []

    def time_left(self):
        return 3600

    async def log(self, text):
        self.logs.append(text)

    async def http(self, url, method="POST", headers=None, body=b"", timeout_sec=120):
        req = urllib.request.Request(url, data=body, headers=headers or {}, method=method)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = resp.read()
                return {
                    "status": resp.status,
                    "headers": dict(resp.headers),
                    "body": data,
                    "truncated": False,
                }
        except urllib.error.HTTPError as exc:
            return {"status": exc.code, "headers": {}, "body": exc.read(), "truncated": False}

    async def exec(self, command, timeout_sec=120):
        return {"exit_code": 1, "output": ""}

    async def usage(self, *a, **k):
        pass


async def main() -> int:
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), MCP)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{httpd.server_address[1]}/mcp/"
    session = FakeSession(url)
    bridge = mcpbridge.MCPBridge(session)

    defs = await bridge.start(
        "Read the cooling load spreadsheet and email the result to the reviewer."
    )
    names = [d["function"]["name"] for d in defs]
    print("catalog:", [t["name"] for t in TOOLS])
    print("registered:", names)
    assert len(bridge.index) == 4, bridge.index
    assert any("spreadsheet_read_range" in n for n in names), names
    assert any("email_send" in n for n in names), names

    spreadsheet = next(n for n in names if "spreadsheet_read_range" in n)
    out = await bridge.call(bridge.resolve(spreadsheet), {"sheet": "S1", "range": "A1:B2"})
    print("call:", out)
    assert "spreadsheet_read_range" in out and "A1:B2" in out

    more = bridge.search("cad drawing distance", 2)
    print("search:", more)
    assert any("cad_measure" in m for m in more)
    added = bridge.register(more)
    assert any("cad_measure" in t["function"]["name"] for t in added), added
    out = await bridge.call(bridge.resolve(added[0]["function"]["name"]), {"sheet": "A-101"})
    print("call after search:", out)
    assert "A-101" in out
    print("RESULT: PASS")
    httpd.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
