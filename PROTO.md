# RSS / Atom plugin contract

Status: v0.9.0 beta release contract.

## Owner

This standalone plugin owns operator-selected RSS/Atom sources, its packaged
maintained-feed catalogue, custom sources, bounded manual refresh, normalized
durable entries, HTTP validators, deduplication, per-feed storage bounds, compact
News excerpts, optional sanitized reader bodies, and refresh status.

protoAgent core owns plugin installation/loading, authenticated console handshake,
configuration persistence, and `security.egress` policy. The plugin requires
protoAgent 0.135.0 or newer. No RSS-specific protoAgent core behavior is required.

## Interface

The agent interface remains four bounded tools:

1. list configured feeds;
2. refresh one configured feed by name;
3. query bounded recent entries, optionally by feed/category;
4. report latest refresh status for one feed.

The operator interface is:

- **News**: category/source filtering, explicit source/category refresh, durable
  health, compact excerpts, and reader/source actions;
- **Manage sources**: packaged catalogue search/checkboxes and custom feed rows;
- generic plugin Configure: global intake/retention/time bounds and compatibility
  visibility for legacy feed rows.

Catalogue selections are persisted as immutable `selected_feed_ids`. Arbitrary
operator sources remain separate `custom_feeds`. Legacy `feeds` rows remain
readable; before the first selector save, exact current/previous catalogue URLs
are proposed as catalogue selections and unmatched rows are proposed as custom.
Saving sets `catalogue_configured` and writes only the three plugin-owned catalogue
keys. It does not erase legacy rows or modify unrelated settings.

Per-feed size accepts 1–2048 KiB and items accepts 1–100. Omitted values inherit
global defaults; 100 is an absolute intake ceiling. The eye choice is browser-local
and controls only the next selected/category refresh. It never mutates source
configuration or stored articles.

News returns only bounded excerpts and reader availability. Structured bodies are
available only from the bearer-protected detail API. The content-free News,
source-selector, help, and reader shells may load without an Authorization header
for normal iframe/navigation behavior; selector data, writes, News data, reader
data, and refresh endpoints remain authenticated.

## Catalogue lifecycle

`feed_catalog.json` is release-owned static data. Every record has:

- immutable lowercase kebab-case `id`;
- category and display name;
- current HTTPS URL;
- zero or more unique previous HTTP(S) URLs used only as exact local migration identities;
- optional bounded per-feed defaults.

Plugin updates may correct current URLs, names, categories, and safe defaults, mark
feeds deprecated, or add selectable sources. Updates never auto-select a new feed,
fetch a remote catalogue, refresh a feed, or delete custom sources/articles.

When a selected catalogue record changes URL, normal local data access performs an
atomic SQLite provenance migration from an exact previous URL to the current URL.
It moves validators, health, entries, and reader bodies and regenerates reader IDs.
It refuses an occupied destination and performs no network request. Opening or
saving the selector does not instantiate storage or perform this migration.

Selector saves late-bind protoAgent's `HOST.apply_settings` service because the
host populates it after plugin registration. The plugin issues one nested
`rss_atom` patch and lets protoAgent validate, persist, and hot-reload it. If that
host service is unavailable or reload fails, the route fails closed; it never
writes YAML directly.

Every public candidate is validated before catalogue inclusion. The release keeps
a bounded validation report with the configured/final URL, HTTPS/redirect result,
HTTP/content-type result, parse/version/entry count, duplicate detection, category
fit, and explicit include/exclude disposition. Validation evidence is release-time
proof, not a promise that an external source will remain available forever.

## Storage lifecycle

SQLite lives under an instance-scoped plugin data directory. Schema creation and
additive migrations are serialized and idempotent. Feed state is currently keyed
by URL; catalogue IDs provide stable configuration identity and the atomic URL
migration preserves durable state across maintained URL corrections.

Entries are unique by `(feed_url, entry_id)`. A successful refresh commits entries,
validators, and status together, then enforces configured retention. Each attempt
records checked time, processed/new/duplicate counts, status, and bounded error. A
failed refresh does not replace good validators or partially insert entries.

Disable and normal uninstall retain data. Existing rows require no network
migration. Plain-text fallback is capped at 64 KiB UTF-8; list excerpts are capped
at 400 characters. Sanitized bodies live separately and are capped at 128 KiB,
5,000 elements, 1,000 links, and the newest 20 meaningful bodies per feed while
metadata retention remains independently configurable.

## HTTP policy

- only operator-selected HTTP(S) sources are reachable; maintained catalogue
  current URLs are HTTPS;
- protoAgent `security.egress.check_url` checks every requested hop;
- redirects are manual, maximum three, and HTTPS-to-HTTP is refused;
- validators are removed on cross-origin redirects;
- credentials and arbitrary request headers are not accepted;
- timeout is a 1–60 second total refresh deadline;
- decompressed response body is bounded from 1 KiB to 2 MiB;
- each refresh processes 1–100 entries;
- non-2xx except 304 fails closed; retry/pacing is never automatic;
- malformed `feedparser` results are rejected completely;
- untrusted HTML becomes bounded plain text in list/tool output;
- meaningful structured content is sanitized by `nh3`/Ammonia into a strict
  structural allowlist;
- scripts, styles, SVG, forms, media/embeds, inline styles/classes/IDs, event
  handlers, relative links, and unsafe URL schemes are removed;
- structured bodies are populated only by explicit refresh; page loads,
  configuration saves, catalogue migration, install, and update never fetch a feed
  or source page.

## Compatibility and release

The manifest's `version` and `min_protoagent_version` must match
`compatibility.json`; CI enforces coherence. For each plugin release, the maintainer
must deliberately update or reaffirm the minimum against the currently accepted
pinned protoAgent version. v0.9.0 declares and tests `0.135.0`.

A public beta release requires:

- packaged catalogue and validation report;
- additive legacy/config/data migration tests, including zero-network proof;
- selector API and browser acceptance on desktop/mobile;
- existing reader/auth/manual-refresh regression suite;
- Ruff and Linux/native Windows/macOS CI;
- exact-head independent review;
- immutable semver tag and GitHub release notes.

## Exclusions

No read/unread state, save/clip, send/share, crawler, source-page extraction,
browser renderer, web search, scheduler, polling, automatic retry, notifications,
remote live catalogue, publishing, credentials, purge tool, or protoAgent core
change is part of v0.9.0 beta.
