"""On-demand audience analytics from nginx access logs.

Parses nginx logs for specific service slugs, extracts unique IPs and
user-agents, and resolves IPs to geo data via MaxMind GeoLite2 local database.

Called only during paid analytics requests, not continuously.
"""

import logging
import os
import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from typing import Optional

from app.config import settings
from app.usage import classify_agent

logger = logging.getLogger("satring.audience")

# Nginx log parsing (same patterns as deploy/monitor/traffic_monitor.py)
_LOG_DATETIME_FMT = "%d/%b/%Y:%H:%M:%S %z"
_IP_RE = re.compile(r"^(\S+)")
_UA_RE = re.compile(r'"([^"]*)"$')
_REQUEST_RE = re.compile(r'"(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)\s+([^\s"]+)')
_STATUS_RE = re.compile(r'"\s+(\d{3})\s')

# Slug extraction from paths
_SLUG_PATH_RE = re.compile(r"^/(?:api/v1/)?services/([^/?]+)")

_NGINX_LOG_PATH = os.getenv(
    "NGINX_LOG_PATH", "/var/log/nginx/satring.com_access.log"
)


def _parse_log_line(line: str) -> Optional[dict]:
    """Parse a single nginx access log line. Returns dict or None."""
    ip_match = _IP_RE.match(line)
    if not ip_match:
        return None
    req_match = _REQUEST_RE.search(line)
    if not req_match:
        return None

    t_start = line.find("[")
    t_end = line.find("]", t_start + 1)
    if t_start == -1 or t_end == -1:
        return None
    try:
        timestamp = datetime.strptime(line[t_start + 1 : t_end], _LOG_DATETIME_FMT)
    except ValueError:
        return None

    status_match = _STATUS_RE.search(line)
    status = int(status_match.group(1)) if status_match else 0
    ua_match = _UA_RE.search(line)

    return {
        "ip": ip_match.group(1),
        "timestamp": timestamp,
        "path": req_match.group(2).split("?")[0],
        "status": status,
        "user_agent": ua_match.group(1) if ua_match else "",
    }


def extract_audience_for_slugs(
    slugs: list[str], days: int = 30
) -> dict:
    """Parse nginx logs and extract audience data for specific slugs.

    Returns:
        {
            "unique_ips": set of IPs,
            "agent_breakdown": Counter of agent_class -> count,
            "source_breakdown": {"api": int, "web": int},
            "ip_hit_counts": Counter of IP -> count,
        }
    """
    slug_set = set(slugs)
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    unique_ips: set[str] = set()
    agent_counts: Counter = Counter()
    source_counts: Counter = Counter()
    ip_hits: Counter = Counter()

    if not os.path.exists(_NGINX_LOG_PATH):
        logger.warning(f"Nginx log not found: {_NGINX_LOG_PATH}")
        return {
            "unique_ips": unique_ips,
            "agent_breakdown": agent_counts,
            "source_breakdown": dict(source_counts),
            "ip_hit_counts": ip_hits,
        }

    with open(_NGINX_LOG_PATH, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            parsed = _parse_log_line(line)
            if not parsed:
                continue
            if parsed["timestamp"] < cutoff:
                continue
            if parsed["status"] >= 400:
                continue

            # Check if this request hits one of our slugs
            slug_match = _SLUG_PATH_RE.match(parsed["path"])
            if not slug_match:
                continue
            slug = slug_match.group(1)
            if slug not in slug_set:
                continue

            ip = parsed["ip"]
            unique_ips.add(ip)
            ip_hits[ip] += 1
            agent_counts[classify_agent(parsed["user_agent"])] += 1

            if parsed["path"].startswith("/api/"):
                source_counts["api"] += 1
            else:
                source_counts["web"] += 1

    return {
        "unique_ips": unique_ips,
        "agent_breakdown": dict(agent_counts),
        "source_breakdown": dict(source_counts),
        "ip_hit_counts": ip_hits,
    }


_GEOLITE2_PATH = os.getenv(
    "GEOLITE2_CITY_PATH",
    os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "GeoLite2-City.mmdb"),
)


def geolocate_ips(ips: list[str], max_ips: int = 200) -> list[dict]:
    """Resolve IPs to geo data via MaxMind GeoLite2 local database.

    Uses the offline .mmdb file for fast lookups with no external API calls.
    Called on-demand per paid analytics request only.
    """
    if not ips:
        return []

    ips = ips[:max_ips]

    if not os.path.exists(_GEOLITE2_PATH):
        logger.warning(f"GeoLite2 database not found: {_GEOLITE2_PATH}")
        return []

    try:
        import geoip2.database
    except ImportError:
        logger.warning("geoip2 package not installed")
        return []

    results = []
    with geoip2.database.Reader(_GEOLITE2_PATH) as reader:
        for ip in ips:
            try:
                r = reader.city(ip)
                results.append({
                    "query": ip,
                    "country": r.country.name or "Unknown",
                    "regionName": (r.subdivisions.most_specific.name
                                   if r.subdivisions else "Unknown"),
                    "city": r.city.name or "Unknown",
                })
            except Exception:
                results.append({"query": ip, "status": "fail"})

    return results


# Keep async signature for backward compatibility with callers using await
async def batch_geolocate(ips: list[str], max_ips: int = 200) -> list[dict]:
    """Async wrapper around geolocate_ips for compatibility."""
    return geolocate_ips(ips, max_ips)


def build_geo_summary(geo_results: list[dict]) -> dict:
    """Aggregate geo results into country/region/city distributions."""
    countries: Counter = Counter()
    regions: Counter = Counter()
    cities: Counter = Counter()

    for r in geo_results:
        if r.get("status") == "fail":
            continue
        countries[r.get("country", "Unknown")] += 1
        regions[r.get("regionName", "Unknown")] += 1
        cities[r.get("city", "Unknown")] += 1

    return {
        "total_resolved": len([r for r in geo_results if r.get("status") != "fail"]),
        "countries": [{"name": k, "count": v} for k, v in countries.most_common(20)],
        "regions": [{"name": k, "count": v} for k, v in regions.most_common(20)],
        "cities": [{"name": k, "count": v} for k, v in cities.most_common(20)],
    }
