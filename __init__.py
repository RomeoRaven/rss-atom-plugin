"""protoAgent RSS / Atom intake plugin."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Callable

from langchain_core.tools import tool

if __package__:
    from .feed_intake import FeedIntake, FeedIntakeError, HttpxTransport, ProtoAgentEgressPolicy
else:  # Standalone pytest imports the root entry as top-level ``__init__``.
    from feed_intake import FeedIntake, FeedIntakeError, HttpxTransport, ProtoAgentEgressPolicy


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
        if isinstance(item, str):
            if "|" not in item:
                raise ValueError("each feed row must use: Name | URL")
            name, url = (part.strip() for part in item.split("|", 1))
        elif isinstance(item, dict):
            name = str(item.get("name") or "").strip()
            url = str(item.get("url") or "").strip()
        else:
            raise ValueError("each configured feed must use: Name | URL")
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
        max_feed_size_kib = max(1, min(int(cfg.get("max_feed_size_kib", 256)), 2048))
        timeout_seconds = max(1.0, min(float(cfg.get("timeout_seconds", 15)), 60.0))
        max_entries = max(1, min(int(cfg.get("max_entries_per_feed", 1000)), 10000))
        max_items = max(1, min(int(cfg.get("max_items_per_refresh", 100)), 1000))
    except (TypeError, ValueError) as exc:
        raise ValueError("RSS/Atom numeric settings must contain valid numbers") from exc
    try:
        from security import egress
    except ImportError as exc:
        raise RuntimeError("protoAgent security.egress is required") from exc
    egress_policy = ProtoAgentEgressPolicy(
        Path(egress.__file__).resolve().parents[1],
        egress.allowed_hosts(),
    )
    return FeedIntake(
        _data_dir() / "feeds.db",
        _transport,
        check_url=egress_policy,
        max_bytes=max_feed_size_kib * 1024,
        timeout_seconds=timeout_seconds,
        max_entries_per_feed=max_entries,
        max_items_per_refresh=max_items,
    )


def _json(value: Any) -> str:
    return json.dumps(value, indent=2, sort_keys=True)


def _public_error(exc: Exception) -> str:
    return str(exc)[:500]


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
        return _json({"status": "error", "error": _public_error(exc), "name": name})


@tool
def rss_recent_entries(limit: int | None = None, name: str = "") -> str:
    """Return recent normalized feed entries, optionally restricted to one configured feed."""
    try:
        configured_default = max(1, min(int(_config().get("default_recent_items", 20)), 100))
        bounded = configured_default if limit is None else max(1, min(int(limit), 100))
        feed_url = _feed(name)["url"] if name.strip() else ""
        return _json(_intake().recent_entries(limit=bounded, feed_url=feed_url))
    except (TypeError, ValueError, RuntimeError) as exc:
        return _json({"status": "error", "error": _public_error(exc)})


@tool
def rss_feed_status(name: str) -> str:
    """Report the latest refresh status for one configured RSS/Atom feed."""
    try:
        feed = _feed(name)
        return _json({"name": feed["name"], "url": feed["url"], **_intake().feed_status(feed["url"])})
    except (ValueError, RuntimeError) as exc:
        return _json({"status": "error", "error": _public_error(exc), "name": name})


def register(registry) -> None:
    """Register four bounded RSS/Atom intake tools; no background surface is created."""
    global _config_provider
    _config_provider = registry.live_config
    registry.register_tools([rss_list_feeds, rss_refresh_feed, rss_recent_entries, rss_feed_status])
