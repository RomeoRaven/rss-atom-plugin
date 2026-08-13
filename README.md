# RSS / Atom Intake for protoAgent

A small, durable intake plugin for operator-configured RSS 2.0 and Atom feeds.
It safely refreshes one named feed on demand, normalizes entries with source
provenance, persists HTTP validators and deduplication state, and lets the agent
query recent entries and refresh status.

## Quick start

1. Open **Plugins → RSS / Atom Intake → Configure**.
2. Add one feed per row as `Category | Name | URL`. Optional per-feed limits use
   `Category | Name | URL | Max size KiB | Items per refresh`.
3. Open **News** from the left rail and choose a category.
4. Click a feed name to show only that source's stored articles.
5. Use the eye to include or skip that source for bulk **Refresh selected**.
6. Use the adjacent refresh icon to fetch only that source immediately.
7. Click **Refresh selected** only when you want to fetch every included feed in
   the category.
8. Use **Read here** for long structured entries or **Open source** to leave the
   plugin. Short and link-only entries remain source-first.

Adding a row configures a source; it does not add or refresh articles by itself.
Removing a row stops it appearing in News but does not purge previously stored
entries from the plugin database.

## Tools

- `rss_list_feeds(category="")`
- `rss_refresh_feed(name)`
- `rss_recent_entries(limit=20, name="", category="")`
- `rss_feed_status(name)`

Feeds and bounded intake settings are operator configuration, not agent-mutable
state. In protoAgent, open **Plugins → RSS / Atom Intake → Configure**. Add one
feed per row using `Category | Name | URL`, for example:

```text
Developer | protoAgent releases | https://github.com/protoLabsAI/protoAgent/releases.atom
```

To override one source without changing the global defaults, add both optional
columns. Either optional value may be blank:

```text
Developer | Hermes Agent releases | https://github.com/NousResearch/hermes-agent/releases.atom | 1280 | 10
Developer | protoAgent releases | https://github.com/protoLabsAI/protoAgent/releases.atom | | 10
```

Feed-size overrides accept 1–2048 KiB. Item-count overrides accept 1–100; 100 is
an absolute per-source ceiling. Existing `Category | Name | URL` and `Name | URL`
rows remain valid, inherit global defaults, and legacy two-field rows appear under
**Uncategorized**.
Enabled plugins also contribute a **News** rail view. Its category selector
filters configured sources and stored articles. Within a category, **All feeds**
or a feed-name button filters stored results by source. The eye button independently
includes or skips that feed for bulk **Refresh selected**; crossed-out eyes are
skipped. The separate refresh icon refreshes only that source, regardless of its
eye state. Those bulk-refresh choices persist in that browser and never delete or
alter configured feeds. Every refresh remains explicit and operator-triggered.
Selecting one source also shows durable health: last-check time, whether the
source worked or failed, new/already-stored counts, stored article count, and the
effective size/item limits. This distinguishes an untested source, no new items,
an unchanged source, an empty source, and a failed refresh.

News never renders a complete article body in the scrolling list. It shows a
bounded plain-text excerpt (at most 400 characters and five visual lines), keeps
the canonical source action visible, and offers **Read here** only when an
explicit refresh stored meaningful structured content. The separate reader
preserves safe headings, paragraphs, lists, blockquotes, code, emphasis, and
HTTP(S) links. Existing plain-text rows remain readable as excerpts and source
links without migration-time network access.

## Optional feed ideas

These examples are not installed automatically. Copy only the rows you want into
the **Feeds** setting, then use News to refresh them explicitly.

```text
Developer | Google Developers | https://developers.googleblog.com/feeds/posts/default?alt=rss
Science | CDC Emerging Infectious Diseases | https://wwwnc.cdc.gov/eid/rss/ahead-of-print.xml
```

Feed payloads and server behavior can change. If an example later fails, check
the source's official feed page and the troubleshooting section below before
raising intake limits.

The generic plugin settings dialog also controls:

- items processed from each fetched document (1–100; default 100);
- recent items returned when no tool limit is supplied (1–100; default 20);
- durable entries retained per feed (1–10000; default 1000);
- maximum decompressed feed size in KiB (1–2048; default 256);
- total refresh timeout in seconds (1–60; default 15).

The size setting applies to one decompressed RSS/Atom response, not to the plugin
package, configured-feed count, category size, or durable database. The fetcher
checks a declared response size when available and otherwise stops streaming as
soon as the configured ceiling is crossed. Increase it only for a trusted feed
whose current payload genuinely requires more room.

The object form remains accepted for existing config files:

```yaml
rss_atom:
  feeds:
    - category: Technology
      name: Example
      url: https://example.com/feed.xml
  max_items_per_refresh: 100
  default_recent_items: 20
  max_feed_size_kib: 256
  timeout_seconds: 15
  max_entries_per_feed: 1000
plugins:
  enabled: [rss_atom]
```

State defaults to `$PROTOAGENT_HOME/rss_atom/feeds.db`. Set
`RSS_ATOM_DATA_DIR` to choose another instance-scoped location.

Existing configurations that still define `max_bytes` remain effective until
`max_feed_size_kib` is explicitly set; the newer KiB setting takes precedence.

## Safety and lifecycle

- pA `security.egress.check_url` runs before every request and redirect.
- Redirects are manual and capped at three; HTTPS downgrade is refused.
- Cross-origin redirects do not receive stored ETag/Last-Modified validators.
- The configured timeout is one total refresh deadline across all redirect hops.
- Decompressed response bytes, query size, and stored entries per feed are bounded.
- Malformed feeds and request errors commit no partial entries and expose bounded error status.
- Plain-text fallback is UTF-8 bounded to 64 KiB and list excerpts to 400 characters.
- Optional reader HTML is sanitized by `nh3`/Ammonia with a strict structural
  allowlist. Scripts, styles, SVG, forms, media/embeds, inline styles/classes/IDs,
  event handlers, relative links, and non-HTTP(S) URL schemes are removed.
- Reader bodies are independently capped at 128 KiB, 5,000 elements, 1,000 links,
  and the newest 20 meaningful bodies per feed by publication time. Entries
  without a usable publication time retain stable feed/storage order. Metadata
  retention remains at the configured value (default 1,000 entries per feed).
- New reader bodies are created only by explicit later refreshes. No migration,
  configuration change, page load, or reader action fetches a feed or source page.
- Refreshes are operator-triggered and serialized in-process.
- Disable stops all activity because there is no background process.
- Disable/uninstall retains the SQLite data. No purge tool exists in this slice.

## Troubleshooting

### Feed exceeds the configured size limit

Confirm that the URL is an RSS/Atom document rather than a full web page. If it
is a trusted feed and its decompressed XML is legitimately larger than the
current ceiling, set a conservative per-feed size override in its **Feeds** row. This preserves the
smaller global default for every other source. The accepted range is 1–2048 KiB.

### Feed request is blocked or fails

The plugin applies protoAgent's egress policy to the original URL and every
redirect. A source may also reject automated readers, return a challenge page,
redirect from HTTPS to HTTP, or exceed the total timeout. The error is recorded
for that feed; no partial entries are committed.

### Refresh succeeds but no articles appear

Select the source and read its durable health panel. It reports whether the source
has never been checked, worked with no new items, returned no entries, was
unchanged, or failed. The eye only controls bulk refresh; the source's refresh
icon always tests that source directly. A successful refresh can insert zero
articles when every returned item is already stored.

## Non-goals

No crawler, browser renderer, web search, News editorial policy, scheduler,
background polling, notifications, publishing, standalone console replacement,
feed add/remove tool, credentials, remediation, or protoAgent core change.

## Platform evidence

The CI matrix runs on Linux, native Windows, and macOS. Each job clones current
protoAgent, loads this repository through its real plugin loader, invokes the
configured-feed/list/status/recent/refresh tool paths with offline fixtures, and
runs the parser/storage/security tests. No live external feed is polled.

This repository is an implementation candidate, not a release. Installation,
live feed configuration, deployment, and publishing remain separate decisions.

## License

[MIT](LICENSE)
