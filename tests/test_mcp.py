"""Tests for the satring MCP server (all 7 tools)."""

import json
from unittest.mock import AsyncMock, patch

import pytest

# Import internals directly so we can test without stdio transport
satring_mcp = pytest.importorskip("satring_mcp", reason="satring_mcp not installed")
CATEGORIES = satring_mcp.CATEGORIES
TOOLS = satring_mcp.TOOLS
_compare = satring_mcp._compare
_dispatch = satring_mcp._dispatch
_sort_services = satring_mcp._sort_services
handle_call_tool = satring_mcp.handle_call_tool
handle_list_tools = satring_mcp.handle_list_tools

# --- Fixtures: canned API responses ---

SERVICE_A = {
    "id": 1,
    "name": "Cheap API",
    "slug": "cheap-api",
    "url": "https://cheap.test.com",
    "description": "A cheap service",
    "pricing_sats": 10,
    "pricing_model": "per-request",
    "protocol": "L402",
    "owner_name": "Alice",
    "logo_url": "",
    "avg_rating": 4.5,
    "rating_count": 8,
    "domain_verified": True,
    "categories": [{"id": 1, "name": "ai/ml", "slug": "ai-ml", "description": "ML"}],
    "created_at": "2025-01-01T00:00:00",
}

SERVICE_B = {
    "id": 2,
    "name": "Premium API",
    "slug": "premium-api",
    "url": "https://premium.test.com",
    "description": "An expensive service",
    "pricing_sats": 500,
    "pricing_model": "per-request",
    "protocol": "x402",
    "owner_name": "Bob",
    "logo_url": "",
    "x402_network": "eip155:8453",
    "x402_asset": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
    "x402_pay_to": "0xWallet",
    "pricing_usd": "0.25",
    "avg_rating": 3.0,
    "rating_count": 2,
    "domain_verified": False,
    "categories": [{"id": 3, "name": "finance", "slug": "finance", "description": "Finance"}],
    "created_at": "2025-02-01T00:00:00",
}

SERVICE_C = {
    "id": 3,
    "name": "Unrated API",
    "slug": "unrated-api",
    "url": "https://unrated.test.com",
    "description": "No ratings yet",
    "pricing_sats": 200,
    "pricing_model": "per-request",
    "protocol": "L402",
    "owner_name": "Charlie",
    "logo_url": "",
    "avg_rating": 0,
    "rating_count": 0,
    "domain_verified": False,
    "categories": [],
    "created_at": "2025-03-01T00:00:00",
}

LIST_RESPONSE = {
    "services": [SERVICE_A, SERVICE_B, SERVICE_C],
    "total": 3,
    "page": 1,
    "page_size": 20,
}

RATINGS = [
    {"id": 1, "score": 5, "comment": "Great", "reviewer_name": "Alice", "created_at": "2025-01-15T00:00:00"},
    {"id": 2, "score": 4, "comment": "Good", "reviewer_name": "Bob", "created_at": "2025-01-10T00:00:00"},
]


def _mock_fetch(responses: dict):
    """Return an AsyncMock for _fetch that returns canned data by path prefix."""
    async def fake_fetch(client, path, params=None):
        for prefix, data in responses.items():
            if path.startswith(prefix):
                return data
        return {"error": "not found"}
    return fake_fetch


# --- Test list_tools ---

@pytest.mark.asyncio
async def test_list_tools_returns_all_seven():
    tools = await handle_list_tools()
    assert len(tools) == 7
    names = {t.name for t in tools}
    assert names == {
        "discover_services", "list_services", "get_service",
        "get_ratings", "list_categories", "compare_services",
        "find_best_service",
    }


@pytest.mark.asyncio
async def test_list_tools_have_input_schemas():
    tools = await handle_list_tools()
    for tool in tools:
        assert tool.inputSchema is not None
        assert tool.inputSchema["type"] == "object"


# --- Test discover_services ---

@pytest.mark.asyncio
async def test_discover_services():
    mock = _mock_fetch({"/search": LIST_RESPONSE})
    with patch("satring_mcp._fetch", side_effect=mock):
        result = await _dispatch(None, "discover_services", {"q": "api"})
    assert result["total"] == 3
    assert len(result["services"]) == 3


@pytest.mark.asyncio
async def test_discover_services_with_filters():
    filtered = {**LIST_RESPONSE, "services": [SERVICE_A], "total": 1}
    mock = _mock_fetch({"/search": filtered})
    with patch("satring_mcp._fetch", side_effect=mock):
        result = await _dispatch(None, "discover_services", {
            "q": "cheap", "protocol": "L402", "status": "live",
        })
    assert result["total"] == 1
    assert result["services"][0]["slug"] == "cheap-api"


# --- Test list_services ---

@pytest.mark.asyncio
async def test_list_services_no_sort():
    mock = _mock_fetch({"/services": LIST_RESPONSE})
    with patch("satring_mcp._fetch", side_effect=mock):
        result = await _dispatch(None, "list_services", {})
    assert result["total"] == 3
    assert "sorted_by" not in result


@pytest.mark.asyncio
async def test_list_services_sort_cheapest():
    mock = _mock_fetch({"/services": LIST_RESPONSE})
    with patch("satring_mcp._fetch", side_effect=mock):
        result = await _dispatch(None, "list_services", {"sort": "cheapest"})
    assert result["sorted_by"] == "cheapest"
    prices = [s["pricing_sats"] for s in result["services"]]
    assert prices == sorted(prices)


@pytest.mark.asyncio
async def test_list_services_sort_top_rated():
    mock = _mock_fetch({"/services": LIST_RESPONSE})
    with patch("satring_mcp._fetch", side_effect=mock):
        result = await _dispatch(None, "list_services", {"sort": "top-rated"})
    assert result["sorted_by"] == "top-rated"
    services = result["services"]
    # Rated services come first, sorted by rating desc
    rated = [s for s in services if s["rating_count"] > 0]
    assert rated[0]["slug"] == "cheap-api"  # 4.5 rating
    assert rated[1]["slug"] == "premium-api"  # 3.0 rating
    # Unrated at the end
    assert services[-1]["slug"] == "unrated-api"


@pytest.mark.asyncio
async def test_list_services_sort_most_reviewed():
    mock = _mock_fetch({"/services": LIST_RESPONSE})
    with patch("satring_mcp._fetch", side_effect=mock):
        result = await _dispatch(None, "list_services", {"sort": "most-reviewed"})
    counts = [s["rating_count"] for s in result["services"]]
    assert counts == sorted(counts, reverse=True)


@pytest.mark.asyncio
async def test_list_services_with_category_filter():
    mock = _mock_fetch({"/services": {**LIST_RESPONSE, "services": [SERVICE_A], "total": 1}})
    with patch("satring_mcp._fetch", side_effect=mock):
        result = await _dispatch(None, "list_services", {"category": "ai-ml"})
    assert result["total"] == 1


# --- Test get_service ---

@pytest.mark.asyncio
async def test_get_service():
    mock = _mock_fetch({"/services/cheap-api": SERVICE_A})
    with patch("satring_mcp._fetch", side_effect=mock):
        result = await _dispatch(None, "get_service", {"slug": "cheap-api"})
    assert result["name"] == "Cheap API"
    assert result["protocol"] == "L402"


@pytest.mark.asyncio
async def test_get_service_not_found():
    mock = _mock_fetch({"/services/nope": {"error": "HTTP 404", "detail": "Not found"}})
    with patch("satring_mcp._fetch", side_effect=mock):
        result = await _dispatch(None, "get_service", {"slug": "nope"})
    assert "error" in result


# --- Test get_ratings ---

@pytest.mark.asyncio
async def test_get_ratings():
    mock = _mock_fetch({"/services/cheap-api/ratings": RATINGS})
    with patch("satring_mcp._fetch", side_effect=mock):
        result = await _dispatch(None, "get_ratings", {"slug": "cheap-api"})
    assert len(result) == 2
    assert result[0]["score"] == 5


@pytest.mark.asyncio
async def test_get_ratings_with_limit_offset():
    mock = _mock_fetch({"/services/cheap-api/ratings": [RATINGS[1]]})
    with patch("satring_mcp._fetch", side_effect=mock):
        result = await _dispatch(None, "get_ratings", {"slug": "cheap-api", "limit": 1, "offset": 1})
    assert len(result) == 1


# --- Test list_categories ---

@pytest.mark.asyncio
async def test_list_categories():
    result = await _dispatch(None, "list_categories", {})
    assert result == CATEGORIES
    assert len(result) == 9
    slugs = {c["slug"] for c in result}
    assert "ai-ml" in slugs
    assert "tools" in slugs


# --- Test compare_services ---

@pytest.mark.asyncio
async def test_compare_services():
    async def mock(client, path, params=None):
        if "cheap-api" in path:
            return SERVICE_A
        if "premium-api" in path:
            return SERVICE_B
        return {"error": "not found"}

    with patch("satring_mcp._fetch", side_effect=mock):
        result = await _dispatch(None, "compare_services", {
            "slug_a": "cheap-api", "slug_b": "premium-api",
        })
    assert result["pricing_sats"]["a"] == 10
    assert result["pricing_sats"]["b"] == 500
    assert result["protocol"]["a"] == "L402"
    assert result["protocol"]["b"] == "x402"
    assert result["avg_rating"]["a"] == 4.5
    assert result["avg_rating"]["b"] == 3.0
    assert "ai/ml" in result["categories"]["a"]
    assert "finance" in result["categories"]["b"]


@pytest.mark.asyncio
async def test_compare_services_one_not_found():
    async def mock(client, path, params=None):
        if "cheap-api" in path:
            return SERVICE_A
        return {"error": "HTTP 404", "detail": "Not found"}

    with patch("satring_mcp._fetch", side_effect=mock):
        result = await _dispatch(None, "compare_services", {
            "slug_a": "cheap-api", "slug_b": "nonexistent",
        })
    assert "error" in result
    assert "nonexistent" in result["error"]


# --- Test find_best_service ---

@pytest.mark.asyncio
async def test_find_best_service_default_strategy():
    mock = _mock_fetch({"/search": LIST_RESPONSE})
    with patch("satring_mcp._fetch", side_effect=mock):
        result = await _dispatch(None, "find_best_service", {"q": "api"})
    assert result["strategy"] == "best"
    assert result["query"] == "api"
    assert result["total_found"] == 3
    assert len(result["top_results"]) == 3  # only 3 services total


@pytest.mark.asyncio
async def test_find_best_service_cheapest():
    mock = _mock_fetch({"/search": LIST_RESPONSE})
    with patch("satring_mcp._fetch", side_effect=mock):
        result = await _dispatch(None, "find_best_service", {"q": "api", "strategy": "cheapest"})
    assert result["strategy"] == "cheapest"
    prices = [s["pricing_sats"] for s in result["top_results"]]
    assert prices == sorted(prices)


@pytest.mark.asyncio
async def test_find_best_service_top_rated():
    mock = _mock_fetch({"/search": LIST_RESPONSE})
    with patch("satring_mcp._fetch", side_effect=mock):
        result = await _dispatch(None, "find_best_service", {"q": "api", "strategy": "top-rated"})
    assert result["strategy"] == "top-rated"
    top = result["top_results"]
    # First result should be highest rated
    assert top[0]["slug"] == "cheap-api"


@pytest.mark.asyncio
async def test_find_best_service_fastest_returns_note():
    result = await _dispatch(None, "find_best_service", {"q": "api", "strategy": "fastest"})
    assert "note" in result
    assert result["strategy"] == "fastest"


@pytest.mark.asyncio
async def test_find_best_service_limits_to_five():
    many_services = [
        {**SERVICE_A, "id": i, "slug": f"svc-{i}", "pricing_sats": i * 10}
        for i in range(10)
    ]
    mock = _mock_fetch({"/search": {"services": many_services, "total": 10, "page": 1, "page_size": 20}})
    with patch("satring_mcp._fetch", side_effect=mock):
        result = await _dispatch(None, "find_best_service", {"q": "api", "strategy": "cheapest"})
    assert len(result["top_results"]) == 5


# --- Test unknown tool ---

@pytest.mark.asyncio
async def test_unknown_tool():
    result = await _dispatch(None, "nonexistent_tool", {})
    assert result == {"error": "Unknown tool: nonexistent_tool"}


# --- Test handle_call_tool (MCP layer) ---

@pytest.mark.asyncio
async def test_handle_call_tool_returns_text_content():
    with patch("satring_mcp._dispatch", new_callable=AsyncMock, return_value=CATEGORIES):
        result = await handle_call_tool("list_categories", {})
    assert len(result) == 1
    assert result[0].type == "text"
    parsed = json.loads(result[0].text)
    assert len(parsed) == 9


# --- Test _sort_services helper ---

def test_sort_cheapest():
    result = _sort_services([SERVICE_B, SERVICE_A, SERVICE_C], "cheapest")
    assert [s["pricing_sats"] for s in result] == [10, 200, 500]


def test_sort_top_rated():
    result = _sort_services([SERVICE_C, SERVICE_B, SERVICE_A], "top-rated")
    # Rated first (sorted desc), then unrated
    assert result[0]["slug"] == "cheap-api"
    assert result[1]["slug"] == "premium-api"
    assert result[2]["slug"] == "unrated-api"


def test_sort_most_reviewed():
    result = _sort_services([SERVICE_C, SERVICE_A, SERVICE_B], "most-reviewed")
    assert [s["rating_count"] for s in result] == [8, 2, 0]


def test_sort_best_composite():
    result = _sort_services([SERVICE_B, SERVICE_C, SERVICE_A], "best")
    # A: 4.5*20 + (1000-10) = 90+990 = 1080
    # B: 3.0*20 + (1000-500) = 60+500 = 560
    # C: 0*20 + (1000-200) = 0+800 = 800
    assert result[0]["slug"] == "cheap-api"
    assert result[1]["slug"] == "unrated-api"
    assert result[2]["slug"] == "premium-api"


# --- Test _compare helper ---

def test_compare_builds_side_by_side():
    result = _compare(SERVICE_A, SERVICE_B)
    assert result["name"]["a"] == "Cheap API"
    assert result["name"]["b"] == "Premium API"
    assert result["pricing_sats"]["a"] == 10
    assert result["pricing_sats"]["b"] == 500
    assert "ai/ml" in result["categories"]["a"]
    assert "finance" in result["categories"]["b"]
