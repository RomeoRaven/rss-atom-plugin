import base64
import json
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
