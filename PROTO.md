# RSS / Atom plugin contract

Status: implementation candidate for RomeoRaven/protoAgent issue #23.

## Owner

This standalone plugin owns generic operator-selected RSS/Atom protocol intake:
configured sources, bounded manual refresh, normalized durable entries, stable
IDs, ETag/Last-Modified state, deduplication, per-feed storage bounds, compact
News excerpts, optional sanitized reader bodies, and refresh status.

protoAgent core owns plugin loading/configuration and `security.egress` policy.
protoPen remains parser/fixture precedent, not a runtime dependency or owner.

## Interface

The complete first-slice interface is four tools:

1. list configured feeds;
2. refresh one configured feed by name;
3. query bounded recent entries, optionally by feed;
4. report the latest refresh status for one feed.

Configured sources and intake bounds are operator input through protoAgent's
generic plugin settings UI. GUI feed rows use `Category | Name | URL`, optionally extended with
`| Max size KiB | Items per refresh`; legacy `Name | URL` rows become
`Uncategorized`, and the existing object YAML form remains compatible with
optional `category`, `max_feed_size_kib`, and `max_items_per_refresh` values.
Per-feed size accepts 1–2048 KiB and per-feed items accepts 1–100; omitted values
inherit global defaults and 100 is the absolute item ceiling. The plugin-owned News view
filters stored articles by category and source. A separate browser-persisted eye
toggle includes or skips each configured category feed for an explicit selected-
feed refresh; the server rejects empty, duplicate, unknown, or cross-category
refresh selections. News returns only bounded excerpts and reader availability;
the structured body is available from a separate detail route. The agent cannot
add, edit, remove, purge, or schedule feeds.

## Storage lifecycle

SQLite lives under an instance-scoped plugin data directory. Schema creation is
idempotent. Entries are unique by `(feed_url, entry_id)`. A successful refresh
commits entries, validators, and status together, then retains only the configured
number of entries from the feed's newest-first document order. Each attempt
durably records checked time, processed/new/duplicate counts, status, and bounded
error. A failed refresh does not replace good validators or partially insert entries.

Disable and uninstall retain data. Re-enable reuses it. Schema creation and the
additive feed-health and reader-body migrations are idempotent; no purge behavior
is introduced. Existing rows receive no reader body and require no migration-time
network access. Plain-text fallback is capped at 64 KiB UTF-8; list excerpts are
capped at 400 characters. Optional sanitized bodies live in a separate table and
are capped at 128 KiB, 5,000 elements, 1,000 links, and the newest 20 meaningful
bodies per feed while metadata retention remains independently configurable.
The body cap selects newest usable publication timestamps first and preserves
stable feed/storage order for entries without a usable timestamp. Legacy
`max_bytes` remains an exact bounded fallback unless `max_feed_size_kib` is
explicitly configured.

## HTTP policy

- only operator-configured HTTP(S) sources are reachable;
- pA `security.egress.check_url` checks every requested hop;
- redirects are manual, maximum three, and HTTPS-to-HTTP is refused;
- validators are removed on cross-origin redirects;
- no credentials or arbitrary headers are accepted;
- timeout is a 1–60 second total refresh deadline across redirect hops;
- decompressed response body is user-configurable from 1 KiB–2 MiB;
- each refresh processes 1–100 newest entries, from the feed override or global default;
- non-2xx except 304 fails closed; retry/background pacing is not automatic;
- `feedparser` bozo/malformed results are rejected completely;
- untrusted HTML becomes bounded plain text in tool results;
- meaningful full-content entries are sanitized by `nh3`/Ammonia into a strict
  structural allowlist: headings, paragraphs, lists, blockquotes, pre/code,
  emphasis, safe HTTP(S) links, breaks, and rules;
- scripts, styles, SVG, forms, media/embeds, inline styles/classes/IDs, event
  handlers, relative links, and unsafe URL schemes are removed;
- structured bodies are populated only by explicit refresh; list/detail page
  loads never fetch a feed or source page.

`security.egress` performs host resolution before each request. This is the host's
current SSRF contract; the plugin does not invent a parallel allowlist. It does
not claim socket-level DNS pinning against rebinding between that check and the
transport connection.

## Exclusions

No core edits, crawler, browser renderer, search engine, scheduler, LLM polling,
background surface, notifications, events, standalone console replacement, source
write-back, feed mutation tools, credentials, publication, release, or PC1/PC2 work.
