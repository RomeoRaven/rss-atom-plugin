# RSS / Atom Intake for protoAgent

A small, durable intake plugin for operator-configured RSS 2.0 and Atom feeds.
It safely refreshes one named feed on demand, normalizes entries with source
provenance, persists HTTP validators and deduplication state, and lets the agent
query recent entries and refresh status.

## Tools

- `rss_list_feeds`
- `rss_refresh_feed(name)`
- `rss_recent_entries(limit=20, name="")`
- `rss_feed_status(name)`

Feeds and bounded intake settings are operator configuration, not agent-mutable
state. In protoAgent, open **Plugins → RSS / Atom Intake → Configure**. Add one
feed per row using `Name | URL`, for example:

```text
protoAgent releases | https://github.com/protoLabsAI/protoAgent/releases.atom
```

The generic plugin settings dialog also controls:

- items processed from each fetched document (1–1000; default 100);
- recent items returned when no tool limit is supplied (1–100; default 20);
- durable entries retained per feed (1–10000; default 1000);
- maximum decompressed feed size in KiB (1–2048; default 256);
- total refresh timeout in seconds (1–60; default 15).

The object form remains accepted for existing config files:

```yaml
rss_atom:
  feeds:
    - name: Example
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

## Safety and lifecycle

- pA `security.egress.check_url` runs before every request and redirect.
- Redirects are manual and capped at three; HTTPS downgrade is refused.
- Cross-origin redirects do not receive stored ETag/Last-Modified validators.
- The configured timeout is one total refresh deadline across all redirect hops.
- Decompressed response bytes, query size, and stored entries per feed are bounded.
- Malformed feeds and request errors commit no partial entries and expose bounded error status.
- Feed HTML is reduced to plain text; scripts and styles are suppressed.
- Refreshes are operator-triggered and serialized in-process.
- Disable stops all activity because there is no background process.
- Disable/uninstall retains the SQLite data. No purge tool exists in this slice.

## Non-goals

No crawler, browser renderer, web search, News editorial policy, scheduler,
background polling, notifications, publishing, console UI, feed add/remove tool,
credentials, remediation, or protoAgent core change.

## Platform evidence

The CI matrix runs on Linux, native Windows, and macOS. Each job clones current
protoAgent, loads this repository through its real plugin loader, invokes the
configured-feed/list/status/recent/refresh tool paths with offline fixtures, and
runs the parser/storage/security tests. No live external feed is polled.

This repository is an implementation candidate, not a release. Installation,
live feed configuration, deployment, and publishing remain separate decisions.
