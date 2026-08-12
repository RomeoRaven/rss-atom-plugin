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

Configured sources and intake bounds are operator input through protoAgent's
generic plugin settings UI. GUI feed rows use `Category | Name | URL`; legacy
`Name | URL` rows become `Uncategorized`, and the existing object YAML form
remains compatible with an optional `category`. The plugin-owned News view
filters stored articles by category and source. A separate browser-persisted eye
toggle includes or skips each configured category feed for an explicit selected-
feed refresh; the server rejects empty, duplicate, unknown, or cross-category
refresh selections. The agent cannot add, edit, remove, purge, or schedule feeds.

## Storage lifecycle

SQLite lives under an instance-scoped plugin data directory. Schema creation is
idempotent. Entries are unique by `(feed_url, entry_id)`. A successful refresh
commits entries, validators, and status together, then retains only the configured
number of entries from the feed's newest-first document order. A failed refresh
records bounded status without replacing good validators or partially inserting entries.

Disable and uninstall retain data. Re-enable reuses it. Explicit purge and schema
migration beyond initial idempotent creation are not admitted in this slice.

## HTTP policy

- only operator-configured HTTP(S) sources are reachable;
- pA `security.egress.check_url` checks every requested hop;
- redirects are manual, maximum three, and HTTPS-to-HTTP is refused;
- validators are removed on cross-origin redirects;
- no credentials or arbitrary headers are accepted;
- timeout is a 1–60 second total refresh deadline across redirect hops;
- decompressed response body is user-configurable from 1 KiB–2 MiB;
- each refresh processes a user-configurable 1–1000 newest entries;
- non-2xx except 304 fails closed; retry/background pacing is not automatic;
- `feedparser` bozo/malformed results are rejected completely;
- untrusted HTML becomes bounded plain text in tool results.

`security.egress` performs host resolution before each request. This is the host's
current SSRF contract; the plugin does not invent a parallel allowlist. It does
not claim socket-level DNS pinning against rebinding between that check and the
transport connection.

## Exclusions

No core edits, crawler, browser renderer, search engine, scheduler, LLM polling,
background surface, notifications, events, standalone console replacement, source
write-back, feed mutation tools, credentials, publication, release, or PC1/PC2 work.
