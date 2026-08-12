import threading
import time
from pathlib import Path

import pytest

from feed_intake import FeedIntake, FeedIntakeError, FeedParseError, FeedSafetyError, FeedTooLargeError, Response

RSS = b"""<?xml version="1.0"?>
<rss version="2.0"><channel><title>Fixture News</title>
<item><guid>rss-1</guid><title>First &amp; Safe</title><link>https://news.example/items/1#fragment</link><pubDate>Tue, 11 Aug 2026 12:00:00 GMT</pubDate><description><![CDATA[<p>Hello <strong>world</strong><script>bad()</script></p>]]></description></item>
</channel></rss>"""

ATOM = b"""<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom"><title>Fixture Atom</title>
<entry><id>tag:example,2026:atom-1</id><title>Atom entry</title><link rel="alternate" href="https://news.example/atom/1"/><updated>2026-08-11T13:00:00Z</updated><summary type="html">&lt;p&gt;Atom &lt;em&gt;summary&lt;/em&gt;&lt;/p&gt;</summary></entry>
</feed>"""


class FixtureTransport:
    def __init__(self, responses):
        self.responses = {url: list(items) for url, items in responses.items()}
        self.calls = []

    def request(self, url, headers, *, timeout_seconds, max_bytes):
        self.calls.append((url, dict(headers), timeout_seconds, max_bytes))
        return self.responses[url].pop(0)


def allow_public(url: str) -> str | None:
    return None


def test_refresh_rss_persists_normalized_entry_with_provenance(tmp_path: Path):
    url = "https://feeds.example/rss"
    intake = FeedIntake(
        tmp_path / "feeds.db",
        FixtureTransport({url: [Response(200, {"etag": '"v1"'}, RSS)]}),
        check_url=allow_public,
    )

    result = intake.refresh(url)

    assert result == {"status": "updated", "inserted": 1, "duplicates": 0, "redirects": 0}
    assert intake.recent_entries(limit=20) == [
        {
            "entry_id": "rss-1",
            "feed_url": url,
            "title": "First & Safe",
            "link": "https://news.example/items/1",
            "published": "2026-08-11T12:00:00Z",
            "summary": "Hello world",
        }
    ]


def test_atom_revalidation_deduplicates_and_304_preserves_entries(tmp_path: Path):
    url = "https://feeds.example/atom"
    transport = FixtureTransport(
        {
            url: [
                Response(200, {"etag": '"atom-v1"', "last-modified": "Tue, 11 Aug 2026 13:01:00 GMT"}, ATOM),
                Response(200, {"etag": '"atom-v1"', "last-modified": "Tue, 11 Aug 2026 13:01:00 GMT"}, ATOM),
                Response(304, {}, b""),
            ]
        }
    )
    intake = FeedIntake(tmp_path / "feeds.db", transport, check_url=allow_public)

    assert intake.refresh(url)["inserted"] == 1
    assert intake.refresh(url) == {"status": "updated", "inserted": 0, "duplicates": 1, "redirects": 0}
    assert intake.refresh(url) == {"status": "not_modified", "inserted": 0, "duplicates": 0, "redirects": 0}

    assert len(intake.recent_entries()) == 1
    for _, headers, _, _ in transport.calls[1:]:
        assert headers["If-None-Match"] == '"atom-v1"'
        assert headers["If-Modified-Since"] == "Tue, 11 Aug 2026 13:01:00 GMT"


def test_redirect_checks_every_hop_blocks_downgrade_and_strips_cross_origin_validators(tmp_path: Path):
    start = "https://feeds.example/start"
    target = "https://cdn.example/feed"
    downgrade = "http://cdn.example/feed"
    checked = []

    def check(url: str) -> str | None:
        checked.append(url)
        return None

    transport = FixtureTransport(
        {
            start: [
                Response(200, {"etag": '"v1"'}, RSS),
                Response(302, {"location": target}, b""),
                Response(302, {"location": downgrade}, b""),
            ],
            target: [Response(200, {}, ATOM)],
        }
    )
    intake = FeedIntake(tmp_path / "feeds.db", transport, check_url=check)
    intake.refresh(start)

    assert intake.refresh(start)["redirects"] == 1
    assert checked[-2:] == [start, target]
    redirected_headers = transport.calls[-1][1]
    assert "If-None-Match" not in redirected_headers
    assert "If-Modified-Since" not in redirected_headers

    with pytest.raises(FeedSafetyError, match="HTTPS downgrade"):
        intake.refresh(start)
    assert downgrade not in checked


@pytest.mark.parametrize(
    ("response", "error_type"),
    [
        (Response(200, {}, b"<rss><channel><item></rss>"), FeedParseError),
        (Response(200, {"content-length": "101"}, b""), FeedTooLargeError),
        (Response(503, {}, b"unavailable"), FeedIntakeError),
    ],
    ids=["malformed", "declared-oversize", "http-error"],
)
def test_failed_refresh_records_status_without_partial_entries(tmp_path: Path, response, error_type):
    url = "https://feeds.example/failure"
    intake = FeedIntake(
        tmp_path / "feeds.db",
        FixtureTransport({url: [response]}),
        check_url=allow_public,
        max_bytes=100,
    )

    with pytest.raises(error_type):
        intake.refresh(url)

    assert intake.recent_entries() == []
    assert intake.feed_status(url)["status"] == "error"
    assert intake.feed_status(url)["error"]


def test_successful_refresh_caps_stored_entries_per_feed(tmp_path: Path):
    url = "https://feeds.example/capped"
    body = b"""<?xml version="1.0"?><rss version="2.0"><channel><title>Capped</title>
    <item><guid>one</guid><title>One</title></item>
    <item><guid>two</guid><title>Two</title></item>
    <item><guid>three</guid><title>Three</title></item>
    </channel></rss>"""
    intake = FeedIntake(
        tmp_path / "feeds.db",
        FixtureTransport({url: [Response(200, {}, body)]}),
        check_url=allow_public,
        max_entries_per_feed=2,
    )

    intake.refresh(url)

    assert [entry["entry_id"] for entry in intake.recent_entries()] == ["three", "two"]


def test_shared_database_path_serializes_refreshes_across_instances(tmp_path: Path):
    url = "https://feeds.example/concurrent"

    class RacingTransport:
        active = 0
        max_active = 0
        guard = threading.Lock()

        def request(self, url, headers, *, timeout_seconds, max_bytes):
            with self.guard:
                self.active += 1
                self.max_active = max(self.max_active, self.active)
            time.sleep(0.05)
            with self.guard:
                self.active -= 1
            return Response(200, {}, RSS)

    transport = RacingTransport()
    db_path = tmp_path / "feeds.db"
    first = FeedIntake(db_path, transport, check_url=allow_public)
    second = FeedIntake(db_path, transport, check_url=allow_public)
    threads = [threading.Thread(target=item.refresh, args=(url,)) for item in (first, second)]

    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert transport.max_active == 1


def test_non_http_scheme_is_rejected_before_egress_or_transport(tmp_path: Path):
    transport = FixtureTransport({})
    intake = FeedIntake(tmp_path / "feeds.db", transport, check_url=allow_public)

    with pytest.raises(FeedSafetyError, match="HTTP"):
        intake.refresh("file:///etc/passwd")

    assert transport.calls == []
