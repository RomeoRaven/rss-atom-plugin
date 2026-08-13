"""protoAgent RSS / Atom intake plugin."""

from __future__ import annotations

import asyncio
import html
import json
import os
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from langchain_core.tools import tool
from markdown_it import MarkdownIt

if __package__:
    from .feed_intake import FeedIntake, FeedIntakeError, HttpxTransport, ProtoAgentEgressPolicy
else:  # Standalone pytest imports the root entry as top-level ``__init__``.
    from feed_intake import FeedIntake, FeedIntakeError, HttpxTransport, ProtoAgentEgressPolicy


def _empty_config() -> dict[str, Any]:
    return {}


_config_provider: Callable[[], dict[str, Any]] = _empty_config
_transport = HttpxTransport()
_markdown: Any = MarkdownIt("commonmark", {"html": False})


def _help_link_open(tokens, index, options, env):
    token = tokens[index]
    href = token.attrGet("href") or ""
    scheme = urlparse(href).scheme.lower()
    if scheme in {"http", "https"}:
        token.attrSet("target", "_blank")
        token.attrSet("rel", "noopener noreferrer")
    elif scheme and scheme != "mailto":
        token.attrSet("href", "#")
    return _markdown.renderer.renderToken(tokens, index, options, env)


_markdown.renderer.rules["link_open"] = _help_link_open


def _data_dir() -> Path:
    override = os.environ.get("RSS_ATOM_DATA_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    home = Path(os.environ.get("PROTOAGENT_HOME") or (Path.home() / ".protoagent"))
    return home / "rss_atom"


def _config() -> dict[str, Any]:
    return dict(_config_provider() or {})


def _feed_override(value: Any, *, label: str, maximum: int) -> int | None:
    if value is None or str(value).strip() == "":
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"feed {label} must be a whole number") from exc
    if not 1 <= parsed <= maximum:
        raise ValueError(f"feed {label} must be 1 through {maximum}")
    return parsed


def _feeds() -> list[dict[str, Any]]:
    raw = _config().get("feeds", [])
    if not isinstance(raw, list):
        raise ValueError("rss_atom.feeds must be a list")
    feeds: list[dict[str, Any]] = []
    names: set[str] = set()
    urls: set[str] = set()
    for item in raw:
        if isinstance(item, str):
            if "|" not in item:
                raise ValueError(
                    "each feed row must use: Category | Name | URL | Max size KiB | Items per refresh or Name | URL"
                )
            parts = [part.strip() for part in item.split("|")]
            if len(parts) == 2:
                category, name, url = "Uncategorized", *parts
                max_feed_size_kib = max_items_per_refresh = None
            elif len(parts) == 3:
                category, name, url = parts
                max_feed_size_kib = max_items_per_refresh = None
            elif len(parts) == 5:
                category, name, url, raw_size, raw_items = parts
                max_feed_size_kib = _feed_override(raw_size, label="size override in KiB", maximum=2048)
                max_items_per_refresh = _feed_override(raw_items, label="items-per-refresh override", maximum=100)
            else:
                raise ValueError(
                    "each feed row must use: Category | Name | URL | Max size KiB | Items per refresh or Name | URL"
                )
        elif isinstance(item, dict):
            category = str(item.get("category") or "Uncategorized").strip()
            name = str(item.get("name") or "").strip()
            url = str(item.get("url") or "").strip()
            max_feed_size_kib = _feed_override(
                item.get("max_feed_size_kib"), label="size override in KiB", maximum=2048
            )
            max_items_per_refresh = _feed_override(
                item.get("max_items_per_refresh"), label="items-per-refresh override", maximum=100
            )
        else:
            raise ValueError(
                "each configured feed must use: Category | Name | URL | Max size KiB | Items per refresh or Name | URL"
            )
        if not category or not name or not url:
            raise ValueError("each configured feed requires non-empty category, name, and url")
        key = name.casefold()
        if key in names or url in urls:
            raise ValueError("configured feed names and URLs must be unique")
        names.add(key)
        urls.add(url)
        feed: dict[str, Any] = {"category": category, "name": name, "url": url}
        if max_feed_size_kib is not None:
            feed["max_feed_size_kib"] = max_feed_size_kib
        if max_items_per_refresh is not None:
            feed["max_items_per_refresh"] = max_items_per_refresh
        feeds.append(feed)
    return sorted(feeds, key=lambda feed: (feed["category"].casefold(), feed["name"].casefold(), feed["url"]))


def _category_feeds(category: str) -> list[dict[str, Any]]:
    wanted = category.strip().casefold()
    matches = [feed for feed in _feeds() if feed["category"].casefold() == wanted]
    if wanted and not matches:
        raise ValueError(f"unknown configured feed category: {category!r}")
    return matches


def _feed(name: str) -> dict[str, Any]:
    wanted = name.strip().casefold()
    matches = [feed for feed in _feeds() if feed["name"].casefold() == wanted]
    if not matches:
        raise ValueError(f"unknown configured feed: {name!r}")
    return matches[0]


def _intake(feed: dict[str, Any] | None = None) -> FeedIntake:
    cfg = _config()
    try:
        if "max_feed_size_kib" in cfg:
            default_max_bytes = max(1024, min(int(cfg["max_feed_size_kib"]) * 1024, 2 * 1024 * 1024))
        else:
            default_max_bytes = max(1024, min(int(cfg.get("max_bytes", 262144)), 2 * 1024 * 1024))
        timeout_seconds = max(1.0, min(float(cfg.get("timeout_seconds", 15)), 60.0))
        max_entries = max(1, min(int(cfg.get("max_entries_per_feed", 1000)), 10000))
        default_items = max(1, min(int(cfg.get("max_items_per_refresh", 100)), 100))
    except (TypeError, ValueError) as exc:
        raise ValueError("RSS/Atom numeric settings must contain valid numbers") from exc
    max_bytes = (
        int((feed or {}).get("max_feed_size_kib", 0)) * 1024
        if (feed or {}).get("max_feed_size_kib")
        else default_max_bytes
    )
    max_items = int((feed or {}).get("max_items_per_refresh", default_items))
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
        max_bytes=max_bytes,
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


def _news_payload(category: str = "", source: str = "") -> dict[str, Any]:
    categories = _category_names()
    if not categories:
        return {"selected_category": "", "categories": [], "feeds": [], "entries": []}
    selected = category.strip() or categories[0]
    selected_feeds = _category_feeds(selected)
    selected_source = source.strip()
    source_feed = None
    if selected_source:
        source_feed = next(
            (feed for feed in selected_feeds if feed["name"].casefold() == selected_source.casefold()),
            None,
        )
        if source_feed is None:
            raise ValueError(f"unknown feed {source!r} in configured category {selected!r}")
        selected_source = source_feed["name"]
    intake = _intake()
    cfg = _config()
    if "max_feed_size_kib" in cfg:
        default_feed_size_kib = max(1, min(int(cfg["max_feed_size_kib"]), 2048))
    else:
        default_feed_size_kib = max(1, min((int(cfg.get("max_bytes", 262144)) + 1023) // 1024, 2048))
    default_items = max(1, min(int(cfg.get("max_items_per_refresh", 100)), 100))
    configured_default = max(1, min(int(_config().get("default_recent_items", 20)), 100))
    category_rows = []
    for name in categories:
        feeds = _category_feeds(name)
        urls = [feed["url"] for feed in feeds]
        category_rows.append(
            {"name": name, "feed_count": len(feeds), "entry_count": intake.count_entries(feed_urls=urls)}
        )
    if source_feed:
        entries = intake.recent_entries_with_reader(limit=configured_default, feed_url=source_feed["url"])
    else:
        entries = intake.recent_entries_with_reader(
            limit=configured_default,
            feed_urls=[feed["url"] for feed in selected_feeds],
        )
    enriched_feeds = []
    for feed in selected_feeds:
        enriched_feeds.append(
            {
                **feed,
                "effective_max_feed_size_kib": int(feed.get("max_feed_size_kib", default_feed_size_kib)),
                "effective_max_items_per_refresh": int(feed.get("max_items_per_refresh", default_items)),
                "stored_count": intake.count_entries(feed_urls=[feed["url"]]),
                "health": intake.feed_status(feed["url"]),
            }
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
        "selected_source": selected_source,
        "categories": category_rows,
        "feeds": enriched_feeds,
        "entries": enriched,
    }


def _view_router():
    from fastapi import APIRouter, HTTPException
    from fastapi.responses import HTMLResponse

    router = APIRouter()

    @router.get("/view")
    async def _view():
        return HTMLResponse(Path(__file__).with_name("news_view.html").read_text(encoding="utf-8"))

    @router.get("/help")
    async def _help():
        try:
            readme = Path(__file__).with_name("README.md").read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise HTTPException(status_code=404, detail="Plugin help is not bundled") from exc
        rendered = _markdown.render(readme)
        title = "RSS / Atom Intake help"
        return HTMLResponse(
            '<!doctype html><html lang="en"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">'
            f"<title>{html.escape(title)}</title>"
            '<script>window.__base=location.pathname.split("/plugins/")[0];'
            "const kitCss=document.createElement('link');kitCss.rel='stylesheet';"
            "kitCss.href=window.__base+'/_ds/plugin-kit.css';document.head.appendChild(kitCss);</script>"
            "<style>:root{color-scheme:dark}*{box-sizing:border-box}body{margin:0;min-height:100vh;"
            "background:var(--pl-color-bg,#0b0e12);color:var(--pl-color-fg,#eef1f5);"
            "font:15px/1.65 ui-sans-serif,system-ui,sans-serif}.help-shell{width:min(860px,100%);margin:0 auto;"
            "padding:clamp(24px,5vw,58px) clamp(18px,5vw,52px) 72px}.help-nav{display:flex;justify-content:space-between;"
            "align-items:center;gap:16px;padding-bottom:18px;border-bottom:1px solid var(--pl-color-border,#29313a)}"
            ".help-nav a,a{color:var(--pl-color-brand,#9f8cff)}.markdown h1{font-size:clamp(28px,5vw,42px);line-height:1.1;"
            "letter-spacing:-.035em}.markdown h2{margin-top:2em;padding-top:.5em;border-top:1px solid var(--pl-color-border,#29313a)}"
            ".markdown h3{margin-top:1.7em}.markdown p,.markdown li{max-width:78ch}.markdown code{color:#eef1f5;font:13px/1.5 ui-monospace,SFMono-Regular,monospace;"
            "background:var(--pl-color-bg-panel,#151b22);padding:2px 5px;border-radius:4px}.markdown pre{overflow:auto;"
            "padding:14px;border:1px solid var(--pl-color-border,#29313a);border-radius:8px;background:var(--pl-color-bg-panel,#11161c)}"
            ".markdown pre code{padding:0;background:transparent}.markdown blockquote{margin-left:0;padding-left:16px;border-left:3px solid var(--pl-color-brand,#8b72ff);"
            "color:var(--pl-color-fg-muted,#a8b0ba)}@media(max-width:540px){.help-nav{align-items:flex-start;flex-direction:column}}"
            f'</style></head><body><main class="help-shell"><nav class="help-nav" aria-label="Plugin help">'
            '<strong>RSS / Atom Intake</strong><a href="/plugins/rss_atom/view">Back to News</a></nav>'
            f'<article class="markdown">{rendered}</article></main></body></html>'
        )

    @router.get("/reader/{reader_id}")
    async def _reader(reader_id: str):
        detail = await asyncio.to_thread(_intake().reader_entry, reader_id)
        if detail is None:
            raise HTTPException(status_code=404, detail="Reader content is unavailable")
        try:
            source = Path(__file__).with_name("reader_view.html").read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise HTTPException(status_code=404, detail="Reader view is not bundled") from exc
        return HTMLResponse(source)

    return router


def _data_router():
    from fastapi import APIRouter, HTTPException

    router = APIRouter()

    @router.get("/news")
    async def _news(category: str = "", source: str = ""):
        try:
            return await asyncio.to_thread(_news_payload, category, source)
        except (TypeError, ValueError, RuntimeError) as exc:
            raise HTTPException(status_code=400, detail=_public_error(exc)) from exc

    @router.get("/reader/{reader_id}")
    async def _reader(reader_id: str):
        detail = await asyncio.to_thread(_intake().reader_entry, reader_id)
        if detail is None:
            raise HTTPException(status_code=404, detail="Reader content is unavailable")
        feed = next((item for item in _feeds() if item["url"] == detail["feed_url"]), None)
        if feed is None:
            raise HTTPException(status_code=404, detail="Reader source is no longer configured")
        return {
            **detail,
            "link": _safe_article_link(detail.get("link")),
            "source": feed["name"],
            "category": feed["category"],
        }

    @router.post("/refresh-category")
    async def _refresh_category(payload: dict[str, Any]):
        category = str(payload.get("category") or "").strip()
        try:
            feeds = _category_feeds(category)
            if "feed_urls" in payload:
                requested = payload["feed_urls"]
                if not isinstance(requested, list) or not requested:
                    raise ValueError("select at least one configured feed to refresh")
                requested_urls = [str(url).strip() for url in requested]
                if any(not url for url in requested_urls) or len(set(requested_urls)) != len(requested_urls):
                    raise ValueError("refresh feed URLs must be non-empty and unique")
                by_url = {feed["url"]: feed for feed in feeds}
                if any(url not in by_url for url in requested_urls):
                    raise ValueError("refresh selection contains a feed outside the configured category")
                feeds = [by_url[url] for url in requested_urls]
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=_public_error(exc)) from exc
        results = []
        for feed in feeds:
            try:
                result = await asyncio.to_thread(_intake(feed).refresh, feed["url"])
                results.append({"name": feed["name"], **result})
            except (FeedIntakeError, RuntimeError) as exc:
                results.append({"name": feed["name"], "status": "error", "error": _public_error(exc)})
        return {
            "category": feeds[0]["category"],
            "requested": len(feeds),
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
        return _json({"name": feed["name"], "url": feed["url"], **_intake(feed).refresh(feed["url"])})
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
