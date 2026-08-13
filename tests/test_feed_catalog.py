from __future__ import annotations

import json
from pathlib import Path

import pytest

from feed_catalog import FeedCatalog, FeedCatalogError


def _write_catalog(path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "catalog_version": "2026.08.0",
                "feeds": [
                    {
                        "id": "fixture-news",
                        "category": "Technology",
                        "name": "Fixture News",
                        "url": "https://feeds.example/current",
                        "previous_urls": ["https://feeds.example/old", "http://feeds.example/older"],
                        "max_items_per_refresh": 20,
                    },
                    {
                        "id": "fixture-science",
                        "category": "Science",
                        "name": "Fixture Science",
                        "url": "https://science.example/rss",
                        "previous_urls": [],
                    },
                ],
            }
        ),
        encoding="utf-8",
    )


def test_catalog_resolves_selected_ids_and_preserves_unmatched_legacy_rows(tmp_path: Path) -> None:
    path = tmp_path / "feed_catalog.json"
    _write_catalog(path)
    catalog = FeedCatalog.load(path)
    legacy = [
        "Old category | My Fixture | https://feeds.example/old | 768 | 10",
        "Personal | Custom source | https://custom.example/feed | 512 | 7",
    ]

    proposal = catalog.migration_proposal(legacy)
    resolved = catalog.resolve(
        {
            "catalogue_configured": True,
            "selected_feed_ids": ["fixture-news"],
            "catalogue_overrides": proposal.catalogue_overrides,
            "custom_feeds": proposal.custom_feeds,
            "feeds": legacy,
        }
    )

    assert catalog.version == "2026.08.0"
    assert catalog.migration_proposal(["Fixture | http://feeds.example/older"]).selected_feed_ids == ["fixture-news"]
    assert proposal.selected_feed_ids == ["fixture-news"]
    assert proposal.catalogue_overrides == {"fixture-news": {"max_feed_size_kib": 768, "max_items_per_refresh": 10}}
    assert proposal.custom_feeds == [
        {
            "category": "Personal",
            "name": "Custom source",
            "url": "https://custom.example/feed",
            "max_feed_size_kib": 512,
            "max_items_per_refresh": 7,
        }
    ]
    assert resolved == [
        {
            "category": "Personal",
            "name": "Custom source",
            "url": "https://custom.example/feed",
            "max_feed_size_kib": 512,
            "max_items_per_refresh": 7,
        },
        {
            "catalogue_id": "fixture-news",
            "category": "Technology",
            "name": "Fixture News",
            "url": "https://feeds.example/current",
            "max_feed_size_kib": 768,
            "max_items_per_refresh": 10,
        },
    ]


def test_catalogue_mode_rejects_unknown_or_duplicate_selection(tmp_path: Path) -> None:
    path = tmp_path / "feed_catalog.json"
    _write_catalog(path)
    catalog = FeedCatalog.load(path)

    with pytest.raises(FeedCatalogError, match="unknown catalogue feed"):
        catalog.resolve({"catalogue_configured": True, "selected_feed_ids": ["missing"], "custom_feeds": []})
    with pytest.raises(FeedCatalogError, match="unique"):
        catalog.resolve(
            {
                "catalogue_configured": True,
                "selected_feed_ids": ["fixture-news", "fixture-news"],
            }
        )


def test_packaged_catalogue_matches_v090_validation_report() -> None:
    root = Path(__file__).resolve().parents[1]
    catalog_payload = json.loads((root / "feed_catalog.json").read_text(encoding="utf-8"))
    report = json.loads((root / "docs/feed-catalog-validation-v0.9.0.json").read_text(encoding="utf-8"))
    catalog = FeedCatalog.load(root / "feed_catalog.json")

    included = {row["name"] for row in report["results"] if row["recommendation"] == "include"}
    excluded = {row["name"] for row in report["results"] if row["recommendation"] == "exclude"}
    assert report["candidate_count"] == 41
    assert report["included_count"] == len(catalog.feeds) == 34
    assert report["excluded_count"] == len(excluded) == 7
    assert {feed["name"] for feed in catalog.feeds} == included
    assert catalog_payload["catalog_version"] == "2026.08.0"
    assert report["duplicate_final_urls"] == {}
    assert all(feed["url"].startswith("https://") for feed in catalog.feeds)
    assert "RomeoRaven/protoAgent" not in json.dumps(report)
    assert excluded == {
        "CNN",
        "Reuters via Google",
        "Scientific American",
        "r/worldnews",
        "r/technology",
        "r/science",
        "RR protoAgent releases",
    }


def test_invalid_packaged_catalog_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "feed_catalog.json"
    _write_catalog(path)
    data = json.loads(path.read_text())
    data["feeds"][1]["url"] = "https://feeds.example/current"
    path.write_text(json.dumps(data))

    with pytest.raises(FeedCatalogError, match="unique"):
        FeedCatalog.load(path)
