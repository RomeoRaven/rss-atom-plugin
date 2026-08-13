from __future__ import annotations

import argparse
import ipaddress
import json
import socket
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, cast
from urllib.parse import urljoin, urlsplit, urlunsplit

import feedparser
import httpx

MAX_BYTES = 2 * 1024 * 1024
MAX_REDIRECTS = 3
USER_AGENT = "rss-atom-plugin-catalog-validator/0.9 (+https://github.com/RomeoRaven/rss-atom-plugin)"


def _public_host(url: str) -> tuple[bool, str]:
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        return False, "catalogue URL must be credential-free HTTPS"
    try:
        addresses = {
            item[4][0] for item in socket.getaddrinfo(parsed.hostname, parsed.port or 443, type=socket.SOCK_STREAM)
        }
    except OSError as exc:
        return False, f"DNS failed: {type(exc).__name__}"
    for raw in addresses:
        ip = ipaddress.ip_address(raw)
        if not ip.is_global:
            return False, f"non-public address: {ip}"
    return True, ""


def _https_candidate(url: str) -> str:
    parsed = urlsplit(url)
    return urlunsplit(("https", parsed.netloc, parsed.path, parsed.query, ""))


def validate(row: dict) -> dict:
    configured = str(row["url"])
    requested = _https_candidate(configured)
    out = {
        "name": row["name"],
        "category": row["category"],
        "configured_url": configured,
        "requested_url": requested,
        "final_url": "",
        "redirects": [],
        "http_status": None,
        "content_type": "",
        "bytes": 0,
        "feed_version": "",
        "entry_count": 0,
        "feed_title": "",
        "bozo": False,
        "parse_error": "",
        "transport_error": "",
        "https_safe": False,
        "duration_ms": 0,
    }
    started = time.monotonic()
    safe, reason = _public_host(requested)
    if not safe:
        out["transport_error"] = reason
        return out
    current = requested
    try:
        with httpx.Client(
            timeout=httpx.Timeout(20.0, connect=8.0),
            follow_redirects=False,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "application/atom+xml, application/rss+xml, application/xml, text/xml;q=0.9, */*;q=0.2",
            },
        ) as client:
            for _ in range(MAX_REDIRECTS + 1):
                safe, reason = _public_host(current)
                if not safe:
                    raise RuntimeError(reason)
                with client.stream("GET", current) as response:
                    out["http_status"] = response.status_code
                    out["content_type"] = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                    if response.status_code in {301, 302, 303, 307, 308}:
                        location = response.headers.get("location")
                        if not location:
                            raise RuntimeError("redirect missing Location")
                        target = urljoin(current, location)
                        if urlsplit(target).scheme != "https":
                            raise RuntimeError("HTTPS downgrade refused")
                        out["redirects"].append({"status": response.status_code, "from": current, "to": target})
                        current = target
                        continue
                    body = bytearray()
                    for chunk in response.iter_bytes():
                        body.extend(chunk)
                        if len(body) > MAX_BYTES:
                            raise RuntimeError("decompressed response exceeds 2 MiB validation ceiling")
                    out["bytes"] = len(body)
                    out["final_url"] = str(response.url)
                    out["https_safe"] = urlsplit(out["final_url"]).scheme == "https"
                    if response.status_code != 200:
                        break
                    parsed = feedparser.parse(bytes(body))
                    out["feed_version"] = str(parsed.get("version") or "")
                    out["entry_count"] = len(parsed.entries)
                    feed_meta = cast(dict[str, Any], parsed.get("feed") or {})
                    out["feed_title"] = str(feed_meta.get("title") or "")[:200]
                    out["bozo"] = bool(parsed.get("bozo"))
                    if parsed.get("bozo_exception"):
                        out["parse_error"] = f"{type(parsed.bozo_exception).__name__}: {parsed.bozo_exception}"[:300]
                    break
            else:
                raise RuntimeError("too many redirects")
    except Exception as exc:
        out["transport_error"] = f"{type(exc).__name__}: {exc}"[:400]
    out["duration_ms"] = round((time.monotonic() - started) * 1000)
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--workers", type=int, default=6)
    args = parser.parse_args()
    rows = json.loads(args.input.read_text(encoding="utf-8"))
    results = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(validate, row): row for row in rows}
        for future in as_completed(futures):
            result = future.result()
            results.append(result)
            print(
                f"{result['name']}: HTTP {result['http_status']} {result['feed_version']} entries={result['entry_count']} error={result['transport_error'] or result['parse_error']}",
                flush=True,
            )
    order = {(row["category"], row["name"], row["url"]): index for index, row in enumerate(rows)}
    results.sort(key=lambda item: order[(item["category"], item["name"], item["configured_url"])])
    final_urls: dict[str, list[str]] = {}
    for result in results:
        if result["final_url"]:
            final_urls.setdefault(result["final_url"], []).append(result["name"])
    payload = {
        "schema_version": 1,
        "validated_at": datetime.now(timezone.utc).isoformat(),
        "candidate_count": len(rows),
        "validator": {"max_bytes": MAX_BYTES, "max_redirects": MAX_REDIRECTS, "https_required": True},
        "duplicate_final_urls": {url: names for url, names in final_urls.items() if len(names) > 1},
        "results": results,
    }
    args.output.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return 0 if len(results) == len(rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
