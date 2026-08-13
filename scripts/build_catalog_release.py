from __future__ import annotations

import argparse
import json
from pathlib import Path

EXCLUDED = {
    "CNN": "Live HTTPS feed failed TLS validation and no current official RSS replacement was verified.",
    "Reuters via Google": "Indirect Google News search proxy rather than a direct maintained Reuters feed.",
    "Scientific American": "Live HTTPS feed failed TLS validation and no current official RSS replacement was verified.",
    "r/worldnews": "Returned HTTP 429 during the bounded validation run; Reddit feed availability was inconsistent.",
    "r/technology": "One request parsed, but sibling Reddit feeds returned HTTP 429 and current rate limiting is inconsistent.",
    "r/science": "Returned HTTP 429 during the bounded validation run; Reddit feed availability was inconsistent.",
    "RR protoAgent releases": "Operator-specific fork feed; excluded from the general public catalogue.",
}

IDS = {
    "BBC News": "bbc-news",
    "The Guardian": "the-guardian-world",
    "New York Times": "new-york-times-top-stories",
    "NPR": "npr-news",
    "Al Jazeera": "al-jazeera",
    "ICIJ": "icij",
    "Bloomberg": "bloomberg-markets",
    "Financial Times": "financial-times-international",
    "Wall Street Journal": "wall-street-journal-world",
    "MarketWatch": "marketwatch-top-stories",
    "CNBC": "cnbc-top-news",
    "Federal Reserve": "federal-reserve-press",
    "TechCrunch": "techcrunch",
    "The Verge": "the-verge",
    "Ars Technica": "ars-technica",
    "Wired": "wired",
    "Engadget": "engadget",
    "MIT Technology Review": "mit-technology-review",
    "ZDNet": "zdnet",
    "CNET": "cnet-news",
    "Gizmodo": "gizmodo",
    "Hacker News": "hacker-news",
    "GitHub Blog": "github-blog",
    "Stack Overflow Blog": "stack-overflow-blog",
    "Dev.to": "devto",
    "Upstream protoAgent releases": "protoagent-releases",
    "Hermes Agent releases": "hermes-agent-releases",
    "Simon Willison": "simon-willison",
    "Google Research": "google-research",
    "NASA": "nasa-news",
    "Nature": "nature",
    "ScienceDaily": "science-daily",
    "Space.com": "space-com",
    "Krebs on Security": "krebs-on-security",
}

BOUNDS = {
    "Dev.to": {"max_feed_size_kib": 512},
    "Hermes Agent releases": {"max_feed_size_kib": 1280, "max_items_per_refresh": 10},
    "Upstream protoAgent releases": {"max_items_per_refresh": 10},
    "Space.com": {"max_feed_size_kib": 1024},
}

CATEGORY_OVERRIDES = {
    "Wall Street Journal": "World",
}

INCLUSION_REASONS = {
    "BBC News": "The legacy configured URL was HTTP; the direct HTTPS form returned HTTP 200, parsed cleanly, and is packaged while the HTTP URL remains migration-only.",
    "MarketWatch": "The legacy configured URL was HTTP; its direct public HTTPS replacement returned HTTP 200 and parsed cleanly.",
    "Nature": "The legacy configured URL was HTTP; the publisher's canonical HTTPS feed returned HTTP 200 and parsed cleanly.",
    "Dev.to": "A direct public developer-community feed; broad user-generated coverage is intentional for the Developer category, and the feed passed technical validation.",
    "Upstream protoAgent releases": "A direct public project-release feed intentionally included for protoAgent operators; it passed technical validation and remains optional.",
    "Hermes Agent releases": "A direct public project-release feed intentionally included for Hermes/protoAgent operators; it passed technical validation and remains optional.",
    "Simon Willison": "A direct public individually operated technical feed intentionally included for its established developer relevance; it passed technical validation and remains optional.",
    "Wall Street Journal": "The healthy direct feed is specifically WSJ World News, so it is included under World rather than the legacy Business category.",
    "Space.com": "Original URL downgraded to HTTP; the publisher's About page identified an HTTPS replacement that returned clean RSS 2.0.",
}


def aliases(configured: str, requested: str, current: str) -> list[str]:
    values: list[str] = []
    for value in (configured, requested):
        if value != current and value not in values:
            values.append(value)
    return values


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("raw", type=Path)
    parser.add_argument("space", type=Path)
    parser.add_argument("catalog", type=Path)
    parser.add_argument("report", type=Path)
    args = parser.parse_args()
    raw = json.loads(args.raw.read_text(encoding="utf-8"))
    space = json.loads(args.space.read_text(encoding="utf-8"))["results"][0]
    results = raw["results"]
    catalog_feeds = []
    report_rows = []
    for item in results:
        name = item["name"]
        if name == "Space.com":
            selected = space
            selected["configured_url"] = item["configured_url"]
            selected["requested_url"] = item["requested_url"]
            selected["replacement_validation"] = {
                "official_source": "https://www.space.com/41418-about-us.html",
                "official_label": "RSS Feed",
                "replacement_url": space["final_url"],
            }
        else:
            selected = dict(item)
        included = name not in EXCLUDED
        if included:
            current = selected["final_url"]
            category = CATEGORY_OVERRIDES.get(name, selected["category"])
            feed = {
                "id": IDS[name],
                "category": category,
                "name": name,
                "url": current,
                "previous_urls": aliases(item["configured_url"], item["requested_url"], current),
                **BOUNDS.get(name, {}),
            }
            catalog_feeds.append(feed)
            reason = INCLUSION_REASONS.get(
                name,
                "Direct public feed returned HTTP 200, parsed without warnings, exposed entries, fit its category, and had no duplicate final URL.",
            )
        else:
            reason = EXCLUDED[name]
        report_item = {
            "name": name,
            "category": CATEGORY_OVERRIDES.get(name, item["category"]),
            "recommendation": "include" if included else "exclude",
            "reason": reason,
            "configured_url": item["configured_url"],
            "requested_url": item["requested_url"],
            "selected_url": selected.get("final_url") if included else None,
            "http_status": selected.get("http_status"),
            "content_type": selected.get("content_type"),
            "feed_version": selected.get("feed_version"),
            "entry_count": selected.get("entry_count"),
            "bytes": selected.get("bytes"),
            "bozo": selected.get("bozo"),
            "redirects": selected.get("redirects"),
            "transport_error": selected.get("transport_error"),
            "parse_error": selected.get("parse_error"),
            "feed_title": selected.get("feed_title"),
            "https_safe": selected.get("https_safe"),
            "category_fit": name not in {"Reuters via Google", "RR protoAgent releases"},
        }
        if name == "Space.com":
            report_item["replacement_validation"] = selected["replacement_validation"]
        if name == "RR protoAgent releases":
            for key in ("configured_url", "requested_url"):
                report_item[key] = "[redacted operator-specific source]"
            report_item["selected_url"] = None
            report_item["feed_title"] = "[redacted operator-specific source]"
        report_rows.append(report_item)
    catalog_feeds.sort(key=lambda item: (item["category"].casefold(), item["name"].casefold()))
    catalog = {"schema_version": 1, "catalog_version": "2026.08.0", "feeds": catalog_feeds}
    report = {
        "schema_version": 1,
        "release": "v0.9.0-beta",
        "validated_at": raw["validated_at"],
        "candidate_count": len(report_rows),
        "included_count": sum(row["recommendation"] == "include" for row in report_rows),
        "excluded_count": sum(row["recommendation"] == "exclude" for row in report_rows),
        "validation_policy": {
            **raw["validator"],
            "candidate_source": "41 operator-configured candidates; local configuration and unrelated state were not copied",
            "inclusion": "direct public HTTPS feed, HTTP 200, parseable RSS/Atom with entries and no parser warning, category fit, no duplicate final URL",
            "exclusion": "failed live validation, indirect/proxy source, inconsistent availability, or operator-specific preference",
        },
        "duplicate_final_urls": raw["duplicate_final_urls"],
        "results": report_rows,
    }
    args.catalog.write_text(json.dumps(catalog, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"catalogue={len(catalog_feeds)} report={len(report_rows)} excluded={len(EXCLUDED)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
