"""Tests for slugify to prevent regressions in slug generation."""

import pytest

from app.utils import slugify


@pytest.mark.parametrize("input_text,expected", [
    # Dots and slashes become hyphens (not silently removed)
    ("lightningfaucet.com/.well-known/mcp.json", "lightningfaucet-com-well-known-mcp-json"),
    ("wot.klabo.world/decay/top", "wot-klabo-world-decay-top"),
    ("dashboard.strike.me", "dashboard-strike-me"),
    ("api.example.com/v1/price", "api-example-com-v1-price"),
    # Colons become hyphens
    ("eip155:8453", "eip155-8453"),
    # Protocol prefixes
    ("https://example.com/path", "https-example-com-path"),
    # Basic slugification
    ("My Cool Service", "my-cool-service"),
    ("  spaces  around  ", "spaces-around"),
    ("UPPER_CASE_name", "upper-case-name"),
    # Multiple separators collapse to single hyphen
    ("a...b///c", "a-b-c"),
    # Special characters stripped
    ("service (beta) #1", "service-beta-1"),
    # No leading/trailing hyphens
    (".leading/trailing.", "leading-trailing"),
])
def test_slugify(input_text, expected):
    assert slugify(input_text) == expected
