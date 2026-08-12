"""protoAgent RSS / Atom intake plugin."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

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
                raise ValueError("each feed row must use: Category | Name | URL or Name | URL")
            parts = [part.strip() for part in item.split("|")]
            if len(parts) == 2:
                category, name, url = "Uncategorized", *parts
            elif len(parts) == 3:
                category, name, url = parts
            else:
                raise ValueError("each feed row must use: Category | Name | URL or Name | URL")
        elif isinstance(item, dict):
            category = str(item.get("category") or "Uncategorized").strip()
            name = str(item.get("name") or "").strip()
            url = str(item.get("url") or "").strip()
        else:
            raise ValueError("each configured feed must use: Category | Name | URL or Name | URL")
        if not category or not name or not url:
            raise ValueError("each configured feed requires non-empty category, name, and url")
        key = name.casefold()
        if key in names or url in urls:
            raise ValueError("configured feed names and URLs must be unique")
        names.add(key)
        urls.add(url)
        feeds.append({"category": category, "name": name, "url": url})
    return sorted(feeds, key=lambda feed: (feed["category"].casefold(), feed["name"].casefold(), feed["url"]))


def _category_feeds(category: str) -> list[dict[str, str]]:
    wanted = category.strip().casefold()
    matches = [feed for feed in _feeds() if feed["category"].casefold() == wanted]
    if wanted and not matches:
        raise ValueError(f"unknown configured feed category: {category!r}")
    return matches


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


def _safe_article_link(value: Any) -> str:
    link = str(value or "").strip()
    try:
        return link if urlparse(link).scheme.lower() in {"http", "https"} else ""
    except ValueError:
        return ""


def _category_names() -> list[str]:
    return sorted({feed["category"] for feed in _feeds()}, key=str.casefold)


def _news_payload(category: str = "") -> dict[str, Any]:
    categories = _category_names()
    if not categories:
        return {"selected_category": "", "categories": [], "feeds": [], "entries": []}
    selected = category.strip() or categories[0]
    selected_feeds = _category_feeds(selected)
    intake = _intake()
    configured_default = max(1, min(int(_config().get("default_recent_items", 20)), 100))
    category_rows = []
    for name in categories:
        feeds = _category_feeds(name)
        urls = [feed["url"] for feed in feeds]
        category_rows.append(
            {"name": name, "feed_count": len(feeds), "entry_count": intake.count_entries(feed_urls=urls)}
        )
    entries = intake.recent_entries(
        limit=configured_default,
        feed_urls=[feed["url"] for feed in selected_feeds],
    )
    feed_by_url = {feed["url"]: feed for feed in selected_feeds}
    enriched = []
    for entry in entries:
        feed = feed_by_url.get(entry.get("feed_url", ""), {})
        enriched.append(
            {
                **entry,
                "link": _safe_article_link(entry.get("link")),
                "source": feed.get("name", "Unknown"),
                "category": selected_feeds[0]["category"],
            }
        )
    return {
        "selected_category": selected_feeds[0]["category"],
        "categories": category_rows,
        "feeds": selected_feeds,
        "entries": enriched,
    }


def _view_router():
    from fastapi import APIRouter
    from fastapi.responses import HTMLResponse

    router = APIRouter()

    @router.get("/view")
    async def _view():
        return HTMLResponse(Path(__file__).with_name("news_view.html").read_text(encoding="utf-8"))

    return router


def _data_router():
    from fastapi import APIRouter, HTTPException

    router = APIRouter()

    @router.get("/news")
    async def _news(category: str = ""):
        try:
            return await asyncio.to_thread(_news_payload, category)
        except (TypeError, ValueError, RuntimeError) as exc:
            raise HTTPException(status_code=400, detail=_public_error(exc)) from exc

    @router.post("/refresh-category")
    async def _refresh_category(payload: dict[str, Any]):
        category = str(payload.get("category") or "").strip()
        try:
            feeds = _category_feeds(category)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=_public_error(exc)) from exc
        results = []
        for feed in feeds:
            try:
                result = await asyncio.to_thread(_intake().refresh, feed["url"])
                results.append({"name": feed["name"], **result})
            except (FeedIntakeError, RuntimeError) as exc:
                results.append({"name": feed["name"], "status": "error", "error": _public_error(exc)})
        return {
            "category": feeds[0]["category"],
            "refreshed": len(feeds),
            "inserted": sum(int(result.get("inserted", 0)) for result in results),
            "failed": sum(result.get("status") == "error" for result in results),
            "results": results,
        }

    return router


@tool
def rss_list_feeds(category: str = "") -> str:
    """List operator-configured RSS/Atom feeds by stable name and source URL."""
    try:
        return _json(_category_feeds(category) if category.strip() else _feeds())
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
def rss_recent_entries(limit: int | None = None, name: str = "", category: str = "") -> str:
    """Return recent normalized feed entries, optionally restricted to one configured feed."""
    try:
        configured_default = max(1, min(int(_config().get("default_recent_items", 20)), 100))
        bounded = configured_default if limit is None else max(1, min(int(limit), 100))
        if name.strip() and category.strip():
            raise ValueError("filter recent entries by feed name or category, not both")
        feed_url = _feed(name)["url"] if name.strip() else ""
        feed_urls = [feed["url"] for feed in _category_feeds(category)] if category.strip() else None
        return _json(_intake().recent_entries(limit=bounded, feed_url=feed_url, feed_urls=feed_urls))
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
    """Register four bounded tools plus a user-driven News view; no background surface is created."""
    global _config_provider
    _config_provider = registry.live_config
    registry.register_tools([rss_list_feeds, rss_refresh_feed, rss_recent_entries, rss_feed_status])
    registry.register_router(_view_router(), prefix="/plugins/rss_atom")
    registry.register_router(_data_router(), prefix="/api/plugins/rss_atom")
