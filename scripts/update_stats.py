#!/usr/bin/env python3
"""Refresh repository traffic and APK GitHub Release download statistics."""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
OWNER = "sharifvau"
REPO = "Arifa-Ai-Agent.apk"
API = f"https://api.github.com/repos/{OWNER}/{REPO}"
README = ROOT / "README.md"
STATS_FILE = ROOT / "stats.json"
START_MARKER = "<!-- REPO_STATS_START -->"
END_MARKER = "<!-- REPO_STATS_END -->"


def get_json(url: str, token: str | None = None):
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "arifa-repository-stats",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = Request(url, headers=headers)
    with urlopen(request, timeout=30) as response:
        return json.load(response)


def safe_get(url: str, token: str | None, errors: list[str]):
    try:
        return get_json(url, token)
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
        status = getattr(exc, "code", None)
        endpoint = url.split("/traffic/")[-1] if "/traffic/" in url else "releases"
        errors.append(f"{endpoint} request failed" + (f" (HTTP {status})" if status else ""))
        return None


def get_releases(token: str | None, errors: list[str]) -> list[dict]:
    releases: list[dict] = []
    page = 1
    while True:
        result = safe_get(f"{API}/releases?per_page=100&page={page}", token, errors)
        if not isinstance(result, list):
            break
        releases.extend(result)
        if len(result) < 100:
            break
        page += 1
    return releases


def format_metric(metric: dict | None) -> str:
    if metric is None:
        return "Not available"
    return f"{metric['count']:,} total · {metric['uniques']:,} unique"


def render_readme(stats: dict) -> None:
    readme = README.read_text(encoding="utf-8")
    if START_MARKER not in readme or END_MARKER not in readme:
        raise RuntimeError("README statistics markers are missing")

    releases = stats["apk_downloads"]["releases"]
    if releases:
        latest = releases[0]
        latest_text = f"{latest['tag']} · {latest['downloads']:,} downloads"
    else:
        latest_text = "No APK release found"

    table = "\n".join(
        [
            START_MARKER,
            "| Metric | Count |",
            "| --- | ---: |",
            f"| Repository views (last 14 days) | {format_metric(stats['traffic']['views'])} |",
            f"| Repository clones (last 14 days) | {format_metric(stats['traffic']['clones'])} |",
            f"| APK downloads (all GitHub Releases) | {stats['apk_downloads']['total']:,} |",
            f"| Latest APK release | {latest_text} |",
            f"| Traffic data updated (UTC) | {stats['traffic_updated_at'] or 'Not available'} |",
            f"| Stats refreshed (UTC) | {stats['updated_at']} |",
            END_MARKER,
        ]
    )
    before, remainder = readme.split(START_MARKER, 1)
    _, after = remainder.split(END_MARKER, 1)
    README.write_text(before + table + after, encoding="utf-8")


def main() -> int:
    errors: list[str] = []
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    previous: dict = {}
    try:
        previous = json.loads(STATS_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        pass

    old_traffic = previous.get("traffic", {})
    traffic: dict[str, dict | None] = {
        "views": old_traffic.get("views"),
        "clones": old_traffic.get("clones"),
    }
    traffic_updated_at = previous.get("traffic_updated_at")
    referrers = previous.get("popular_referrers", [])
    traffic_token = os.environ.get("TRAFFIC_TOKEN", "").strip() or None
    api_token = os.environ.get("GITHUB_TOKEN", "").strip() or None

    if traffic_token:
        fresh_traffic: dict[str, dict | None] = {"views": None, "clones": None}
        for key in ("views", "clones"):
            payload = safe_get(f"{API}/traffic/{key}", traffic_token, errors)
            if isinstance(payload, dict):
                fresh_traffic[key] = {
                    "count": int(payload.get("count", 0)),
                    "uniques": int(payload.get("uniques", 0)),
                    "daily": payload.get(key, []),
                }
        if fresh_traffic["views"] is not None or fresh_traffic["clones"] is not None:
            for key, value in fresh_traffic.items():
                if value is not None:
                    traffic[key] = value
            traffic_updated_at = now

        payload = safe_get(f"{API}/traffic/popular/referrers", traffic_token, errors)
        if isinstance(payload, list):
            referrers = [
                {
                    "referrer": item.get("referrer", ""),
                    "count": int(item.get("count", 0)),
                    "uniques": int(item.get("uniques", 0)),
                }
                for item in payload[:5]
            ]
    else:
        errors.append("TRAFFIC_TOKEN is not configured; traffic data was not refreshed")

    apk_releases = []
    for release in get_releases(api_token, errors):
        apk_assets = [asset for asset in release.get("assets", []) if asset.get("name", "").lower().endswith(".apk")]
        if not apk_assets:
            continue
        apk_releases.append(
            {
                "tag": release.get("tag_name", ""),
                "name": release.get("name") or release.get("tag_name", ""),
                "published_at": release.get("published_at"),
                "downloads": sum(int(asset.get("download_count", 0)) for asset in apk_assets),
                "assets": [
                    {
                        "name": asset.get("name", ""),
                        "downloads": int(asset.get("download_count", 0)),
                        "size_bytes": int(asset.get("size", 0)),
                        "url": asset.get("browser_download_url", ""),
                    }
                    for asset in apk_assets
                ],
            }
        )

    stats = {
        "updated_at": now,
        "traffic_updated_at": traffic_updated_at,
        "traffic_window_days": 14,
        "traffic": traffic,
        "apk_downloads": {
            "count_type": "GitHub Release APK asset downloads; not unique people",
            "total": sum(item["downloads"] for item in apk_releases),
            "releases": apk_releases,
        },
        "popular_referrers": referrers,
        "errors": errors,
    }
    STATS_FILE.write_text(json.dumps(stats, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    render_readme(stats)

    if errors:
        print("Statistics refreshed with limitations: " + "; ".join(errors), file=sys.stderr)
    else:
        print("Statistics refreshed successfully.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
