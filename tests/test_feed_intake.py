import base64
import json
import sqlite3
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from feed_intake import (
    FeedIntake,
    FeedIntakeError,
    FeedParseError,
    FeedSafetyError,
    FeedTooLargeError,
    HttpxTransport,
    ProtoAgentEgressPolicy,
    Response,
)

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

    assert result == {"status": "updated", "processed": 1, "inserted": 1, "duplicates": 0, "redirects": 0}
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


def test_explicit_refresh_stores_bounded_sanitized_reader_body_and_compact_excerpt(tmp_path: Path):
    url = "https://feeds.example/structured"
    repeated = " ".join(f"reader word {index}" for index in range(80))
    structured = f"""
    <h2 id="release" style="color:red">Release notes</h2>
    <p onclick="bad()">{repeated} <strong>important</strong></p>
    <blockquote>Keep this context.</blockquote>
    <ul><li>First item</li><li><code>safe_call()</code></li></ul>
    <script>window.PWNED = true</script><style>body{{display:none}}</style>
    <svg onload="bad()"><script>svgBad()</script></svg>
    <iframe src="https://evil.example"></iframe><form><input name="secret"></form>
    <a href="javascript:bad()">unsafe</a>
    <a href="/relative">relative</a>
    <a href="https://safe.example/docs" class="tracked">safe docs</a>
    """
    body = f"""<?xml version="1.0"?><rss version="2.0"><channel><title>Structured</title>
    <item><guid>structured-1</guid><title>Structured entry</title>
    <link>https://news.example/structured-1</link><description><![CDATA[{structured}]]></description></item>
    </channel></rss>""".encode()
    intake = FeedIntake(
        tmp_path / "feeds.db",
        FixtureTransport({url: [Response(200, {}, body)]}),
        check_url=allow_public,
    )

    assert intake.refresh(url)["inserted"] == 1

    listed = intake.recent_entries_with_reader(limit=20)
    assert len(listed) == 1
    assert listed[0]["entry_id"] == "structured-1"
    assert listed[0]["reader_id"]
    assert listed[0]["has_reader"] is True
    assert len(listed[0]["excerpt"]) <= 400
    assert listed[0]["excerpt"].endswith("…")
    assert "reader word" in listed[0]["excerpt"]
    assert "summary" not in listed[0]

    detail = intake.reader_entry(listed[0]["reader_id"])
    assert detail is not None
    assert detail["title"] == "Structured entry"
    assert detail["link"] == "https://news.example/structured-1"
    assert detail["content_version"] == 1
    reader_html = detail["reader_html"]
    for retained in (
        "<h2>Release notes</h2>",
        "<p>",
        "<strong>important</strong>",
        "<blockquote>",
        "<ul>",
        "<li>",
        "<code>safe_call()</code>",
    ):
        assert retained in reader_html
    assert 'href="https://safe.example/docs"' in reader_html
    assert 'target="_blank"' in reader_html
    assert 'rel="noopener noreferrer"' in reader_html
    for removed in (
        "<script",
        "<style",
        "<svg",
        "<iframe",
        "<form",
        "<input",
        "onclick",
        "style=",
        "class=",
        "javascript:",
        'href="/relative"',
    ):
        assert removed not in reader_html


def test_short_and_existing_plain_text_entries_remain_source_first_without_reader(tmp_path: Path):
    url = "https://feeds.example/short"
    db_path = tmp_path / "feeds.db"
    with sqlite3.connect(db_path) as db:
        db.executescript(
            """
            CREATE TABLE feeds (
                url TEXT PRIMARY KEY,
                etag TEXT NOT NULL DEFAULT '',
                last_modified TEXT NOT NULL DEFAULT '',
                last_status TEXT NOT NULL DEFAULT '',
                last_error TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE entries (
                feed_url TEXT NOT NULL,
                entry_id TEXT NOT NULL,
                title TEXT NOT NULL,
                link TEXT NOT NULL,
                published TEXT NOT NULL,
                summary TEXT NOT NULL,
                PRIMARY KEY (feed_url, entry_id)
            );
            INSERT INTO entries VALUES (
                'https://feeds.example/legacy', 'legacy-1', 'Legacy item',
                'https://news.example/legacy', '', 'A legacy plain-text summary.'
            );
            """
        )
    short_feed = b"""<?xml version="1.0"?><rss version="2.0"><channel><title>Short</title>
    <item><guid>short-1</guid><title>Short item</title><link>https://news.example/short</link>
    <description><![CDATA[<p>Comments</p>]]></description></item></channel></rss>"""
    intake = FeedIntake(
        db_path,
        FixtureTransport({url: [Response(200, {}, short_feed)]}),
        check_url=allow_public,
    )

    before_refresh = intake.recent_entries_with_reader(limit=20)
    assert before_refresh[0]["entry_id"] == "legacy-1"
    assert before_refresh[0]["excerpt"] == "A legacy plain-text summary."
    assert before_refresh[0]["has_reader"] is False
    with sqlite3.connect(db_path) as db:
        reader_columns = {row[1] for row in db.execute("PRAGMA table_info(reader_bodies)").fetchall()}
        assert "reader_id" in reader_columns
        reader_indexes = {row[1] for row in db.execute("PRAGMA index_list(reader_bodies)").fetchall()}
        assert "reader_bodies_reader_id" in reader_indexes

    intake.refresh(url)
    by_id = {entry["entry_id"]: entry for entry in intake.recent_entries_with_reader(limit=20)}
    assert by_id["short-1"]["excerpt"] == "Comments"
    assert by_id["short-1"]["has_reader"] is False
    assert intake.reader_entry(by_id["short-1"]["reader_id"]) is None


def test_reader_id_migration_backfills_existing_reader_body_without_network(tmp_path: Path):
    db_path = tmp_path / "feeds.db"
    feed_url = "https://feeds.example/early-reader"
    with sqlite3.connect(db_path) as db:
        db.executescript(
            f"""
            CREATE TABLE feeds (
                url TEXT PRIMARY KEY, etag TEXT NOT NULL DEFAULT '', last_modified TEXT NOT NULL DEFAULT '',
                last_status TEXT NOT NULL DEFAULT '', last_error TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE entries (
                feed_url TEXT NOT NULL, entry_id TEXT NOT NULL, title TEXT NOT NULL, link TEXT NOT NULL,
                published TEXT NOT NULL, summary TEXT NOT NULL, PRIMARY KEY (feed_url, entry_id)
            );
            CREATE TABLE reader_bodies (
                feed_url TEXT NOT NULL, entry_id TEXT NOT NULL, content_version INTEGER NOT NULL,
                reader_html TEXT NOT NULL, PRIMARY KEY (feed_url, entry_id)
            );
            INSERT INTO entries VALUES (
                '{feed_url}', 'early-1', 'Early reader', 'https://news.example/early', '', 'Long fallback'
            );
            INSERT INTO reader_bodies VALUES (
                '{feed_url}', 'early-1', 1, '<h2>Preserved</h2><p>Reader body</p>'
            );
            """
        )

    intake = FeedIntake(db_path, FixtureTransport({}), check_url=allow_public)
    listed = intake.recent_entries_with_reader(limit=1)[0]
    detail = intake.reader_entry(str(listed["reader_id"]))

    assert listed["has_reader"] is True
    assert detail is not None
    assert detail["reader_html"] == "<h2>Preserved</h2><p>Reader body</p>"
    with sqlite3.connect(db_path) as db:
        assert db.execute("SELECT reader_id FROM reader_bodies").fetchone()[0] == listed["reader_id"]


@pytest.mark.parametrize(
    "structured",
    [
        "<p>" + ("x" * (128 * 1024)) + "</p>",
        "".join("<p>x</p>" for _ in range(5001)),
        ("<p>" + ("context " * 60) + "</p>")
        + "".join(f'<a href="https://safe.example/{index}">x</a>' for index in range(1001)),
    ],
    ids=["bytes", "nodes", "links"],
)
def test_structured_reader_body_fails_closed_when_any_independent_bound_is_exceeded(tmp_path: Path, structured: str):
    url = "https://feeds.example/oversize"
    body = f"""<?xml version="1.0"?><rss version="2.0"><channel><title>Oversize</title>
    <item><guid>oversize-1</guid><title>Oversize item</title><link>https://news.example/oversize</link>
    <description><![CDATA[{structured}]]></description></item></channel></rss>""".encode()
    intake = FeedIntake(
        tmp_path / "feeds.db",
        FixtureTransport({url: [Response(200, {}, body)]}),
        check_url=allow_public,
        max_bytes=len(body) + 1,
    )

    intake.refresh(url)

    listed = intake.recent_entries_with_reader(limit=20)
    assert listed[0]["entry_id"] == "oversize-1"
    assert listed[0]["excerpt"]
    assert listed[0]["has_reader"] is False
    assert intake.reader_entry(str(listed[0]["reader_id"])) is None


def test_plain_text_fallback_is_utf8_bounded_independently_from_reader_body(tmp_path: Path):
    url = "https://feeds.example/plain-bound"
    huge_text = "界" * 30000
    structured = f"<h2>Bounded fallback</h2><p>{huge_text}</p>"
    body = f"""<?xml version="1.0"?><rss version="2.0"><channel><title>Bounded</title>
    <item><guid>plain-bound-1</guid><title>Bounded item</title>
    <description><![CDATA[{structured}]]></description></item></channel></rss>""".encode()
    intake = FeedIntake(
        tmp_path / "feeds.db",
        FixtureTransport({url: [Response(200, {}, body)]}),
        check_url=allow_public,
        max_bytes=len(body) + 1,
    )

    intake.refresh(url)

    stored = intake.recent_entries(limit=1)[0]["summary"]
    assert len(stored.encode("utf-8")) <= 64 * 1024
    assert stored.endswith("…")
    listed = intake.recent_entries_with_reader(limit=1)[0]
    assert len(str(listed["excerpt"])) <= 400
    assert listed["has_reader"] is True


def test_reader_body_retention_is_independent_from_metadata_retention(tmp_path: Path):
    url = "https://feeds.example/retention"
    content = "<h2>Full item</h2><p>" + ("reader content " * 40) + "</p>"
    items = "".join(
        f"<item><guid>{index}</guid><title>Item {index}</title><description><![CDATA[{content}]]></description></item>"
        for index in range(25)
    )
    body = f'<rss version="2.0"><channel>{items}</channel></rss>'.encode()
    intake = FeedIntake(
        tmp_path / "feeds.db",
        FixtureTransport({url: [Response(200, {}, body)]}),
        check_url=allow_public,
        max_entries_per_feed=1000,
    )

    intake.refresh(url)

    listed = intake.recent_entries_with_reader(limit=100)
    assert len(listed) == 25
    assert sum(entry["has_reader"] for entry in listed) == 20
    assert intake.count_entries(feed_urls=[url]) == 25


def test_explicit_refresh_removes_stale_reader_when_same_entry_becomes_source_only(tmp_path: Path):
    url = "https://feeds.example/changes"
    long_content = "<h2>Full item</h2><p>" + ("reader content " * 40) + "</p>"
    long_body = f"""<rss version="2.0"><channel><item><guid>changing-1</guid><title>Changing</title>
    <link>https://news.example/changing</link><description><![CDATA[{long_content}]]></description></item></channel></rss>""".encode()
    short_body = b"""<rss version="2.0"><channel><item><guid>changing-1</guid><title>Changing</title>
    <link>https://news.example/changing</link><description>Comments</description></item></channel></rss>"""
    intake = FeedIntake(
        tmp_path / "feeds.db",
        FixtureTransport({url: [Response(200, {}, long_body), Response(200, {}, short_body)]}),
        check_url=allow_public,
    )

    intake.refresh(url)
    first = intake.recent_entries_with_reader(limit=1)[0]
    assert first["has_reader"] is True

    intake.refresh(url)
    second = intake.recent_entries_with_reader(limit=1)[0]
    assert second["entry_id"] == "changing-1"
    assert second["excerpt"] == "Comments"
    assert second["has_reader"] is False
    assert intake.reader_entry(str(second["reader_id"])) is None


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
    assert intake.refresh(url) == {
        "status": "updated",
        "processed": 1,
        "inserted": 0,
        "duplicates": 1,
        "redirects": 0,
    }
    assert intake.refresh(url) == {
        "status": "not_modified",
        "processed": 0,
        "inserted": 0,
        "duplicates": 0,
        "redirects": 0,
    }

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
        FixtureTransport({url: [Response(200, {}, body), Response(200, {}, body)]}),
        check_url=allow_public,
        max_entries_per_feed=2,
    )

    first = intake.refresh(url)
    assert first["inserted"] == 2
    assert first["duplicates"] == 0
    assert [entry["entry_id"] for entry in intake.recent_entries()] == ["one", "two"]

    second = intake.refresh(url)
    assert second["inserted"] == 0
    assert second["duplicates"] == 2
    assert [entry["entry_id"] for entry in intake.recent_entries()] == ["one", "two"]


def test_successful_refresh_limits_items_processed_before_retention(tmp_path: Path):
    url = "https://feeds.example/capped-refresh"
    body = b"""<?xml version="1.0"?><rss version="2.0"><channel><title>Capped</title>
    <item><guid>one</guid><title>One</title></item>
    <item><guid>two</guid><title>Two</title></item>
    <item><guid>three</guid><title>Three</title></item>
    </channel></rss>"""
    intake = FeedIntake(
        tmp_path / "feeds.db",
        FixtureTransport({url: [Response(200, {}, body)]}),
        check_url=allow_public,
        max_items_per_refresh=2,
        max_entries_per_feed=10,
    )

    result = intake.refresh(url)

    assert result["inserted"] == 2
    assert [entry["entry_id"] for entry in intake.recent_entries()] == ["one", "two"]


def test_intake_enforces_absolute_100_item_refresh_ceiling(tmp_path: Path):
    url = "https://feeds.example/hard-cap"
    items = "".join(f"<item><guid>{index}</guid><title>{index}</title></item>" for index in range(120))
    body = f'<rss version="2.0"><channel>{items}</channel></rss>'.encode()
    intake = FeedIntake(
        tmp_path / "feeds.db",
        FixtureTransport({url: [Response(200, {}, body)]}),
        check_url=allow_public,
        max_items_per_refresh=500,
        max_entries_per_feed=1000,
    )

    result = intake.refresh(url)

    assert result["processed"] == 100
    assert result["inserted"] == 100
    assert intake.count_entries(feed_urls=[url]) == 100


def test_refresh_persists_durable_attempt_details_and_migrates_existing_database(tmp_path: Path):
    url = "https://feeds.example/health"
    db_path = tmp_path / "feeds.db"
    with sqlite3.connect(db_path) as db:
        db.executescript(
            """
            CREATE TABLE feeds (
                url TEXT PRIMARY KEY,
                etag TEXT NOT NULL DEFAULT '',
                last_modified TEXT NOT NULL DEFAULT '',
                last_status TEXT NOT NULL DEFAULT '',
                last_error TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE entries (
                feed_url TEXT NOT NULL,
                entry_id TEXT NOT NULL,
                title TEXT NOT NULL,
                link TEXT NOT NULL,
                published TEXT NOT NULL,
                summary TEXT NOT NULL,
                PRIMARY KEY (feed_url, entry_id)
            );
            """
        )
    body = b"""<?xml version="1.0"?><rss version="2.0"><channel><title>Health</title>
    <item><guid>one</guid><title>One</title></item>
    <item><guid>two</guid><title>Two</title></item>
    <item><guid>three</guid><title>Three</title></item>
    </channel></rss>"""
    intake = FeedIntake(
        db_path,
        FixtureTransport({url: [Response(200, {}, body), Response(200, {}, body)]}),
        check_url=allow_public,
        max_items_per_refresh=2,
    )

    first = intake.refresh(url)
    first_status = intake.feed_status(url)
    second = intake.refresh(url)
    second_status = intake.feed_status(url)

    assert first == {"status": "updated", "processed": 2, "inserted": 2, "duplicates": 0, "redirects": 0}
    assert first_status["status"] == "updated"
    assert first_status["checked_at"]
    assert first_status | {"checked_at": "<timestamp>"} == {
        "status": "updated",
        "error": "",
        "checked_at": "<timestamp>",
        "processed": 2,
        "inserted": 2,
        "duplicates": 0,
    }
    assert second == {"status": "updated", "processed": 2, "inserted": 0, "duplicates": 2, "redirects": 0}
    assert second_status["checked_at"]
    assert second_status | {"checked_at": "<timestamp>"} == {
        "status": "updated",
        "error": "",
        "checked_at": "<timestamp>",
        "processed": 2,
        "inserted": 0,
        "duplicates": 2,
    }


def test_failed_refresh_persists_checked_time_and_clears_attempt_counts(tmp_path: Path):
    url = "https://feeds.example/error-health"
    intake = FeedIntake(
        tmp_path / "feeds.db",
        FixtureTransport({url: [Response(503, {}, b"unavailable")]}),
        check_url=allow_public,
    )

    with pytest.raises(FeedIntakeError):
        intake.refresh(url)

    status = intake.feed_status(url)
    assert status["checked_at"]
    assert status | {"checked_at": "<timestamp>", "error": "<error>"} == {
        "status": "error",
        "error": "<error>",
        "checked_at": "<timestamp>",
        "processed": 0,
        "inserted": 0,
        "duplicates": 0,
    }


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


def test_url_userinfo_is_rejected_before_egress_or_transport(tmp_path: Path):
    transport = FixtureTransport({})
    intake = FeedIntake(tmp_path / "feeds.db", transport, check_url=allow_public)

    with pytest.raises(FeedSafetyError, match="credentials"):
        intake.refresh("https://user:secret@feeds.example/rss")

    assert transport.calls == []


def test_malformed_feed_url_records_safety_failure(tmp_path: Path):
    transport = FixtureTransport({})
    intake = FeedIntake(tmp_path / "feeds.db", transport, check_url=allow_public)
    url = "https://[invalid/feed"

    with pytest.raises(FeedSafetyError, match="malformed"):
        intake.refresh(url)

    assert transport.calls == []
    assert intake.feed_status(url)["status"] == "error"


@pytest.mark.parametrize(
    "location",
    [
        "https://target.example:invalid/feed",
        "https://[invalid/feed",
        "//[invalid/feed",
    ],
)
def test_malformed_redirect_url_records_safety_failure(tmp_path: Path, location: str):
    url = "https://feeds.example/start"
    intake = FeedIntake(
        tmp_path / "feeds.db",
        FixtureTransport({url: [Response(302, {"location": location}, b"")]}),
        check_url=allow_public,
    )

    with pytest.raises(FeedSafetyError, match="malformed"):
        intake.refresh(url)

    assert intake.feed_status(url)["status"] == "error"


def test_http_transport_enforces_total_response_deadline():
    class SlowHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            for _ in range(6):
                self.wfile.write(b"x")
                self.wfile.flush()
                time.sleep(0.1)

        def log_message(self, format, *args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), SlowHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    started = time.monotonic()
    elapsed = None
    try:
        with pytest.raises(FeedIntakeError, match="deadline"):
            HttpxTransport().request(
                f"http://127.0.0.1:{server.server_port}/feed",
                {},
                timeout_seconds=0.2,
                max_bytes=1024,
            )
        elapsed = time.monotonic() - started
    finally:
        server.shutdown()
        server.server_close()
    assert elapsed is not None and elapsed < 0.55


def test_http_transport_deadline_includes_headers_and_body_wait():
    class SplitDelayHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            time.sleep(0.18)
            self.send_response(200)
            self.end_headers()
            time.sleep(0.18)
            self.wfile.write(b"x")
            self.wfile.flush()

        def log_message(self, format, *args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), SplitDelayHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    started = time.monotonic()
    elapsed = None
    try:
        with pytest.raises(FeedIntakeError, match="deadline"):
            HttpxTransport().request(
                f"http://127.0.0.1:{server.server_port}/feed",
                {},
                timeout_seconds=0.2,
                max_bytes=1024,
            )
        elapsed = time.monotonic() - started
    finally:
        server.shutdown()
        server.server_close()
    assert elapsed is not None and elapsed < 0.3


def test_http_transport_deadline_is_not_blocked_by_uncancellable_dns(monkeypatch):
    slow_dns = """
import socket
import time

def slow_getaddrinfo(*args, **kwargs):
    time.sleep(0.7)
    raise socket.gaierror("fixture DNS failure")

socket.getaddrinfo = slow_getaddrinfo
"""
    monkeypatch.setattr(HttpxTransport, "_WORKER", slow_dns + HttpxTransport._WORKER)
    children = []
    original_popen = subprocess.Popen

    def capture_popen(*args, **kwargs):
        process = original_popen(*args, **kwargs)
        children.append(process)
        return process

    monkeypatch.setattr(subprocess, "Popen", capture_popen)
    started = time.monotonic()
    with pytest.raises(FeedIntakeError, match="deadline"):
        HttpxTransport().request(
            "http://slow.invalid/feed",
            {},
            timeout_seconds=0.2,
            max_bytes=1024,
        )
    assert time.monotonic() - started < 0.35
    assert len(children) == 1
    assert children[0].poll() is not None


@pytest.mark.parametrize(
    "worker_result",
    [
        [],
        {"ok": "yes"},
        {"ok": True, "status": True, "headers": {}, "body": ""},
        {"ok": True, "status": 200, "headers": [], "body": ""},
        {
            "ok": True,
            "status": 200,
            "headers": {},
            "body": base64.b64encode(b"12345").decode("ascii"),
        },
    ],
)
def test_http_transport_rejects_invalid_worker_protocol(monkeypatch, worker_result):
    class FakeProcess:
        returncode = 0

        def communicate(self, payload=None, timeout=None):
            return json.dumps(worker_result), ""

        def kill(self):
            return None

    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: FakeProcess())
    with pytest.raises(FeedIntakeError, match="worker"):
        HttpxTransport().request(
            "https://feeds.example/rss",
            {},
            timeout_seconds=1,
            max_bytes=4,
        )


def test_http_transport_maps_worker_launch_failure(monkeypatch):
    def fail_launch(*args, **kwargs):
        raise OSError("fixture spawn failure")

    monkeypatch.setattr(subprocess, "Popen", fail_launch)
    with pytest.raises(FeedIntakeError, match="worker"):
        HttpxTransport().request(
            "https://feeds.example/rss",
            {},
            timeout_seconds=1,
            max_bytes=1024,
        )


def test_worker_cleanup_failure_does_not_override_fixed_error(monkeypatch):
    class CleanupFailureProcess:
        returncode = 0

        def __init__(self):
            self.communicate_calls = 0

        def communicate(self, payload=None, timeout=None):
            self.communicate_calls += 1
            raise OSError("fixture communicate failure")

        def kill(self):
            raise PermissionError("fixture kill failure")

    process = CleanupFailureProcess()
    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: process)
    with pytest.raises(FeedIntakeError) as raised:
        HttpxTransport().request(
            "https://feeds.example/rss",
            {},
            timeout_seconds=1,
            max_bytes=1024,
        )
    assert str(raised.value) == "feed request worker failed"
    assert process.communicate_calls == 2


@pytest.mark.parametrize(
    "worker_stdout",
    [
        "[" * 1100 + "]" * 1100,
        "1" * 5000,
    ],
)
def test_http_transport_contains_pathological_worker_json(monkeypatch, worker_stdout):
    class FakeProcess:
        returncode = 0

        def communicate(self, payload=None, timeout=None):
            return worker_stdout, ""

        def kill(self):
            return None

    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: FakeProcess())
    with pytest.raises(FeedIntakeError, match="worker returned invalid data"):
        HttpxTransport().request(
            "https://feeds.example/rss",
            {},
            timeout_seconds=1,
            max_bytes=1024,
        )


def test_http_transport_does_not_expose_worker_error_detail(monkeypatch):
    class FakeProcess:
        returncode = 0

        def communicate(self, payload=None, timeout=None):
            return '{"ok":false,"error":"httpx:SECRET_WORKER_DETAIL"}', ""

        def kill(self):
            return None

    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: FakeProcess())
    with pytest.raises(FeedIntakeError) as raised:
        HttpxTransport().request(
            "https://feeds.example/rss",
            {},
            timeout_seconds=1,
            max_bytes=1024,
        )
    assert str(raised.value) == "feed request worker returned invalid data"
    assert "SECRET" not in str(raised.value)


def test_http_transport_maps_real_http_failure_to_fixed_error():
    with pytest.raises(FeedIntakeError) as raised:
        HttpxTransport().request(
            "http://127.0.0.1:1/feed",
            {},
            timeout_seconds=5,
            max_bytes=1024,
        )
    assert str(raised.value) == "feed request failed"


def test_egress_check_is_bounded_by_refresh_deadline(tmp_path: Path, monkeypatch):
    url = "https://feeds.example/rss"
    transport = FixtureTransport({})
    slow_dns = """
import socket
import time


def slow_getaddrinfo(*args, **kwargs):
    time.sleep(0.7)
    raise socket.gaierror("fixture DNS failure")


socket.getaddrinfo = slow_getaddrinfo
"""
    policy = ProtoAgentEgressPolicy(Path(__file__).parents[2] / "protoAgent", [])
    monkeypatch.setattr(policy, "_WORKER", slow_dns + policy._WORKER)
    intake = FeedIntake(
        tmp_path / "feeds.db",
        transport,
        check_url=policy,
        timeout_seconds=0.2,
    )
    started = time.monotonic()
    with pytest.raises(FeedIntakeError, match="deadline"):
        intake.refresh(url)
    assert time.monotonic() - started < 0.35
    assert transport.calls == []


def test_protoagent_egress_policy_preserves_allowlist_and_private_ip_rules():
    root = Path(__file__).parents[2] / "protoAgent"
    assert (
        ProtoAgentEgressPolicy(root, ["internal.example"])("https://internal.example/feed", timeout_seconds=1) is None
    )
    blocked = ProtoAgentEgressPolicy(root, [])("http://127.0.0.1/feed", timeout_seconds=1)
    assert blocked is not None and "private/internal" in blocked


def test_protoagent_egress_policy_requires_complete_worker_schema(monkeypatch):
    class MissingFieldProcess:
        returncode = 0

        def communicate(self, payload=None, timeout=None):
            return '{"ok":true}', ""

        def kill(self):
            return None

    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: MissingFieldProcess())
    policy = ProtoAgentEgressPolicy(Path(__file__).parents[2] / "protoAgent", [])
    with pytest.raises(FeedIntakeError, match="invalid data"):
        policy("https://feeds.example/rss", timeout_seconds=1)


def test_malformed_entry_url_records_parse_failure(tmp_path: Path):
    url = "https://feeds.example/malformed-link"
    body = b"""<?xml version="1.0"?><rss version="2.0"><channel><title>Bad link</title>
    <item><guid>bad-link</guid><title>Bad link</title><link>https://news.example:invalid/item</link></item>
    </channel></rss>"""
    intake = FeedIntake(
        tmp_path / "feeds.db",
        FixtureTransport({url: [Response(200, {}, body)]}),
        check_url=allow_public,
    )

    with pytest.raises(FeedParseError, match="entry URL"):
        intake.refresh(url)

    assert intake.feed_status(url)["status"] == "error"


def test_refresh_deadline_is_shared_across_redirect_hops(tmp_path: Path):
    start = "https://feeds.example/start"
    target = "https://feeds.example/target"

    class SlowRedirectTransport:
        def request(self, url, headers, *, timeout_seconds, max_bytes):
            time.sleep(0.04)
            if url == start:
                return Response(302, {"location": target}, b"")
            return Response(200, {}, RSS)

    intake = FeedIntake(
        tmp_path / "feeds.db",
        SlowRedirectTransport(),
        check_url=allow_public,
        timeout_seconds=0.06,
    )

    with pytest.raises(FeedIntakeError, match="deadline"):
        intake.refresh(start)
