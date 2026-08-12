from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace

import yaml

from feed_intake import Response

PLUGIN_ROOT = Path(__file__).parents[1]
PROTOAGENT_CHECKOUT = Path(os.environ.get("PROTOAGENT_CHECKOUT", "../protoAgent")).resolve()


def _load_plugin(tmp_path: Path, monkeypatch, feeds: list[dict[str, str]]):
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
        plugin_config={"rss_atom": {"feeds": feeds}},
    )
    return load_plugins(config)


def test_current_protoagent_loads_four_tools_and_invokes_offline_paths(tmp_path, monkeypatch):
    result = _load_plugin(
        tmp_path,
        monkeypatch,
        [{"name": "Fixture", "url": "https://feeds.example/rss"}],
    )
    names = {tool.name for tool in result.tools}

    assert names == {"rss_list_feeds", "rss_refresh_feed", "rss_recent_entries", "rss_feed_status"}
    meta = next(item for item in result.meta if item["id"] == "rss_atom")
    assert meta["loaded"] is True
    assert set(meta["tools"]) == names

    by_name = {tool.name: tool for tool in result.tools}
    listed = json.loads(by_name["rss_list_feeds"].invoke({}))
    assert listed == [{"name": "Fixture", "url": "https://feeds.example/rss"}]
    recent = json.loads(by_name["rss_recent_entries"].invoke({"limit": 20}))
    assert recent == []
    status = json.loads(by_name["rss_feed_status"].invoke({"name": "Fixture"}))
    assert status == {"error": "", "name": "Fixture", "status": "never_refreshed", "url": "https://feeds.example/rss"}

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
    assert manifest["requires_pip"] == ["feedparser>=6.0.14,<7", "httpx>=0.27,<1"]
    assert manifest["capabilities"] == {"network": ["configured RSS/Atom feed hosts"], "filesystem": "scoped"}
    source = (PLUGIN_ROOT / "__init__.py").read_text()
    assert "register_surface" not in source
    assert "schedule_recurring" not in source
