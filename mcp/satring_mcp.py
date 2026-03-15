"""MCP server for satring.com: discover L402 and x402 paid API services."""

import asyncio
import json
import os

import httpx
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

API_URL = os.environ.get("SATRING_API_URL", "https://satring.com/api/v1")

CATEGORIES = [
    {"slug": "ai-ml", "name": "ai/ml", "description": "Machine learning and AI inference APIs"},
    {"slug": "data", "name": "data", "description": "Data feeds, aggregation, and analytics"},
    {"slug": "finance", "name": "finance", "description": "Financial data, trading, and payment APIs"},
    {"slug": "identity", "name": "identity", "description": "KYC, authentication, and verification"},
    {"slug": "media", "name": "media", "description": "Image, video, and audio processing"},
    {"slug": "search", "name": "search", "description": "Web search, indexing, and discovery"},
    {"slug": "social", "name": "social", "description": "Social networks, communications, and notification APIs"},
    {"slug": "storage", "name": "storage", "description": "File storage and content delivery"},
    {"slug": "tools", "name": "tools", "description": "Developer tools, utilities, and infrastructure"},
]

TOOLS = [
    Tool(
        name="discover_services",
        description="Search satring.com for L402/x402 paid API services by keyword.",
        inputSchema={
            "type": "object",
            "properties": {
                "q": {"type": "string", "description": "Search query"},
                "status": {
                    "type": "string",
                    "enum": ["unverified", "confirmed", "live", "dead"],
                    "description": "Filter by service status",
                },
                "protocol": {
                    "type": "string",
                    "enum": ["L402", "X402"],
                    "description": "Filter by payment protocol",
                },
                "page": {"type": "integer", "minimum": 1, "default": 1},
                "page_size": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20},
            },
            "required": ["q"],
        },
    ),
    Tool(
        name="list_services",
        description=(
            "List all services on satring.com with optional filters and sorting. "
            "Sort by cheapest, top-rated, or most-reviewed."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "category": {
                    "type": "string",
                    "enum": [c["slug"] for c in CATEGORIES],
                    "description": "Filter by category slug",
                },
                "status": {
                    "type": "string",
                    "enum": ["unverified", "confirmed", "live", "dead"],
                },
                "protocol": {"type": "string", "enum": ["L402", "X402"]},
                "sort": {
                    "type": "string",
                    "enum": ["cheapest", "top-rated", "most-reviewed"],
                    "description": "Sort strategy (applied client-side)",
                },
                "page": {"type": "integer", "minimum": 1, "default": 1},
                "page_size": {"type": "integer", "minimum": 1, "maximum": 20, "default": 20},
            },
        },
    ),
    Tool(
        name="get_service",
        description="Get full details for a single service by its slug.",
        inputSchema={
            "type": "object",
            "properties": {
                "slug": {"type": "string", "description": "Service URL slug"},
            },
            "required": ["slug"],
        },
    ),
    Tool(
        name="get_ratings",
        description="Get ratings and reviews for a service.",
        inputSchema={
            "type": "object",
            "properties": {
                "slug": {"type": "string", "description": "Service URL slug"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20},
                "offset": {"type": "integer", "minimum": 0, "default": 0},
            },
            "required": ["slug"],
        },
    ),
    Tool(
        name="list_categories",
        description="List all service categories on satring.com.",
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="compare_services",
        description="Compare two services side by side (pricing, ratings, protocol, status).",
        inputSchema={
            "type": "object",
            "properties": {
                "slug_a": {"type": "string", "description": "First service slug"},
                "slug_b": {"type": "string", "description": "Second service slug"},
            },
            "required": ["slug_a", "slug_b"],
        },
    ),
    Tool(
        name="find_best_service",
        description=(
            "Search for services by keyword and return the top results ranked by strategy: "
            "cheapest, top-rated, fastest (requires health data), or best (composite score)."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "q": {"type": "string", "description": "Search query"},
                "strategy": {
                    "type": "string",
                    "enum": ["cheapest", "top-rated", "fastest", "best"],
                    "default": "best",
                    "description": "Ranking strategy",
                },
            },
            "required": ["q"],
        },
    ),
]


async def _fetch(client: httpx.AsyncClient, path: str, params: dict | None = None) -> dict | list:
    """GET a JSON endpoint, returning parsed data or an error dict."""
    try:
        resp = await client.get(f"{API_URL}{path}", params=params, timeout=15)
        resp.raise_for_status()
        return resp.json()
    except httpx.HTTPStatusError as e:
        return {"error": f"HTTP {e.response.status_code}", "detail": e.response.text[:500]}
    except httpx.RequestError as e:
        return {"error": str(e)}


def _sort_services(services: list[dict], strategy: str) -> list[dict]:
    """Sort a list of service dicts by the given strategy."""
    if strategy == "cheapest":
        return sorted(services, key=lambda s: s.get("pricing_sats", 0))
    if strategy == "top-rated":
        rated = [s for s in services if s.get("rating_count", 0) > 0]
        unrated = [s for s in services if s.get("rating_count", 0) == 0]
        return sorted(rated, key=lambda s: s.get("avg_rating", 0), reverse=True) + unrated
    if strategy == "most-reviewed":
        return sorted(services, key=lambda s: s.get("rating_count", 0), reverse=True)
    # "best": composite score
    def score(s: dict) -> float:
        rating = s.get("avg_rating", 0) * 20  # 0-100
        price_penalty = min(s.get("pricing_sats", 0), 1000)
        return rating + (1000 - price_penalty)
    return sorted(services, key=score, reverse=True)


def _compare(a: dict, b: dict) -> dict:
    """Build a side-by-side comparison of two services."""
    fields = [
        "name", "slug", "url", "protocol", "pricing_sats", "pricing_usd",
        "avg_rating", "rating_count", "domain_verified",
    ]
    comparison = {}
    for f in fields:
        comparison[f] = {"a": a.get(f), "b": b.get(f)}
    comparison["categories"] = {
        "a": [c["name"] for c in a.get("categories", [])],
        "b": [c["name"] for c in b.get("categories", [])],
    }
    return comparison


server = Server("satring")


@server.list_tools()
async def handle_list_tools() -> list[Tool]:
    return TOOLS


@server.call_tool()
async def handle_call_tool(name: str, arguments: dict) -> list[TextContent]:
    async with httpx.AsyncClient() as client:
        result = await _dispatch(client, name, arguments)
    text = json.dumps(result, indent=2, default=str)
    return [TextContent(type="text", text=text)]


async def _dispatch(client: httpx.AsyncClient, name: str, args: dict) -> dict | list:
    if name == "discover_services":
        params = {"q": args["q"]}
        for key in ("status", "protocol", "page", "page_size"):
            if key in args:
                params[key] = args[key]
        return await _fetch(client, "/search", params)

    if name == "list_services":
        params: dict = {}
        for key in ("category", "status", "protocol", "page", "page_size"):
            if key in args:
                params[key] = args[key]
        sort = args.get("sort")
        data = await _fetch(client, "/services", params)
        if sort and isinstance(data, dict) and "services" in data:
            data["services"] = _sort_services(data["services"], sort)
            data["sorted_by"] = sort
        return data

    if name == "get_service":
        return await _fetch(client, f"/services/{args['slug']}")

    if name == "get_ratings":
        params = {}
        if "limit" in args:
            params["limit"] = args["limit"]
        if "offset" in args:
            params["offset"] = args["offset"]
        return await _fetch(client, f"/services/{args['slug']}/ratings", params)

    if name == "list_categories":
        return CATEGORIES

    if name == "compare_services":
        a, b = await asyncio.gather(
            _fetch(client, f"/services/{args['slug_a']}"),
            _fetch(client, f"/services/{args['slug_b']}"),
        )
        if "error" in a:
            return {"error": f"Service '{args['slug_a']}' not found", "detail": a}
        if "error" in b:
            return {"error": f"Service '{args['slug_b']}' not found", "detail": b}
        return _compare(a, b)

    if name == "find_best_service":
        strategy = args.get("strategy", "best")
        if strategy == "fastest":
            return {
                "note": "The 'fastest' strategy requires health probe latency data, which is not available via the free API. Use 'best' or 'top-rated' instead.",
                "strategy": strategy,
            }
        data = await _fetch(client, "/search", {"q": args["q"], "page_size": 20})
        if isinstance(data, dict) and "services" in data:
            sorted_services = _sort_services(data["services"], strategy)
            return {
                "strategy": strategy,
                "query": args["q"],
                "total_found": data.get("total", 0),
                "top_results": sorted_services[:5],
            }
        return data

    return {"error": f"Unknown tool: {name}"}


async def main():
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


def main_sync():
    asyncio.run(main())


if __name__ == "__main__":
    main_sync()
