"""CI smoke test: MCP initialize + tools/list against a running image (stdlib only).

Checks the five tool names and that every tool has a title and all four hints.
    python3 scripts/smoke_mcp.py http://127.0.0.1:8000/mcp
"""

import json
import sys
import urllib.request

EXPECTED = {
    "list_feeds",
    "add_episode",
    "list_episodes",
    "get_episode",
    "remove_episode",
}
HINTS = ("readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint")


def call(url: str, body: dict) -> dict:
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": "2025-06-18",
            "Host": "mcp.example.com",
        },
    )
    with urllib.request.urlopen(request, timeout=10) as r:
        text = r.read().decode()
    # Streamable HTTP answers with JSON or with server-sent events.
    for line in text.splitlines():
        if line.startswith("data:"):
            return json.loads(line[5:])
    return json.loads(text)


def main(url: str) -> None:
    init = call(
        url,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "ci", "version": "0"},
            },
        },
    )
    assert "serverInfo" in init["result"], init
    tools = call(url, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})["result"]["tools"]
    names = {t["name"] for t in tools}
    assert names == EXPECTED, f"tools differ: {sorted(names ^ EXPECTED)}"
    for t in tools:
        a = t.get("annotations") or {}
        assert a.get("title") or t.get("title"), f"{t['name']}: no title"
        missing = [h for h in HINTS if h not in a]
        assert not missing, f"{t['name']}: missing {missing}"
    print(f"smoke ok: {len(tools)} tools, all annotated")


if __name__ == "__main__":
    main(sys.argv[1])
