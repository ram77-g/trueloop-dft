"""Check a running TrustLoop the way Talkdesk will reach it.

    python scripts/check_deployment.py https://your-host [api-key]
"""

import asyncio
import json
import sys

import httpx
from mcp import ClientSession
from mcp.client.sse import sse_client
from mcp.client.streamable_http import streamablehttp_client


async def check_mcp(name, connect):
    try:
        async with connect() as streams:
            async with ClientSession(streams[0], streams[1]) as session:
                await session.initialize()
                tools = [t.name for t in (await session.list_tools()).tools]
                result = await session.call_tool("get_card_status", {"customer_id": "C1002", "session_id": "deploy-check"})
                ok = "dispatched" in json.dumps([c.model_dump() for c in result.content])
                print(f"  {name}: {len(tools)} tools, test call {'OK' if ok else 'unexpected result'}")
                return ok
    except Exception as exc:  # report every failure, keep checking the rest
        print(f"  {name}: FAILED ({exc.__class__.__name__}: {exc})")
        return False


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    base = sys.argv[1].rstrip("/")
    key = sys.argv[2] if len(sys.argv) > 2 else ""
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    health = httpx.get(f"{base}/health", timeout=30).json()
    print(f"health: {health}")
    ok = asyncio.run(check_mcp("MCP streamable HTTP  " + base + "/mcp",
                               lambda: streamablehttp_client(f"{base}/mcp", headers=headers)))
    ok &= asyncio.run(check_mcp("MCP SSE              " + base + "/sse",
                                lambda: sse_client(f"{base}/sse", headers=headers)))
    card = httpx.get(f"{base}/api/card", params={"phone": "9845011111"}, headers=headers, timeout=30)
    print(f"  agent card API: HTTP {card.status_code}")
    print("ALL GOOD" if ok and card.status_code == 200 else "SOMETHING FAILED - see above")


if __name__ == "__main__":
    main()
