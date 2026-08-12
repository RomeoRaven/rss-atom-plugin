# RSS / Atom plugin contract

Status: implementation candidate for RomeoRaven/protoAgent issue #23.

## Owner

This standalone plugin owns generic operator-selected RSS/Atom protocol intake:
configured sources, bounded manual refresh, normalized durable entries, stable
IDs, ETag/Last-Modified state, deduplication, per-feed storage bounds, and
refresh status.

protoAgent core owns plugin loading/configuration and `security.egress` policy.
protoPen remains parser/fixture precedent, not a runtime dependency or owner.

## Interface

The complete first-slice interface is four tools:

1. list configured feeds;
2. refresh one configured feed by name;
3. query bounded recent entries, optionally by feed;
4. report the latest refresh status for one feed.

Configured sources are operator input. The agent cannot add, edit, remove, purge,
or schedule them.

## Storage lifecycle

SQLite lives under an instance-scoped plugin data directory. Schema creation is
idempotent. Entries are unique by `(feed_url, entry_id)`. A successful refresh
commits entries, validators, and status together, then retains only the newest
configured number of entries per feed. A failed refresh records bounded status
without replacing good validators or partially inserting entries.

Disable and uninstall retain data. Re-enable reuses it. Explicit purge and schema
migration beyond initial idempotent creation are not admitted in this slice.

## HTTP policy

- only operator-configured HTTP(S) sources are reachable;
- pA `security.egress.check_url` checks every requested hop;
- redirects are manual, maximum three, and HTTPS-to-HTTP is refused;
- validators are removed on cross-origin redirects;
- no credentials or arbitrary headers are accepted;
- timeout is 1–60 seconds; decompressed response body is 1 KiB–2 MiB;
- non-2xx except 304 fails closed; retry/background pacing is not automatic;
- `feedparser` bozo/malformed results are rejected completely;
- untrusted HTML becomes bounded plain text in tool results.

`security.egress` performs host resolution before each request. This is the host's
current SSRF contract; the plugin does not invent a parallel allowlist. It does
not claim socket-level DNS pinning against rebinding between that check and the
transport connection.

## Exclusions

No core edits, News plugin, crawler, browser, search engine, scheduler, LLM polling,
background surface, notifications, events, console view, source write-back,
feed mutation tools, credentials, publication, deployment, release, or PC1/PC2 work.
