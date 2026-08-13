from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

_ID_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


class FeedCatalogError(ValueError):
    pass


@dataclass(frozen=True)
class MigrationProposal:
    selected_feed_ids: list[str]
    catalogue_overrides: dict[str, dict[str, int]]
    custom_feeds: list[dict[str, Any]]


def _bounded_override(value: Any, *, label: str, maximum: int) -> int | None:
    if value is None or str(value).strip() == "":
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise FeedCatalogError(f"feed {label} must be a whole number") from exc
    if not 1 <= parsed <= maximum:
        raise FeedCatalogError(f"feed {label} must be 1 through {maximum}")
    return parsed


def parse_feed_rows(raw: Any) -> list[dict[str, Any]]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise FeedCatalogError("feed rows must be a list")
    feeds: list[dict[str, Any]] = []
    names: set[str] = set()
    urls: set[str] = set()
    for item in raw:
        if isinstance(item, str):
            if "|" not in item:
                raise FeedCatalogError(
                    "each feed row must use: Category | Name | URL | Max size KiB | Items per refresh or Name | URL"
                )
            parts = [part.strip() for part in item.split("|")]
            if len(parts) == 2:
                category, name, url = "Uncategorized", *parts
                raw_size = raw_items = None
            elif len(parts) == 3:
                category, name, url = parts
                raw_size = raw_items = None
            elif len(parts) == 5:
                category, name, url, raw_size, raw_items = parts
            else:
                raise FeedCatalogError(
                    "each feed row must use: Category | Name | URL | Max size KiB | Items per refresh or Name | URL"
                )
        elif isinstance(item, dict):
            category = str(item.get("category") or "Uncategorized").strip()
            name = str(item.get("name") or "").strip()
            url = str(item.get("url") or "").strip()
            raw_size = item.get("max_feed_size_kib")
            raw_items = item.get("max_items_per_refresh")
        else:
            raise FeedCatalogError("each configured feed must be a row string or object")
        if not category or not name or not url:
            raise FeedCatalogError("each configured feed requires non-empty category, name, and url")
        try:
            parsed_url = urlsplit(url)
        except ValueError as exc:
            raise FeedCatalogError("configured feed URL is malformed") from exc
        if parsed_url.scheme.lower() not in {"http", "https"} or not parsed_url.hostname:
            raise FeedCatalogError("configured feed URL must use HTTP or HTTPS and include a host")
        if parsed_url.username is not None or parsed_url.password is not None:
            raise FeedCatalogError("configured feed URL must not include credentials")
        key = name.casefold()
        if key in names or url in urls:
            raise FeedCatalogError("configured feed names and URLs must be unique")
        names.add(key)
        urls.add(url)
        feed: dict[str, Any] = {"category": category, "name": name, "url": url}
        size = _bounded_override(raw_size, label="size override in KiB", maximum=2048)
        items = _bounded_override(raw_items, label="items-per-refresh override", maximum=100)
        if size is not None:
            feed["max_feed_size_kib"] = size
        if items is not None:
            feed["max_items_per_refresh"] = items
        feeds.append(feed)
    return feeds


class FeedCatalog:
    def __init__(self, *, version: str, feeds: list[dict[str, Any]]) -> None:
        self.version = version
        self.feeds = feeds
        self._by_id = {feed["id"]: feed for feed in feeds}
        self._by_url: dict[str, dict[str, Any]] = {}
        for feed in feeds:
            for url in [feed["url"], *feed["previous_urls"]]:
                self._by_url[url] = feed

    @classmethod
    def load(cls, path: str | Path) -> FeedCatalog:
        try:
            payload = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise FeedCatalogError("packaged feed catalogue is unreadable") from exc
        if not isinstance(payload, dict) or payload.get("schema_version") != 1:
            raise FeedCatalogError("packaged feed catalogue schema is unsupported")
        version = str(payload.get("catalog_version") or "").strip()
        raw_feeds = payload.get("feeds")
        if not version or not isinstance(raw_feeds, list):
            raise FeedCatalogError("packaged feed catalogue is incomplete")
        feeds: list[dict[str, Any]] = []
        ids: set[str] = set()
        names: set[str] = set()
        urls: set[str] = set()
        for raw in raw_feeds:
            if not isinstance(raw, dict):
                raise FeedCatalogError("packaged catalogue feeds must be objects")
            feed_id = str(raw.get("id") or "").strip()
            category = str(raw.get("category") or "").strip()
            name = str(raw.get("name") or "").strip()
            current_url = str(raw.get("url") or "").strip()
            previous = raw.get("previous_urls", [])
            if not _ID_RE.fullmatch(feed_id):
                raise FeedCatalogError("catalogue feed IDs must be lowercase kebab-case")
            if not category or not name or not isinstance(previous, list):
                raise FeedCatalogError("packaged catalogue feed is incomplete")
            previous_urls = [str(value).strip() for value in previous]
            parsed = parse_feed_rows([{"category": category, "name": name, "url": current_url}])[0]
            if urlsplit(current_url).scheme.lower() != "https":
                raise FeedCatalogError("packaged catalogue current URLs must use HTTPS")
            all_urls = [current_url, *previous_urls]
            for url in previous_urls:
                parse_feed_rows([{"category": category, "name": f"{name} alias", "url": url}])
            if feed_id in ids or name.casefold() in names or any(url in urls for url in all_urls):
                raise FeedCatalogError("catalogue feed IDs, names, and URLs must be unique")
            ids.add(feed_id)
            names.add(name.casefold())
            urls.update(all_urls)
            feed: dict[str, Any] = {
                "id": feed_id,
                "category": parsed["category"],
                "name": parsed["name"],
                "url": parsed["url"],
                "previous_urls": previous_urls,
            }
            for key, maximum in (("max_feed_size_kib", 2048), ("max_items_per_refresh", 100)):
                value = _bounded_override(raw.get(key), label=key.replace("_", " "), maximum=maximum)
                if value is not None:
                    feed[key] = value
            feeds.append(feed)
        return cls(
            version=version,
            feeds=sorted(feeds, key=lambda item: (item["category"].casefold(), item["name"].casefold())),
        )

    def aliases_for(self, feed_id: str) -> list[str]:
        feed = self._by_id.get(feed_id)
        if feed is None:
            raise FeedCatalogError(f"unknown catalogue feed: {feed_id}")
        return list(feed["previous_urls"])

    def migration_proposal(self, legacy_rows: Any) -> MigrationProposal:
        selected: list[str] = []
        overrides: dict[str, dict[str, int]] = {}
        custom: list[dict[str, Any]] = []
        for row in parse_feed_rows(legacy_rows):
            known = self._by_url.get(row["url"])
            if known:
                feed_id = str(known["id"])
                if feed_id not in selected:
                    selected.append(feed_id)
                override = {key: int(row[key]) for key in ("max_feed_size_kib", "max_items_per_refresh") if key in row}
                if override:
                    overrides[feed_id] = override
            else:
                custom.append(row)
        return MigrationProposal(selected, overrides, custom)

    def resolve(self, config: dict[str, Any]) -> list[dict[str, Any]]:
        if not config.get("catalogue_configured"):
            return sorted(
                parse_feed_rows(config.get("feeds", [])),
                key=lambda feed: (feed["category"].casefold(), feed["name"].casefold(), feed["url"]),
            )
        raw_ids = config.get("selected_feed_ids", [])
        if not isinstance(raw_ids, list) or any(not isinstance(value, str) for value in raw_ids):
            raise FeedCatalogError("selected feed IDs must be a list")
        selected = [value.strip() for value in raw_ids]
        if len(selected) != len(set(selected)):
            raise FeedCatalogError("selected feed IDs must be unique")
        unknown = [value for value in selected if value not in self._by_id]
        if unknown:
            raise FeedCatalogError(f"unknown catalogue feed: {unknown[0]}")
        raw_overrides = config.get("catalogue_overrides", {})
        if not isinstance(raw_overrides, dict) or any(key not in selected for key in raw_overrides):
            raise FeedCatalogError("catalogue overrides must belong to selected feeds")
        overrides: dict[str, dict[str, int]] = {}
        for feed_id, raw_override in raw_overrides.items():
            if not isinstance(raw_override, dict):
                raise FeedCatalogError("catalogue overrides must be objects")
            unknown_keys = set(raw_override) - {"max_feed_size_kib", "max_items_per_refresh"}
            if unknown_keys:
                raise FeedCatalogError("catalogue override contains an unknown setting")
            bounded: dict[str, int] = {}
            for key, maximum in (("max_feed_size_kib", 2048), ("max_items_per_refresh", 100)):
                value = _bounded_override(raw_override.get(key), label=key.replace("_", " "), maximum=maximum)
                if value is not None:
                    bounded[key] = value
            if bounded:
                overrides[feed_id] = bounded
        resolved: list[dict[str, Any]] = []
        for feed_id in selected:
            source = self._by_id[feed_id]
            feed = {
                "catalogue_id": source["id"],
                "category": source["category"],
                "name": source["name"],
                "url": source["url"],
            }
            for key in ("max_feed_size_kib", "max_items_per_refresh"):
                if key in source:
                    feed[key] = source[key]
            feed.update(overrides.get(feed_id, {}))
            resolved.append(feed)
        resolved.extend(parse_feed_rows(config.get("custom_feeds", [])))
        names: set[str] = set()
        urls: set[str] = set()
        for feed in resolved:
            if feed["name"].casefold() in names or feed["url"] in urls:
                raise FeedCatalogError("selected and custom feed names and URLs must be unique")
            names.add(feed["name"].casefold())
            urls.add(feed["url"])
        return sorted(resolved, key=lambda feed: (feed["category"].casefold(), feed["name"].casefold(), feed["url"]))

    def public_payload(self, config: dict[str, Any]) -> dict[str, Any]:
        proposal = self.migration_proposal(config.get("feeds", []))
        configured = bool(config.get("catalogue_configured"))
        selected = config.get("selected_feed_ids", []) if configured else proposal.selected_feed_ids
        overrides = config.get("catalogue_overrides", {}) if configured else proposal.catalogue_overrides
        custom = config.get("custom_feeds", []) if configured else proposal.custom_feeds
        return {
            "catalog_version": self.version,
            "catalogue_configured": configured,
            "selected_feed_ids": list(selected) if isinstance(selected, list) else [],
            "catalogue_overrides": overrides if isinstance(overrides, dict) else {},
            "custom_feeds": parse_feed_rows(custom),
            "feeds": [
                {key: value for key, value in feed.items() if key not in {"previous_urls"}} for feed in self.feeds
            ],
        }
