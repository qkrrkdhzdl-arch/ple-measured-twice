"""MCP tool bridge with query-based tool selection.

Twelve of the 29 dev tasks (and a similar share of the hidden set) serve their
project applications - email, documents, spreadsheets, CAD - as MCP servers
inside the task container. A task's tool catalog can hold hundreds of tools
and about half a megabyte of JSON: putting all of it in every request is the
single most expensive mistake a harness can make here, and it also buries the
three tools the task actually needs.

So the catalog is fetched once, indexed by name and description, and only the
tools that match the task text are registered. `mcp_search` stays available so
the model can pull in a tool it discovers it needs mid-task.
"""

from __future__ import annotations

import json
import re

import taskbridge

STOP = {
    "the", "and", "for", "with", "that", "this", "from", "into", "your", "you",
    "are", "was", "were", "its", "all", "any", "not", "but", "has", "have",
    "will", "must", "should", "each", "every", "task", "file", "files", "use",
}


def _words(text: str) -> set[str]:
    return {
        w
        for w in re.findall(r"[a-z0-9]{3,}", (text or "").lower())
        if w not in STOP
    }


def _safe(name: str) -> str:
    """Tool names must match the model's function-name rules."""
    cleaned = re.sub(r"[^a-zA-Z0-9_-]", "_", name)
    return cleaned[:60]


def _text_of(result: dict) -> str:
    """Flattens an MCP tools/call result into text the model can read."""
    parts = []
    for item in result.get("content") or []:
        if isinstance(item, dict):
            if item.get("type") == "text" and isinstance(item.get("text"), str):
                parts.append(item["text"])
            elif item.get("type") == "resource":
                resource = item.get("resource") or {}
                if isinstance(resource.get("text"), str):
                    parts.append(resource["text"])
            elif item:
                parts.append(json.dumps(item)[:2000])
    if parts:
        return "\n".join(parts)
    return json.dumps(result)[:4000]


class MCPBridge:
    """Registers a relevant subset of each task MCP server's tools."""

    def __init__(self, session, limit: int = 40):
        self.session = session
        self.limit = limit
        self.clients: dict[str, taskbridge.MCPClient] = {}
        self.catalog: dict[str, list[dict]] = {}
        self.index: list[dict] = []  # {server, name, desc, words}
        self.active: set[str] = set()
        self.ready = False

    async def start(self, instruction: str) -> list[dict]:
        """Connects to every MCP server and returns the initially exposed tools."""
        for server in self.session.mcp_servers:
            name, url = server.get("name"), server.get("url")
            if not isinstance(name, str) or not isinstance(url, str):
                continue
            client = taskbridge.MCPClient(self.session, url, client="measured-twice")
            try:
                await client.initialize()
                tools = await client.list_tools()
            except Exception as exc:  # a broken app must not sink the task
                await self.session.log(f"[mcp] {name}: {type(exc).__name__}: {exc}")
                continue
            self.clients[name] = client
            self.catalog[name] = tools
            for tool in tools:
                self.index.append(
                    {
                        "server": name,
                        "name": tool["name"],
                        "desc": tool.get("description", "")[:600],
                        "schema": tool.get("inputSchema") or {"type": "object", "properties": {}},
                        "words": _words(tool["name"] + " " + tool.get("description", "")),
                    }
                )
        self.ready = bool(self.index)
        await self.session.log(
            f"[mcp] servers={list(self.clients)} tools={len(self.index)}"
        )
        if not self.ready:
            return []
        return self.register(self.search(instruction, self.limit))

    # ------------------------------------------------------------- selection
    def search(self, query: str, k: int = 12) -> list[str]:
        """Ranks catalog tools against a query and returns fully-qualified names."""
        wanted = _words(query)
        scored = []
        for item in self.index:
            overlap = len(wanted & item["words"])
            if overlap:
                scored.append((overlap, -len(item["desc"]), item))
        if not scored:  # nothing matched: take the shortest descriptions
            scored = [(0, -len(i["desc"]), i) for i in self.index]
        scored.sort(key=lambda x: (-x[0], x[1]))
        return [f"{i['server']}::{i['name']}" for _, _, i in scored[:k]]

    def register(self, qualified: list[str]) -> list[dict]:
        """Adds tools to the exposed set and returns their OpenAI definitions."""
        defs = []
        for full in qualified:
            server, _, name = full.partition("::")
            item = next(
                (
                    i
                    for i in self.index
                    if i["server"] == server and i["name"] == name
                ),
                None,
            )
            if item is None or full in self.active:
                continue
            self.active.add(full)
            schema = item["schema"] if isinstance(item["schema"], dict) else {}
            props = schema.get("properties") or {}
            defs.append(
                {
                    "type": "function",
                    "function": {
                        "name": _safe(full),
                        "description": (
                            f"[{server}] {item['desc'][:400]}".strip()
                        ),
                        # Pass through the server's own schema so the model
                        # produces arguments the app accepts.
                        "parameters": {
                            "type": "object",
                            "properties": props,
                            "required": schema.get("required") or [],
                        },
                    },
                }
            )
        return defs

    def meta_tools(self) -> list[dict]:
        """The always-available search tool."""
        if not self.ready:
            return []
        return [
            {
                "type": "function",
                "function": {
                    "name": "mcp_search",
                    "description": (
                        "Search this task's application tools (documents, "
                        "spreadsheets, email, CAD) by keyword and add the best "
                        "matches to your tool list. Use it when no registered "
                        "tool does what you need."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "query": {"type": "string"},
                            "limit": {"type": "integer"},
                        },
                        "required": ["query"],
                    },
                },
            }
        ]

    # ------------------------------------------------------------- execution
    async def call(self, qualified: str, args: dict) -> str:
        server, _, name = qualified.partition("::")
        client = self.clients.get(server)
        if client is None:
            return f"Error: no MCP server named {server}"
        try:
            result = await client.call_tool(name, args or {})
        except Exception as exc:
            return f"Error calling {qualified}: {type(exc).__name__}: {exc}"
        if result.get("isError"):
            return f"Tool reported an error: {_text_of(result)}"
        return _text_of(result)

    def resolve(self, tool_name: str) -> str | None:
        """Maps a model-facing tool name back to `server::tool`."""
        for full in self.active:
            if _safe(full) == tool_name:
                return full
        return None
