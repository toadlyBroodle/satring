#!/usr/bin/env python3
"""Smoke tests against a live uvicorn server (test mode).

Run with pytest:
    pytest tests/test_endpt_smoke.py -v -s

Run standalone (same behavior, selective endpoints):
    python tests/test_endpt_smoke.py                  # Hit all endpoints
    python tests/test_endpt_smoke.py list detail      # Hit specific ones
    python tests/test_endpt_smoke.py --help           # Show available endpoint names

Starts uvicorn automatically if not already running on :8000.
Forces AUTH_ROOT_KEY=test-mode so L402 paywalls are bypassed.
"""

import json
import os
import subprocess
import socket
import sys
import time

import pytest

# Force test mode before anything imports app.config
os.environ["AUTH_ROOT_KEY"] = "test-mode"

import httpx

BASE = "http://localhost:8000"
TIMEOUT = 10

# State shared across ordered tests (populated by create)
_state = {"slug": None, "edit_token": None}


# ---------------------------------------------------------------------------
# Fixture: start uvicorn for the module, stop when done
# ---------------------------------------------------------------------------

def _server_running() -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(("localhost", 8000)) == 0


@pytest.fixture(scope="module")
def live_server():
    """Start a uvicorn server in test-mode for the duration of this module."""
    if _server_running():
        yield None  # already running externally
        return

    env = {**os.environ, "AUTH_ROOT_KEY": "test-mode"}
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--port", "8000"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=env,
    )
    for _ in range(30):
        time.sleep(0.3)
        if _server_running():
            break
    else:
        proc.kill()
        pytest.fail("uvicorn failed to start within 9 seconds")

    yield proc
    proc.terminate()
    proc.wait(timeout=5)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _pp(data):
    print(json.dumps(data, indent=2, default=str))


def _header(name, method, path):
    print(f"\n{'='*60}")
    print(f"  {name}")
    print(f"  {method} {path}")
    print(f"{'='*60}")


def _show_result(resp):
    status_color = "\033[32m" if resp.status_code < 400 else "\033[31m"
    print(f"  status: {status_color}{resp.status_code}\033[0m")
    ct = resp.headers.get("content-type", "")
    if "json" in ct:
        _pp(resp.json())
    else:
        print(resp.text[:500])


# ---------------------------------------------------------------------------
# Tests (ordered: create before patch/detail so slug+token are available)
# ---------------------------------------------------------------------------

def test_list(live_server):
    _header("List Services", "GET", "/api/v1/services")
    r = httpx.get(f"{BASE}/api/v1/services?page_size=3", timeout=TIMEOUT)
    _show_result(r)
    assert r.status_code == 200
    services = r.json().get("services", [])
    if services:
        _state["slug"] = services[0]["slug"]


def test_search(live_server):
    _header("Search", "GET", "/api/v1/search?q=satring")
    r = httpx.get(f"{BASE}/api/v1/search?q=satring", timeout=TIMEOUT)
    _show_result(r)
    assert r.status_code == 200


def test_create(live_server):
    _header("Create Service", "POST", "/api/v1/services")
    r = httpx.post(f"{BASE}/api/v1/services", json={
        "name": f"Smoke Test {int(time.time())}",
        "url": f"https://smoke-{int(time.time())}.example.com",
        "description": "Created by smoke test",
        "pricing_sats": 42,
    }, timeout=TIMEOUT)
    _show_result(r)
    assert r.status_code == 201
    data = r.json()
    _state["slug"] = data["slug"]
    _state["edit_token"] = data.get("edit_token")


def test_detail(live_server):
    slug = _state["slug"] or "satring-directory-api"
    _header("Service Detail", "GET", f"/api/v1/services/{slug}")
    r = httpx.get(f"{BASE}/api/v1/services/{slug}", timeout=TIMEOUT)
    _show_result(r)
    assert r.status_code == 200


def test_patch(live_server):
    slug = _state.get("slug")
    token = _state.get("edit_token")
    if not slug or not token:
        pytest.skip("no service created yet")
    _header("Patch Service", "PATCH", f"/api/v1/services/{slug}")
    r = httpx.patch(f"{BASE}/api/v1/services/{slug}", json={
        "description": "Updated by smoke test",
    }, headers={"X-Edit-Token": token}, timeout=TIMEOUT)
    _show_result(r)
    assert r.status_code == 200


def test_ratings(live_server):
    slug = _state["slug"] or "satring-directory-api"
    _header("List Ratings", "GET", f"/api/v1/services/{slug}/ratings")
    r = httpx.get(f"{BASE}/api/v1/services/{slug}/ratings", timeout=TIMEOUT)
    _show_result(r)
    assert r.status_code == 200


def test_rate(live_server):
    slug = _state["slug"] or "satring-directory-api"
    _header("Create Rating", "POST", f"/api/v1/services/{slug}/ratings")
    r = httpx.post(f"{BASE}/api/v1/services/{slug}/ratings", json={
        "score": 5,
        "comment": "Smoke test review",
        "reviewer_name": "SmokeBot",
    }, timeout=TIMEOUT)
    _show_result(r)
    assert r.status_code == 201


def test_analytics(live_server):
    _header("Analytics", "GET", "/api/v1/analytics")
    r = httpx.get(f"{BASE}/api/v1/analytics", timeout=TIMEOUT)
    _show_result(r)
    assert r.status_code == 200


def test_reputation(live_server):
    slug = _state["slug"] or "satring-directory-api"
    _header("Reputation", "GET", f"/api/v1/services/{slug}/reputation")
    r = httpx.get(f"{BASE}/api/v1/services/{slug}/reputation", timeout=TIMEOUT)
    _show_result(r)
    assert r.status_code == 200


def test_bulk(live_server):
    _header("Bulk Export", "GET", "/api/v1/services/bulk")
    r = httpx.get(f"{BASE}/api/v1/services/bulk", timeout=TIMEOUT)
    _show_result(r)
    assert r.status_code == 200


def test_recover_generate(live_server):
    slug = _state["slug"] or "satring-directory-api"
    _header("Recover Generate", "POST", f"/api/v1/services/{slug}/recover/generate")
    r = httpx.post(f"{BASE}/api/v1/services/{slug}/recover/generate", timeout=TIMEOUT)
    _show_result(r)
    assert r.status_code == 200


# ---------------------------------------------------------------------------
# Standalone CLI mode
# ---------------------------------------------------------------------------

# (function, method, path, description)
ENDPOINTS = {
    "list":       (test_list,             "GET",  "/api/v1/services",                  "Paginated service listing"),
    "detail":     (test_detail,           "GET",  "/api/v1/services/{slug}",            "Single service by slug"),
    "search":     (test_search,           "GET",  "/api/v1/search?q=...",               "Full-text search"),
    "create":     (test_create,           "POST", "/api/v1/services",                   "Submit new service (returns edit token)"),
    "patch":      (test_patch,            "PATCH","/api/v1/services/{slug}",            "Edit service with token"),
    "ratings":    (test_ratings,          "GET",  "/api/v1/services/{slug}/ratings",    "List ratings for a service"),
    "rate":       (test_rate,             "POST", "/api/v1/services/{slug}/ratings",    "Submit a rating"),
    "analytics":  (test_analytics,        "GET",  "/api/v1/analytics",                  "Aggregate directory stats (L402)"),
    "reputation": (test_reputation,       "GET",  "/api/v1/services/{slug}/reputation", "Reputation report (L402)"),
    "bulk":       (test_bulk,             "GET",  "/api/v1/services/bulk",              "Full JSON export (L402)"),
    "recover":    (test_recover_generate, "POST", "/api/v1/services/{slug}/recover/generate", "Domain verification challenge"),
}

ALL_ORDER = ["list", "search", "create", "detail", "patch", "ratings", "rate",
             "analytics", "reputation", "bulk", "recover"]


def show_help():
    print(__doc__)
    print("Endpoints:")
    for name in ALL_ORDER:
        _, method, path, desc = ENDPOINTS[name]
        print(f"  {name:<12} {method:<6} {path:<45} {desc}")
    print()
    print("Examples:")
    print("  python tests/test_endpt_smoke.py              # run all")
    print("  python tests/test_endpt_smoke.py list bulk    # run specific endpoints")
    print("  python tests/test_endpt_smoke.py --help       # this message")


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    flags = [a for a in sys.argv[1:] if a.startswith("-")]

    if "--help" in flags or "-h" in flags or "--list" in flags or "-l" in flags:
        show_help()
        return

    print("\n=== satring Smoke Test ===\n")

    # In standalone mode, start server and pass None as live_server arg
    if _server_running():
        print("  server already running on :8000\n")
        proc = None
    else:
        print("  starting uvicorn (test-mode)...")
        env = {**os.environ, "AUTH_ROOT_KEY": "test-mode"}
        proc = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "app.main:app", "--port", "8000"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env,
        )
        for _ in range(30):
            time.sleep(0.3)
            if _server_running():
                break
        else:
            proc.kill()
            print("  ERROR: server failed to start")
            sys.exit(1)
        print("  server started\n")

    targets = args if args else ALL_ORDER
    unknown = [t for t in targets if t not in ENDPOINTS]
    if unknown:
        print(f"Unknown endpoints: {', '.join(unknown)}")
        print("Run with --help to see available endpoints")
        sys.exit(1)

    passed = failed = 0
    for name in targets:
        try:
            ENDPOINTS[name][0](None)  # pass None for live_server
            passed += 1
        except Exception as e:
            print(f"  ERROR: {e}\n")
            failed += 1

    print(f"\n{'='*60}")
    print(f"  Done: {passed} passed, {failed} failed")
    print(f"{'='*60}")

    if proc:
        proc.terminate()
        print("  server stopped")


if __name__ == "__main__":
    main()
