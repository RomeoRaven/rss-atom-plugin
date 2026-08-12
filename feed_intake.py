from __future__ import annotations

import calendar
import hashlib
import html
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Callable, Protocol
from urllib.parse import urljoin, urlsplit, urlunsplit

import feedparser
import httpx

_DB_LOCKS: dict[str, threading.RLock] = {}
_DB_LOCKS_GUARD = threading.Lock()


def _db_lock(path: Path) -> threading.RLock:
    key = str(path.expanduser().resolve())
    with _DB_LOCKS_GUARD:
        return _DB_LOCKS.setdefault(key, threading.RLock())


class FeedIntakeError(RuntimeError):
    pass


class FeedSafetyError(FeedIntakeError):
    pass


class FeedParseError(FeedIntakeError):
    pass


class FeedTooLargeError(FeedIntakeError):
    pass


@dataclass(frozen=True)
class Response:
    status: int
    headers: dict[str, str]
    body: bytes


class Transport(Protocol):
    def request(self, url: str, headers: dict[str, str], *, timeout_seconds: float, max_bytes: int) -> Response: ...


class HttpxTransport:
    def request(self, url: str, headers: dict[str, str], *, timeout_seconds: float, max_bytes: int) -> Response:
        deadline = time.monotonic() + timeout_seconds
        try:
            with httpx.Client(follow_redirects=False, timeout=timeout_seconds) as client:
                with client.stream("GET", url, headers=headers) as response:
                    declared = response.headers.get("content-length")
                    if declared:
                        try:
                            if int(declared) > max_bytes:
                                raise FeedTooLargeError(f"feed exceeds {max_bytes} byte limit")
                        except ValueError as exc:
                            raise FeedIntakeError("invalid Content-Length") from exc
                    body = bytearray()
                    for chunk in response.iter_bytes():
                        if time.monotonic() > deadline:
                            raise FeedIntakeError("feed response exceeded total deadline")
                        body.extend(chunk)
                        if len(body) > max_bytes:
                            raise FeedTooLargeError(f"feed exceeds {max_bytes} byte limit")
                    return Response(response.status_code, dict(response.headers), bytes(body))
        except FeedIntakeError:
            raise
        except httpx.HTTPError as exc:
            raise FeedIntakeError(f"feed request failed: {type(exc).__name__}") from exc


class _PlainText(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.suppressed = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag.lower() in {"script", "style"}:
            self.suppressed += 1

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in {"script", "style"} and self.suppressed:
            self.suppressed -= 1

    def handle_data(self, data: str) -> None:
        if not self.suppressed:
            self.parts.append(data)


def _plain_text(value: str) -> str:
    parser = _PlainText()
    parser.feed(value or "")
    parser.close()
    return " ".join(html.unescape(" ".join(parser.parts)).split())


def _canonical_url(value: str) -> str:
    if not value:
        return ""
    parts = urlsplit(value)
    host = (parts.hostname or "").lower()
    netloc = host
    if parts.port:
        netloc = f"{host}:{parts.port}"
    return urlunsplit((parts.scheme.lower(), netloc, parts.path or "", parts.query, ""))


def _published_iso(entry) -> str:
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    if parsed:
        return datetime.fromtimestamp(calendar.timegm(parsed), tz=UTC).isoformat().replace("+00:00", "Z")
    return str(entry.get("published") or entry.get("updated") or "")


class FeedIntake:
    def __init__(
        self,
        db_path: str | Path,
        transport: Transport,
        *,
        check_url: Callable[[str], str | None],
        max_bytes: int = 256 * 1024,
        timeout_seconds: float = 15.0,
        max_entries_per_feed: int = 1000,
    ) -> None:
        self.db_path = Path(db_path)
        self.transport = transport
        self.check_url = check_url
        self.max_bytes = max_bytes
        self.timeout_seconds = timeout_seconds
        self.max_entries_per_feed = max_entries_per_feed
        self._lock = _db_lock(self.db_path)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        return connection

    def _init_db(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS feeds (
                    url TEXT PRIMARY KEY,
                    etag TEXT NOT NULL DEFAULT '',
                    last_modified TEXT NOT NULL DEFAULT '',
                    last_status TEXT NOT NULL DEFAULT '',
                    last_error TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS entries (
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

    def _normalize(self, feed_url: str, body: bytes) -> list[dict[str, str]]:
        parsed = feedparser.parse(body)
        if getattr(parsed, "bozo", 0):
            raise FeedParseError(f"malformed feed: {type(parsed.bozo_exception).__name__}")
        entries: list[dict[str, str]] = []
        for item in parsed.entries:
            try:
                link = _canonical_url(str(item.get("link") or ""))
            except ValueError as exc:
                raise FeedParseError("malformed entry URL") from exc
            title = _plain_text(str(item.get("title") or ""))
            published = _published_iso(item)
            summary_source = item.get("summary") or item.get("description") or ""
            if not summary_source and item.get("content"):
                summary_source = item.content[0].get("value", "")
            raw_id = str(item.get("id") or item.get("guid") or "").strip()
            entry_id = raw_id or hashlib.sha256("\x1f".join((feed_url, link, title, published)).encode()).hexdigest()
            entries.append(
                {
                    "entry_id": entry_id,
                    "feed_url": feed_url,
                    "title": title,
                    "link": link,
                    "published": published,
                    "summary": _plain_text(str(summary_source)),
                }
            )
        return entries

    def _validators(self, feed_url: str) -> dict[str, str]:
        with self._connect() as db:
            row = db.execute("SELECT etag, last_modified FROM feeds WHERE url = ?", (feed_url,)).fetchone()
        headers = {"User-Agent": "protoAgent-rss-atom/0.1"}
        if row and row["etag"]:
            headers["If-None-Match"] = row["etag"]
        if row and row["last_modified"]:
            headers["If-Modified-Since"] = row["last_modified"]
        return headers

    def _fetch(self, feed_url: str, headers: dict[str, str]) -> tuple[Response, int]:
        current = feed_url
        request_headers = dict(headers)
        redirects = 0
        deadline = time.monotonic() + self.timeout_seconds
        while True:
            try:
                parts = urlsplit(current)
                current_port = parts.port
            except ValueError as exc:
                raise FeedSafetyError("malformed feed URL") from exc
            if parts.scheme.lower() not in {"http", "https"} or not parts.hostname:
                raise FeedSafetyError("feed URL must use HTTP or HTTPS and include a host")
            if parts.username is not None or parts.password is not None:
                raise FeedSafetyError("feed URL must not include credentials")
            blocked = self.check_url(current)
            if blocked:
                raise FeedSafetyError("feed URL blocked by protoAgent egress policy")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise FeedIntakeError("feed refresh exceeded total deadline")
            response = self.transport.request(
                current,
                request_headers,
                timeout_seconds=remaining,
                max_bytes=self.max_bytes,
            )
            if time.monotonic() > deadline:
                raise FeedIntakeError("feed refresh exceeded total deadline")
            lowered = {str(key).lower(): str(value) for key, value in response.headers.items()}
            response = Response(response.status, lowered, response.body)
            declared = response.headers.get("content-length")
            if declared:
                try:
                    if int(declared) > self.max_bytes:
                        raise FeedTooLargeError(f"feed exceeds {self.max_bytes} byte limit")
                except ValueError as exc:
                    raise FeedIntakeError("invalid Content-Length") from exc
            if len(response.body) > self.max_bytes:
                raise FeedTooLargeError(f"feed exceeds {self.max_bytes} byte limit")
            if response.status not in {301, 302, 303, 307, 308}:
                return response, redirects
            location = response.headers.get("location", "")
            if not location:
                raise FeedIntakeError("redirect response omitted Location")
            redirects += 1
            if redirects > 3:
                raise FeedSafetyError("too many feed redirects")
            target = urljoin(current, location)
            try:
                target_parts = urlsplit(target)
                target_port = target_parts.port
            except ValueError as exc:
                raise FeedSafetyError("malformed redirect URL") from exc
            if parts.scheme.lower() == "https" and target_parts.scheme.lower() != "https":
                raise FeedSafetyError("HTTPS downgrade redirect refused")
            current_origin = (parts.scheme.lower(), parts.hostname, current_port)
            target_origin = (target_parts.scheme.lower(), target_parts.hostname, target_port)
            if current_origin != target_origin:
                request_headers = {
                    key: value
                    for key, value in request_headers.items()
                    if key.lower() not in {"if-none-match", "if-modified-since"}
                }
            current = target

    def _refresh(self, feed_url: str) -> dict[str, int | str]:
        response, redirects = self._fetch(feed_url, self._validators(feed_url))
        if response.status == 304:
            with self._connect() as db:
                db.execute(
                    "INSERT INTO feeds(url, last_status) VALUES(?, 'not_modified') "
                    "ON CONFLICT(url) DO UPDATE SET last_status='not_modified', last_error=''",
                    (feed_url,),
                )
            return {
                "status": "not_modified",
                "inserted": 0,
                "duplicates": 0,
                "redirects": redirects,
            }
        if not 200 <= response.status < 300:
            raise FeedIntakeError(f"feed request failed with HTTP {response.status}")
        normalized = self._normalize(feed_url, response.body)
        inserted = 0
        with self._connect() as db:
            for entry in reversed(normalized):
                inserted += db.execute(
                    "INSERT OR IGNORE INTO entries(feed_url, entry_id, title, link, published, summary) "
                    "VALUES(:feed_url, :entry_id, :title, :link, :published, :summary)",
                    entry,
                ).rowcount
            db.execute(
                "DELETE FROM entries WHERE feed_url = ? AND rowid NOT IN "
                "(SELECT rowid FROM entries WHERE feed_url = ? ORDER BY rowid DESC LIMIT ?)",
                (feed_url, feed_url, self.max_entries_per_feed),
            )
            db.execute(
                "INSERT INTO feeds(url, etag, last_modified, last_status) VALUES(?, ?, ?, 'updated') "
                "ON CONFLICT(url) DO UPDATE SET etag=excluded.etag, "
                "last_modified=excluded.last_modified, last_status='updated', last_error=''",
                (
                    feed_url,
                    response.headers.get("etag", ""),
                    response.headers.get("last-modified", ""),
                ),
            )
        return {
            "status": "updated",
            "inserted": inserted,
            "duplicates": len(normalized) - inserted,
            "redirects": redirects,
        }

    def refresh(self, feed_url: str) -> dict[str, int | str]:
        with self._lock:
            try:
                return self._refresh(feed_url)
            except FeedIntakeError as exc:
                with self._connect() as db:
                    db.execute(
                        "INSERT INTO feeds(url, last_status, last_error) VALUES(?, 'error', ?) "
                        "ON CONFLICT(url) DO UPDATE SET last_status='error', last_error=excluded.last_error",
                        (feed_url, f"{type(exc).__name__}: {exc}"[:500]),
                    )
                raise

    def feed_status(self, feed_url: str) -> dict[str, str]:
        with self._connect() as db:
            row = db.execute("SELECT last_status, last_error FROM feeds WHERE url = ?", (feed_url,)).fetchone()
        if not row:
            return {"status": "never_refreshed", "error": ""}
        return {"status": row["last_status"], "error": row["last_error"]}

    def recent_entries(self, limit: int = 20, feed_url: str = "") -> list[dict[str, str]]:
        where = "WHERE feed_url = ?" if feed_url else ""
        params: tuple[str | int, ...] = (feed_url, limit) if feed_url else (limit,)
        with self._connect() as db:
            rows = db.execute(
                "SELECT entry_id, feed_url, title, link, published, summary "
                f"FROM entries {where} ORDER BY rowid DESC LIMIT ?",
                params,
            ).fetchall()
        return [dict(row) for row in rows]
