from __future__ import annotations

import base64
import calendar
import hashlib
import html
import inspect
import json
import sqlite3
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Callable, Protocol
from urllib.parse import urljoin, urlsplit, urlunsplit

import feedparser

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


def _cleanup_worker(process: subprocess.Popen[str]) -> None:
    killed = False
    try:
        process.kill()
        killed = True
    except OSError:
        pass
    try:
        process.communicate(timeout=None if killed else 0)
    except (OSError, subprocess.TimeoutExpired):
        pass


def _run_worker(worker: str, payload: str, *, timeout_seconds: float, label: str) -> str:
    started = time.monotonic()
    try:
        process = subprocess.Popen(
            [sys.executable, "-c", worker],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
    except OSError as exc:
        raise FeedIntakeError(f"{label} worker failed") from exc
    try:
        remaining = timeout_seconds - (time.monotonic() - started)
        if remaining <= 0:
            _cleanup_worker(process)
            raise FeedIntakeError("feed refresh exceeded total deadline")
        stdout, _ = process.communicate(payload, timeout=remaining)
    except subprocess.TimeoutExpired as exc:
        _cleanup_worker(process)
        raise FeedIntakeError("feed refresh exceeded total deadline") from exc
    except OSError as exc:
        _cleanup_worker(process)
        raise FeedIntakeError(f"{label} worker failed") from exc
    if process.returncode != 0:
        raise FeedIntakeError(f"{label} worker failed")
    return stdout


class ProtoAgentEgressPolicy:
    _WORKER = r"""
import json
import sys

request = json.loads(sys.stdin.read())
sys.path.insert(0, request["module_root"])
try:
    from security import egress
    egress.set_allowed_hosts(request["allowed_hosts"])
    result = {"ok": True, "blocked": egress.check_url(request["url"])}
except BaseException:
    result = {"ok": False}
sys.stdout.write(json.dumps(result, separators=(",", ":")))
"""

    def __init__(self, module_root: str | Path, allowed_hosts: list[str]) -> None:
        self.module_root = str(Path(module_root).resolve())
        self.allowed_hosts = [str(host) for host in allowed_hosts]

    def __call__(self, url: str, *, timeout_seconds: float) -> str | None:
        payload = json.dumps(
            {
                "url": url,
                "module_root": self.module_root,
                "allowed_hosts": self.allowed_hosts,
            },
            separators=(",", ":"),
        )
        stdout = _run_worker(
            self._WORKER,
            payload,
            timeout_seconds=timeout_seconds,
            label="feed egress",
        )
        try:
            result = json.loads(stdout)
        except (json.JSONDecodeError, TypeError, ValueError, RecursionError) as exc:
            raise FeedIntakeError("feed egress worker returned invalid data") from exc
        if not isinstance(result, dict) or set(result) != {"ok", "blocked"} or result.get("ok") is not True:
            raise FeedIntakeError("feed egress worker returned invalid data")
        blocked = result.get("blocked")
        if blocked is not None and not isinstance(blocked, str):
            raise FeedIntakeError("feed egress worker returned invalid data")
        return blocked


class HttpxTransport:
    _WORKER = r"""
import base64
import json
import sys
import httpx

request = json.loads(sys.stdin.read())
try:
    with httpx.Client(follow_redirects=False, timeout=request["timeout_seconds"], trust_env=False) as client:
        with client.stream("GET", request["url"], headers=request["headers"]) as response:
            declared = response.headers.get("content-length")
            if declared:
                try:
                    if int(declared) > request["max_bytes"]:
                        raise RuntimeError("too_large")
                except ValueError:
                    raise RuntimeError("invalid_content_length")
            body = bytearray()
            for chunk in response.iter_bytes():
                body.extend(chunk)
                if len(body) > request["max_bytes"]:
                    raise RuntimeError("too_large")
            result = {
                "ok": True,
                "status": response.status_code,
                "headers": dict(response.headers),
                "body": base64.b64encode(bytes(body)).decode("ascii"),
            }
except RuntimeError as exc:
    result = {"ok": False, "error": str(exc)}
except httpx.HTTPError:
    result = {"ok": False, "error": "httpx"}
except BaseException:
    result = {"ok": False, "error": "internal"}
sys.stdout.write(json.dumps(result, separators=(",", ":")))
"""

    def request(self, url: str, headers: dict[str, str], *, timeout_seconds: float, max_bytes: int) -> Response:
        payload = json.dumps(
            {
                "url": url,
                "headers": headers,
                "timeout_seconds": timeout_seconds,
                "max_bytes": max_bytes,
            },
            separators=(",", ":"),
        )
        stdout = _run_worker(
            self._WORKER,
            payload,
            timeout_seconds=timeout_seconds,
            label="feed request",
        )
        try:
            result = json.loads(stdout)
        except (json.JSONDecodeError, TypeError, ValueError, RecursionError) as exc:
            raise FeedIntakeError("feed request worker returned invalid data") from exc
        if not isinstance(result, dict) or type(result.get("ok")) is not bool:
            raise FeedIntakeError("feed request worker returned invalid data")
        if not result["ok"]:
            error = result.get("error")
            if not isinstance(error, str):
                raise FeedIntakeError("feed request worker returned invalid data")
            if error == "too_large":
                raise FeedTooLargeError(f"feed exceeds {max_bytes} byte limit")
            if error == "invalid_content_length":
                raise FeedIntakeError("invalid Content-Length")
            if error == "httpx":
                raise FeedIntakeError("feed request failed")
            if error == "internal":
                raise FeedIntakeError("feed request worker failed")
            raise FeedIntakeError("feed request worker returned invalid data")
        try:
            encoded_body = result["body"]
            status = result["status"]
            raw_headers = result["headers"]
            if not isinstance(encoded_body, str):
                raise TypeError
            if type(status) is not int or not 100 <= status <= 599:
                raise TypeError
            if not isinstance(raw_headers, dict) or not all(
                isinstance(key, str) and isinstance(value, str) for key, value in raw_headers.items()
            ):
                raise TypeError
            body = base64.b64decode(encoded_body, validate=True)
            if len(body) > max_bytes:
                raise TypeError
        except (KeyError, TypeError, ValueError) as exc:
            raise FeedIntakeError("feed request worker returned invalid data") from exc
        return Response(status, raw_headers, body)


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
        check_url: Callable[..., str | None],
        max_bytes: int = 256 * 1024,
        timeout_seconds: float = 15.0,
        max_entries_per_feed: int = 1000,
    ) -> None:
        self.db_path = Path(db_path)
        self.transport = transport
        parameters = inspect.signature(check_url).parameters
        if "timeout_seconds" in parameters:
            self.check_url = check_url
        else:
            self.check_url = lambda url, *, timeout_seconds: check_url(url)
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
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise FeedIntakeError("feed refresh exceeded total deadline")
            blocked = self.check_url(current, timeout_seconds=remaining)
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
            try:
                target = urljoin(current, location)
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
        duplicates = 0
        with self._connect() as db:
            existing = [
                dict(row)
                for row in db.execute(
                    "SELECT entry_id, feed_url, title, link, published, summary "
                    "FROM entries WHERE feed_url = ? ORDER BY rowid DESC",
                    (feed_url,),
                ).fetchall()
            ]
            existing_ids = {entry["entry_id"] for entry in existing}
            retained: list[dict[str, str]] = []
            retained_ids: set[str] = set()
            for entry in [*normalized, *existing]:
                entry_id = entry["entry_id"]
                if entry_id in retained_ids:
                    continue
                retained.append(entry)
                retained_ids.add(entry_id)
                if len(retained) == self.max_entries_per_feed:
                    break
            inserted = sum(1 for entry in retained if entry["entry_id"] not in existing_ids)
            duplicates = sum(1 for entry in retained if entry["entry_id"] in existing_ids)
            db.execute("DELETE FROM entries WHERE feed_url = ?", (feed_url,))
            for entry in reversed(retained):
                db.execute(
                    "INSERT INTO entries(feed_url, entry_id, title, link, published, summary) "
                    "VALUES(:feed_url, :entry_id, :title, :link, :published, :summary)",
                    entry,
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
            "duplicates": duplicates,
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
