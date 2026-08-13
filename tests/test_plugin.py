from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace

import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient

from feed_intake import Response

PLUGIN_ROOT = Path(__file__).parents[1]
PROTOAGENT_CHECKOUT = Path(os.environ.get("PROTOAGENT_CHECKOUT", "../protoAgent")).resolve()


def _load_plugin(tmp_path: Path, monkeypatch, feeds, **plugin_config):
    if not (PROTOAGENT_CHECKOUT / "graph" / "plugins" / "loader.py").exists():
        raise AssertionError(f"current protoAgent checkout missing: {PROTOAGENT_CHECKOUT}")
    live = tmp_path / "plugins"
    live.mkdir()
    shutil.copytree(PLUGIN_ROOT, live / "rss_atom", ignore=shutil.ignore_patterns(".git", ".venv", "__pycache__"))
    monkeypatch.syspath_prepend(str(PROTOAGENT_CHECKOUT))
    monkeypatch.setenv("PROTOAGENT_PLUGINS_DIR", str(live))
    monkeypatch.setenv("PROTOAGENT_HOME", str(tmp_path / "state"))

    from graph.plugins.loader import load_plugins

    config = SimpleNamespace(
        plugins_dir=str(live),
        plugins_enabled=["rss_atom"],
        plugins_disabled=[],
        plugin_config={"rss_atom": {"feeds": feeds, **plugin_config}},
    )
    return load_plugins(config)


def test_current_protoagent_loads_four_tools_and_invokes_offline_paths(tmp_path, monkeypatch):
    result = _load_plugin(
        tmp_path,
        monkeypatch,
        [{"name": "Fixture", "url": "https://feeds.example/rss"}],
    )
    rss_names = {"rss_list_feeds", "rss_refresh_feed", "rss_recent_entries", "rss_feed_status"}
    names = {tool.name for tool in result.tools if tool.name in rss_names}

    assert names == rss_names
    meta = next(item for item in result.meta if item["id"] == "rss_atom")
    assert meta["loaded"] is True
    assert set(meta["tools"]) == names

    by_name = {tool.name: tool for tool in result.tools}
    listed = json.loads(by_name["rss_list_feeds"].invoke({}))
    assert listed == [{"category": "Uncategorized", "name": "Fixture", "url": "https://feeds.example/rss"}]
    recent = json.loads(by_name["rss_recent_entries"].invoke({"limit": 20}))
    assert recent == []
    status = json.loads(by_name["rss_feed_status"].invoke({"name": "Fixture"}))
    assert status == {
        "checked_at": "",
        "duplicates": 0,
        "error": "",
        "inserted": 0,
        "name": "Fixture",
        "processed": 0,
        "status": "never_refreshed",
        "url": "https://feeds.example/rss",
    }

    loaded_module = sys.modules["protoagent_plugin_rss_atom"]

    class OfflineTransport:
        def request(self, url, headers, *, timeout_seconds, max_bytes):
            body = b"""<?xml version="1.0"?><rss version="2.0"><channel><title>Fixture</title>
            <item><guid>ci-1</guid><title>CI entry</title><link>https://feeds.example/entry</link>
            <pubDate>Tue, 11 Aug 2026 12:00:00 GMT</pubDate><description>Native path</description></item>
            </channel></rss>"""
            return Response(200, {"etag": '"ci-v1"'}, body)

    loaded_module._transport = OfflineTransport()
    from security.egress import set_allowed_hosts

    set_allowed_hosts(["feeds.example"])
    refreshed = json.loads(by_name["rss_refresh_feed"].invoke({"name": "Fixture"}))
    assert refreshed["status"] == "updated"
    assert refreshed["inserted"] == 1
    recent = json.loads(by_name["rss_recent_entries"].invoke({"limit": 20, "name": "Fixture"}))
    assert [entry["entry_id"] for entry in recent] == ["ci-1"]


def test_manifest_declares_scoped_state_network_and_no_background_surface():
    manifest = yaml.safe_load((PLUGIN_ROOT / "protoagent.plugin.yaml").read_text())
    assert manifest["id"] == "rss_atom"
    assert manifest["enabled"] is False
    assert manifest["repository"] == "https://github.com/RomeoRaven/rss-atom-plugin"
    assert manifest["homepage"] == "https://agent.protolabs.studio"
    assert manifest["config"]["max_bytes"] == 262144
    assert "max_feed_size_kib" not in manifest["config"]
    assert manifest["requires_pip"] == [
        {"pkg": "feedparser>=6.0.14,<7", "scope": "host"},
        {"pkg": "httpx>=0.27,<1", "scope": "host"},
        {"pkg": "markdown-it-py>=3,<5", "scope": "host"},
        {"pkg": "nh3==0.3.6", "scope": "host"},
    ]
    assert manifest["capabilities"] == {"network": ["configured RSS/Atom feed hosts"], "filesystem": "scoped"}
    settings = {item["key"]: item for item in manifest["settings"]}
    assert list(settings) == [
        "feeds",
        "max_items_per_refresh",
        "default_recent_items",
        "max_entries_per_feed",
        "max_feed_size_kib",
        "timeout_seconds",
    ]
    assert settings["feeds"]["type"] == "string_list"
    assert (settings["max_items_per_refresh"]["minimum"], settings["max_items_per_refresh"]["maximum"]) == (1, 100)
    assert (settings["default_recent_items"]["minimum"], settings["default_recent_items"]["maximum"]) == (1, 100)
    assert (settings["max_entries_per_feed"]["minimum"], settings["max_entries_per_feed"]["maximum"]) == (1, 10000)
    assert (settings["max_feed_size_kib"]["minimum"], settings["max_feed_size_kib"]["maximum"]) == (1, 2048)
    assert (settings["timeout_seconds"]["minimum"], settings["timeout_seconds"]["maximum"]) == (1, 60)
    source = (PLUGIN_ROOT / "__init__.py").read_text()
    assert "register_surface" not in source
    assert "schedule_recurring" not in source


def test_bundled_readme_is_exposed_as_the_manifest_help_page(tmp_path, monkeypatch):
    result = _load_plugin(
        tmp_path,
        monkeypatch,
        ["Developer | Fixture News | https://feeds.example/rss"],
    )
    manifest = yaml.safe_load((PLUGIN_ROOT / "protoagent.plugin.yaml").read_text())

    assert manifest["guide_url"] == "/plugins/rss_atom/help"
    assert manifest["public_paths"] == ["/plugins/rss_atom/help"]
    rss_routers = [router for router in result.routers if router["plugin_id"] == "rss_atom"]
    app = FastAPI()
    for router in rss_routers:
        app.include_router(router["router"], prefix=router["prefix"])

    response = TestClient(app).get("/plugins/rss_atom/help")

    assert response.status_code == 200
    assert "<title>RSS / Atom Intake help</title>" in response.text
    assert "<h1>RSS / Atom Intake for protoAgent</h1>" in response.text
    assert "maximum decompressed feed size" in response.text


def test_bundled_readme_help_is_safe_offline_and_linked_from_news(tmp_path, monkeypatch):
    result = _load_plugin(
        tmp_path,
        monkeypatch,
        ["Developer | Fixture News | https://feeds.example/rss"],
    )
    loaded_module = sys.modules["protoagent_plugin_rss_atom"]
    assert loaded_module.__file__ is not None
    readme = Path(loaded_module.__file__).with_name("README.md")
    original = readme.read_text(encoding="utf-8")
    readme.write_text(
        original
        + "\n\n## Safety fixture\n\n[Example](https://example.com/docs)\n\n"
        + '<script id="readme-script">window.README_EXECUTED = true</script>\n',
        encoding="utf-8",
    )
    rss_routers = [router for router in result.routers if router["plugin_id"] == "rss_atom"]
    app = FastAPI()
    for router in rss_routers:
        app.include_router(router["router"], prefix=router["prefix"])
    client = TestClient(app)

    help_page = client.get("/plugins/rss_atom/help")
    news_page = client.get("/plugins/rss_atom/view")

    assert help_page.status_code == 200
    assert "plugin-kit.css" in help_page.text
    assert ".markdown code{color:#eef1f5" in help_page.text
    assert 'href="https://example.com/docs" target="_blank" rel="noopener noreferrer"' in help_page.text
    assert '<script id="readme-script">' not in help_page.text
    assert "&lt;script id=&quot;readme-script&quot;&gt;" in help_page.text
    assert 'href="/plugins/rss_atom/help"' in news_page.text


def test_bundled_readme_help_contains_copyable_optional_feed_guidance(tmp_path, monkeypatch):
    result = _load_plugin(
        tmp_path,
        monkeypatch,
        ["Developer | Fixture News | https://feeds.example/rss"],
    )
    rss_routers = [router for router in result.routers if router["plugin_id"] == "rss_atom"]
    app = FastAPI()
    for router in rss_routers:
        app.include_router(router["router"], prefix=router["prefix"])

    response = TestClient(app).get("/plugins/rss_atom/help")

    assert response.status_code == 200
    assert "Quick start" in response.text
    assert "Optional feed ideas" in response.text
    assert "Google Developers" in response.text
    assert "CDC Emerging Infectious Diseases" in response.text
    assert "does not add or refresh" in response.text
    assert "Troubleshooting" in response.text


def test_bundled_readme_help_reports_missing_source_cleanly(tmp_path, monkeypatch):
    result = _load_plugin(
        tmp_path,
        monkeypatch,
        ["Developer | Fixture News | https://feeds.example/rss"],
    )
    loaded_module = sys.modules["protoagent_plugin_rss_atom"]
    assert loaded_module.__file__ is not None
    Path(loaded_module.__file__).with_name("README.md").unlink()
    rss_routers = [router for router in result.routers if router["plugin_id"] == "rss_atom"]
    app = FastAPI()
    for router in rss_routers:
        app.include_router(router["router"], prefix=router["prefix"])

    response = TestClient(app).get("/plugins/rss_atom/help")

    assert response.status_code == 404
    assert response.json() == {"detail": "Plugin help is not bundled"}


def test_gui_feed_rows_are_normalized_and_default_recent_limit_is_configurable(tmp_path, monkeypatch):
    result = _load_plugin(
        tmp_path,
        monkeypatch,
        ["Fixture News | https://feeds.example/rss", "Updates|https://updates.example/atom"],
        default_recent_items=7,
    )
    by_name = {tool.name: tool for tool in result.tools}

    assert json.loads(by_name["rss_list_feeds"].invoke({})) == [
        {"category": "Uncategorized", "name": "Fixture News", "url": "https://feeds.example/rss"},
        {"category": "Uncategorized", "name": "Updates", "url": "https://updates.example/atom"},
    ]
    loaded_module = sys.modules["protoagent_plugin_rss_atom"]
    captured = {}

    class StubIntake:
        def recent_entries(self, *, limit, feed_url, feed_urls=None):
            captured.update(limit=limit, feed_url=feed_url, feed_urls=feed_urls)
            return []

    monkeypatch.setattr(loaded_module, "_intake", lambda: StubIntake())
    assert json.loads(by_name["rss_recent_entries"].invoke({"name": "Fixture News"})) == []
    assert captured == {"limit": 7, "feed_url": "https://feeds.example/rss", "feed_urls": None}


def test_categorized_feed_rows_and_legacy_rows_support_category_filters(tmp_path, monkeypatch):
    result = _load_plugin(
        tmp_path,
        monkeypatch,
        [
            "Technology | Fixture News | https://feeds.example/rss",
            "Developer | Updates | https://updates.example/atom",
            "Legacy | https://legacy.example/feed",
        ],
        default_recent_items=7,
    )
    by_name = {tool.name: tool for tool in result.tools}

    assert json.loads(by_name["rss_list_feeds"].invoke({"category": "technology"})) == [
        {"category": "Technology", "name": "Fixture News", "url": "https://feeds.example/rss"}
    ]
    assert json.loads(by_name["rss_list_feeds"].invoke({"category": ""})) == [
        {"category": "Developer", "name": "Updates", "url": "https://updates.example/atom"},
        {"category": "Technology", "name": "Fixture News", "url": "https://feeds.example/rss"},
        {"category": "Uncategorized", "name": "Legacy", "url": "https://legacy.example/feed"},
    ]

    loaded_module = sys.modules["protoagent_plugin_rss_atom"]
    captured = {}

    class StubIntake:
        def recent_entries(self, *, limit, feed_url="", feed_urls=None):
            captured.update(limit=limit, feed_url=feed_url, feed_urls=feed_urls)
            return []

    monkeypatch.setattr(loaded_module, "_intake", lambda: StubIntake())
    assert json.loads(by_name["rss_recent_entries"].invoke({"category": "Developer"})) == []
    assert captured == {
        "limit": 7,
        "feed_url": "",
        "feed_urls": ["https://updates.example/atom"],
    }


def test_feed_rows_support_optional_per_feed_size_and_item_limits(tmp_path, monkeypatch):
    result = _load_plugin(
        tmp_path,
        monkeypatch,
        [
            "Developer | Hermes releases | https://example.com/releases.atom | 1280 | 10",
            "Developer | Inherited defaults | https://example.com/default.atom",
        ],
        max_items_per_refresh=100,
        max_feed_size_kib=256,
    )
    listed = next(tool for tool in result.tools if tool.name == "rss_list_feeds")

    assert json.loads(listed.invoke({})) == [
        {
            "category": "Developer",
            "max_feed_size_kib": 1280,
            "max_items_per_refresh": 10,
            "name": "Hermes releases",
            "url": "https://example.com/releases.atom",
        },
        {
            "category": "Developer",
            "name": "Inherited defaults",
            "url": "https://example.com/default.atom",
        },
    ]


def test_refresh_uses_per_feed_size_and_item_limits(tmp_path, monkeypatch):
    result = _load_plugin(
        tmp_path,
        monkeypatch,
        ["Developer | Bounded | https://feeds.example/rss | 768 | 10"],
        max_feed_size_kib=256,
        max_items_per_refresh=20,
    )
    loaded_module = sys.modules["protoagent_plugin_rss_atom"]
    calls = []

    class OfflineTransport:
        def request(self, url, headers, *, timeout_seconds, max_bytes):
            calls.append({"url": url, "max_bytes": max_bytes})
            items = "".join(f"<item><guid>{index}</guid><title>Item {index}</title></item>" for index in range(12))
            return Response(200, {}, f'<rss version="2.0"><channel>{items}</channel></rss>'.encode())

    loaded_module._transport = OfflineTransport()
    from security.egress import set_allowed_hosts

    set_allowed_hosts(["feeds.example"])
    refresh = next(tool for tool in result.tools if tool.name == "rss_refresh_feed")

    payload = json.loads(refresh.invoke({"name": "Bounded"}))

    assert calls == [{"url": "https://feeds.example/rss", "max_bytes": 768 * 1024}]
    assert payload["processed"] == 10
    assert payload["inserted"] == 10


def test_legacy_max_bytes_remains_effective_unless_new_kib_setting_is_explicit(tmp_path, monkeypatch):
    manifest_defaults = yaml.safe_load((PLUGIN_ROOT / "protoagent.plugin.yaml").read_text())["config"]
    manifest_defaults.pop("feeds", None)
    legacy_resolved = {**manifest_defaults, "max_bytes": 333333}
    result = _load_plugin(
        tmp_path,
        monkeypatch,
        ["Legacy | https://feeds.example/rss"],
        **legacy_resolved,
    )
    loaded_module = sys.modules["protoagent_plugin_rss_atom"]
    calls = []

    class OfflineTransport:
        def request(self, url, headers, *, timeout_seconds, max_bytes):
            calls.append(max_bytes)
            return Response(200, {}, b'<rss version="2.0"><channel></channel></rss>')

    loaded_module._transport = OfflineTransport()
    from security.egress import set_allowed_hosts

    set_allowed_hosts(["feeds.example"])
    refresh = next(tool for tool in result.tools if tool.name == "rss_refresh_feed")
    assert json.loads(refresh.invoke({"name": "Legacy"}))["status"] == "updated"
    assert calls == [333333]

    explicit_root = tmp_path / "explicit"
    explicit_root.mkdir()
    explicit_resolved = {**manifest_defaults, "max_bytes": 333333, "max_feed_size_kib": 512}
    explicit = _load_plugin(
        explicit_root,
        monkeypatch,
        ["Modern | https://feeds.example/modern"],
        **explicit_resolved,
    )
    explicit_module = sys.modules["protoagent_plugin_rss_atom"]
    explicit_calls = []

    class ExplicitTransport:
        def request(self, url, headers, *, timeout_seconds, max_bytes):
            explicit_calls.append(max_bytes)
            return Response(200, {}, b'<rss version="2.0"><channel></channel></rss>')

    explicit_module._transport = ExplicitTransport()
    set_allowed_hosts(["feeds.example"])
    explicit_refresh = next(tool for tool in explicit.tools if tool.name == "rss_refresh_feed")
    assert json.loads(explicit_refresh.invoke({"name": "Modern"}))["status"] == "updated"
    assert explicit_calls == [512 * 1024]


def test_per_feed_item_limit_above_hard_ceiling_fails_closed(tmp_path, monkeypatch):
    result = _load_plugin(
        tmp_path,
        monkeypatch,
        ["Developer | Too many | https://example.com/feed | 256 | 101"],
    )
    listed = next(tool for tool in result.tools if tool.name == "rss_list_feeds")

    payload = json.loads(listed.invoke({}))

    assert payload["status"] == "invalid_configuration"
    assert "1 through 100" in payload["error"]


def test_news_view_exposes_category_selector_filtered_entries_and_category_refresh(tmp_path, monkeypatch):
    result = _load_plugin(
        tmp_path,
        monkeypatch,
        [
            "Technology | Fixture News | https://feeds.example/rss",
            "Developer | Updates | https://updates.example/atom",
            "Technology | More Tech | https://more.example/rss",
        ],
        default_recent_items=7,
    )
    manifest = yaml.safe_load((PLUGIN_ROOT / "protoagent.plugin.yaml").read_text())
    assert manifest["views"] == [
        {
            "id": "news",
            "label": "News",
            "icon": "Newspaper",
            "path": "/plugins/rss_atom/view",
        }
    ]
    rss_routers = [router for router in result.routers if router["plugin_id"] == "rss_atom"]
    assert {router["prefix"] for router in rss_routers} == {
        "/plugins/rss_atom",
        "/api/plugins/rss_atom",
    }

    loaded_module = sys.modules["protoagent_plugin_rss_atom"]
    refreshed = []

    class StubIntake:
        def count_entries(self, *, feed_urls):
            return 1 if feed_urls == ["https://feeds.example/rss", "https://more.example/rss"] else 0

        def recent_entries_with_reader(self, *, limit, feed_url="", feed_urls=None):
            assert limit == 7
            if feed_url:
                assert feed_url == "https://more.example/rss"
                assert feed_urls is None
            else:
                assert feed_urls == ["https://feeds.example/rss", "https://more.example/rss"]
            return [
                {
                    "entry_id": "story-1",
                    "feed_url": feed_url or "https://feeds.example/rss",
                    "title": "A useful headline",
                    "link": "javascript:alert(1)",
                    "published": "2026-08-12T12:00:00+00:00",
                    "excerpt": "A concise summary.",
                    "has_reader": False,
                    "reader_id": "0" * 64,
                }
            ]

        def refresh(self, url):
            refreshed.append(url)
            return {"status": "updated", "processed": 1, "inserted": 1, "duplicates": 0}

        def feed_status(self, url):
            return {
                "status": "never_refreshed",
                "error": "",
                "checked_at": "",
                "processed": 0,
                "inserted": 0,
                "duplicates": 0,
            }

    monkeypatch.setattr(loaded_module, "_intake", lambda feed=None: StubIntake())
    app = FastAPI()
    for router in rss_routers:
        app.include_router(router["router"], prefix=router["prefix"])
    client = TestClient(app)

    view = client.get("/plugins/rss_atom/view")
    assert view.status_code == 200
    assert 'id="category-selector"' in view.text
    assert "plugin-kit.css" in view.text
    assert "grid-template-columns: minmax(0, 1fr)" in view.text
    assert ".rail, .main { width: 100%; min-width: 0; }" in view.text
    assert 'data-source="all"' in view.text
    assert "data-skip-toggle" in view.text
    assert "data-feed-refresh" in view.text
    obsolete_toggle = "data-" + "refresh" + "-toggle"
    assert obsolete_toggle not in view.text
    assert 'class="category-name"' in view.text
    assert ".category { width: 100%; display: grid; grid-template-columns: minmax(0, 1fr);" in view.text
    assert "async function refreshFeeds(feedUrls, label)" in view.text
    assert "refreshFeeds([button.dataset.url], button.dataset.name)" in view.text
    assert 'aria-label="Filter articles by source"' in view.text
    assert "rss_atom.skipped_feed_urls" in view.text
    assert "feed_urls: feedUrls" in view.text
    assert "refreshFeeds(includedFeeds, state.category)" in view.text
    data = client.get("/api/plugins/rss_atom/news", params={"category": "Technology"})
    assert data.status_code == 200
    assert data.json()["categories"] == [
        {"name": "Developer", "feed_count": 1, "entry_count": 0},
        {"name": "Technology", "feed_count": 2, "entry_count": 1},
    ]
    assert data.json()["entries"][0]["source"] == "Fixture News"
    assert data.json()["entries"][0]["category"] == "Technology"
    assert data.json()["entries"][0]["link"] == ""
    filtered = client.get(
        "/api/plugins/rss_atom/news",
        params={"category": "Technology", "source": "More Tech"},
    )
    assert filtered.status_code == 200
    assert filtered.json()["selected_source"] == "More Tech"
    assert filtered.json()["entries"][0]["source"] == "More Tech"
    invalid_source = client.get(
        "/api/plugins/rss_atom/news",
        params={"category": "Technology", "source": "Updates"},
    )
    assert invalid_source.status_code == 400
    refresh = client.post("/api/plugins/rss_atom/refresh-category", json={"category": "Technology"})
    assert refresh.status_code == 200
    assert refreshed == ["https://feeds.example/rss", "https://more.example/rss"]
    refreshed.clear()
    subset = client.post(
        "/api/plugins/rss_atom/refresh-category",
        json={"category": "Technology", "feed_urls": ["https://more.example/rss"]},
    )
    assert subset.status_code == 200
    assert subset.json()["requested"] == 1
    assert refreshed == ["https://more.example/rss"]
    outside = client.post(
        "/api/plugins/rss_atom/refresh-category",
        json={"category": "Technology", "feed_urls": ["https://updates.example/atom"]},
    )
    assert outside.status_code == 400
    empty = client.post(
        "/api/plugins/rss_atom/refresh-category",
        json={"category": "Technology", "feed_urls": []},
    )
    assert empty.status_code == 400


def test_news_exposes_durable_per_feed_health_and_effective_limits(tmp_path, monkeypatch):
    result = _load_plugin(
        tmp_path,
        monkeypatch,
        [
            "Developer | Healthy | https://feeds.example/healthy | 768 | 10",
            "Developer | Failed | https://feeds.example/failed",
        ],
        max_feed_size_kib=256,
        max_items_per_refresh=20,
    )
    loaded_module = sys.modules["protoagent_plugin_rss_atom"]

    class StubIntake:
        def count_entries(self, *, feed_urls):
            return 4 if feed_urls == ["https://feeds.example/failed", "https://feeds.example/healthy"] else 0

        def recent_entries_with_reader(self, *, limit, feed_url="", feed_urls=None):
            return []

        def feed_status(self, url):
            if url.endswith("healthy"):
                return {
                    "status": "updated",
                    "error": "",
                    "checked_at": "2026-08-12T18:00:00Z",
                    "processed": 10,
                    "inserted": 0,
                    "duplicates": 10,
                }
            return {
                "status": "error",
                "error": "FeedIntakeError: feed request failed with HTTP 503",
                "checked_at": "2026-08-12T18:01:00Z",
                "processed": 0,
                "inserted": 0,
                "duplicates": 0,
            }

    monkeypatch.setattr(loaded_module, "_intake", lambda feed=None: StubIntake())
    app = FastAPI()
    for router in [item for item in result.routers if item["plugin_id"] == "rss_atom"]:
        app.include_router(router["router"], prefix=router["prefix"])
    client = TestClient(app)

    payload = client.get("/api/plugins/rss_atom/news", params={"category": "Developer"}).json()
    by_name = {feed["name"]: feed for feed in payload["feeds"]}

    assert by_name["Healthy"] == {
        "category": "Developer",
        "name": "Healthy",
        "url": "https://feeds.example/healthy",
        "max_feed_size_kib": 768,
        "max_items_per_refresh": 10,
        "effective_max_feed_size_kib": 768,
        "effective_max_items_per_refresh": 10,
        "stored_count": 0,
        "health": {
            "status": "updated",
            "error": "",
            "checked_at": "2026-08-12T18:00:00Z",
            "processed": 10,
            "inserted": 0,
            "duplicates": 10,
        },
    }
    assert by_name["Failed"]["effective_max_feed_size_kib"] == 256
    assert by_name["Failed"]["effective_max_items_per_refresh"] == 20
    assert by_name["Failed"]["health"]["status"] == "error"

    view = client.get("/plugins/rss_atom/view").text
    assert 'id="feed-health"' in view
    assert "Not checked yet" in view
    assert "Working — no new articles" in view
    assert "Last refresh failed" in view
    assert "Source unchanged since the last check" in view
    assert "Source returned no entries" in view


def test_news_list_and_dedicated_reader_api_keep_structured_body_off_the_list(tmp_path, monkeypatch):
    result = _load_plugin(
        tmp_path,
        monkeypatch,
        ["Developer | Structured | https://feeds.example/structured"],
        default_recent_items=7,
    )
    loaded_module = sys.modules["protoagent_plugin_rss_atom"]
    structured = (
        "<h2>Release notes</h2><p>"
        + ("A bounded but meaningful reader paragraph. " * 20)
        + "</p><ul><li>First change</li><li><code>safe_call()</code></li></ul>"
        + '<a href="https://safe.example/docs">Documentation</a>'
        + '<script>window.PWNED=true</script><a href="javascript:bad()">unsafe</a>'
    )

    class OfflineTransport:
        def request(self, url, headers, *, timeout_seconds, max_bytes):
            body = f"""<?xml version="1.0"?><rss version="2.0"><channel><title>Structured</title>
            <item><guid>reader-1</guid><title>Readable release</title>
            <link>https://news.example/reader-1</link><description><![CDATA[{structured}]]></description></item>
            </channel></rss>""".encode()
            return Response(200, {}, body)

    loaded_module._transport = OfflineTransport()
    from security.egress import set_allowed_hosts

    set_allowed_hosts(["feeds.example"])
    refresh = next(tool for tool in result.tools if tool.name == "rss_refresh_feed")
    assert json.loads(refresh.invoke({"name": "Structured"}))["inserted"] == 1
    app = FastAPI()
    for router in [item for item in result.routers if item["plugin_id"] == "rss_atom"]:
        app.include_router(router["router"], prefix=router["prefix"])
    client = TestClient(app)

    news = client.get("/api/plugins/rss_atom/news", params={"category": "Developer"})
    assert news.status_code == 200
    listed = news.json()["entries"][0]
    assert listed["entry_id"] == "reader-1"
    assert listed["has_reader"] is True
    assert len(listed["excerpt"]) <= 400
    assert "reader_html" not in listed
    assert "summary" not in listed

    detail = client.get(f"/api/plugins/rss_atom/reader/{listed['reader_id']}")
    assert detail.status_code == 200
    payload = detail.json()
    assert payload["source"] == "Structured"
    assert payload["category"] == "Developer"
    assert payload["link"] == "https://news.example/reader-1"
    assert "<h2>Release notes</h2>" in payload["reader_html"]
    assert "<ul>" in payload["reader_html"]
    assert "javascript:" not in payload["reader_html"]
    assert "<script" not in payload["reader_html"]

    reader_page = client.get(f"/plugins/rss_atom/reader/{listed['reader_id']}")
    assert reader_page.status_code == 200
    assert 'id="reader-content"' in reader_page.text
    assert "/api/plugins/rss_atom/reader/" in reader_page.text
    assert "Back to News" in reader_page.text

    news_page = client.get("/plugins/rss_atom/view")
    assert news_page.status_code == 200
    assert "entry.excerpt" in news_page.text
    assert "entry.summary" not in news_page.text
    assert 'class="entry-actions"' in news_page.text
    assert "entry.has_reader" in news_page.text
    assert "Read here" in news_page.text
    assert "Open source" in news_page.text
    assert "-webkit-line-clamp: 5" in news_page.text
    assert "/plugins/rss_atom/reader/" in news_page.text
    assert "window.setTimeout" not in news_page.text

    assert client.get("/api/plugins/rss_atom/reader/not-a-reader-id").status_code == 404
    assert client.get("/plugins/rss_atom/reader/not-a-reader-id").status_code == 404

    empty_refresh = client.post("/api/plugins/rss_atom/refresh-category", json={})
    assert empty_refresh.status_code == 400
    assert empty_refresh.json()["detail"] == "choose a configured category to refresh"


def test_invalid_gui_feed_row_fails_closed_with_actionable_format(tmp_path, monkeypatch):
    result = _load_plugin(tmp_path, monkeypatch, ["Missing separator https://feeds.example/rss"])
    listed = next(tool for tool in result.tools if tool.name == "rss_list_feeds")

    payload = json.loads(listed.invoke({}))

    assert payload["status"] == "invalid_configuration"
    assert "Name | URL" in payload["error"]


def test_repository_declares_mit_license():
    license_text = (PLUGIN_ROOT / "LICENSE").read_text()
    assert license_text.startswith("MIT License\n")
    assert "Copyright (c) 2026 RomeoRaven" in license_text


def test_refresh_tool_bounds_agent_visible_egress_error(tmp_path, monkeypatch):
    result = _load_plugin(
        tmp_path,
        monkeypatch,
        [{"name": "Blocked", "url": "https://blocked.example/rss"}],
    )
    from security.egress import set_allowed_hosts

    set_allowed_hosts([f"allowed-{index}.example" for index in range(2000)])
    refresh = next(tool for tool in result.tools if tool.name == "rss_refresh_feed")

    payload = json.loads(refresh.invoke({"name": "Blocked"}))

    assert payload["status"] == "error"
    assert len(payload["error"]) <= 500
    assert len(json.dumps(payload)) < 700
