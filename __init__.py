"""protoAgent RSS / Atom intake plugin."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Callable

from langchain_core.tools import tool

if __package__:
    from .feed_intake import FeedIntake, FeedIntakeError, HttpxTransport
else:  # Standalone pytest imports the root entry as top-level ``__init__``.
    from feed_intake import FeedIntake, FeedIntakeError, HttpxTransport


def _empty_config() -> dict[str, Any]:
    return {}


_config_provider: Callable[[], dict[str, Any]] = _empty_config
_transport = HttpxTransport()


def _data_dir() -> Path:
    override = os.environ.get("RSS_ATOM_DATA_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    home = Path(os.environ.get("PROTOAGENT_HOME") or (Path.home() / ".protoagent"))
    return home / "rss_atom"


def _config() -> dict[str, Any]:
    return dict(_config_provider() or {})


def _feeds() -> list[dict[str, str]]:
    raw = _config().get("feeds", [])
    if not isinstance(raw, list):
        raise ValueError("rss_atom.feeds must be a list")
    feeds: list[dict[str, str]] = []
    names: set[str] = set()
    urls: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("each configured feed must be an object")
        name = str(item.get("name") or "").strip()
        url = str(item.get("url") or "").strip()
        if not name or not url:
            raise ValueError("each configured feed requires non-empty name and url")
        key = name.casefold()
        if key in names or url in urls:
            raise ValueError("configured feed names and URLs must be unique")
        names.add(key)
        urls.add(url)
        feeds.append({"name": name, "url": url})
    return sorted(feeds, key=lambda feed: (feed["name"].casefold(), feed["url"]))


def _feed(name: str) -> dict[str, str]:
    wanted = name.strip().casefold()
    matches = [feed for feed in _feeds() if feed["name"].casefold() == wanted]
    if not matches:
        raise ValueError(f"unknown configured feed: {name!r}")
    return matches[0]


def _intake() -> FeedIntake:
    cfg = _config()
    try:
        max_bytes = max(1024, min(int(cfg.get("max_bytes", 262144)), 2 * 1024 * 1024))
        timeout_seconds = max(1.0, min(float(cfg.get("timeout_seconds", 15)), 60.0))
        max_entries = max(1, min(int(cfg.get("max_entries_per_feed", 1000)), 10000))
    except (TypeError, ValueError) as exc:
        raise ValueError("max_bytes and timeout_seconds must be numeric") from exc
    try:
        from security.egress import check_url
    except ImportError as exc:
        raise RuntimeError("protoAgent security.egress is required") from exc
    return FeedIntake(
        _data_dir() / "feeds.db",
        _transport,
        check_url=check_url,
        max_bytes=max_bytes,
        timeout_seconds=timeout_seconds,
        max_entries_per_feed=max_entries,
    )


def _json(value: Any) -> str:
    return json.dumps(value, indent=2, sort_keys=True)


@tool
def rss_list_feeds() -> str:
    """List operator-configured RSS/Atom feeds by stable name and source URL."""
    try:
        return _json(_feeds())
    except ValueError as exc:
        return _json({"status": "invalid_configuration", "error": str(exc)})


@tool
def rss_refresh_feed(name: str) -> str:
    """Safely refresh one configured RSS/Atom feed by name and persist new entries."""
    try:
        feed = _feed(name)
        return _json({"name": feed["name"], "url": feed["url"], **_intake().refresh(feed["url"])})
    except (ValueError, FeedIntakeError, RuntimeError) as exc:
        return _json({"status": "error", "error": str(exc), "name": name})


@tool
def rss_recent_entries(limit: int = 20, name: str = "") -> str:
    """Return recent normalized feed entries, optionally restricted to one configured feed."""
    try:
        bounded = max(1, min(int(limit), 100))
        feed_url = _feed(name)["url"] if name.strip() else ""
        return _json(_intake().recent_entries(limit=bounded, feed_url=feed_url))
    except (TypeError, ValueError, RuntimeError) as exc:
        return _json({"status": "error", "error": str(exc)})


@tool
def rss_feed_status(name: str) -> str:
    """Report the latest refresh status for one configured RSS/Atom feed."""
    try:
        feed = _feed(name)
        return _json({"name": feed["name"], "url": feed["url"], **_intake().feed_status(feed["url"])})
    except (ValueError, RuntimeError) as exc:
        return _json({"status": "error", "error": str(exc), "name": name})


def register(registry) -> None:
    """Register four bounded RSS/Atom intake tools; no background surface is created."""
    global _config_provider
    _config_provider = registry.live_config
    registry.register_tools([rss_list_feeds, rss_refresh_feed, rss_recent_entries, rss_feed_status])
