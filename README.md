# RSS / Atom Intake for protoAgent

A manual, bounded RSS 2.0 and Atom reader for protoAgent. It provides a maintained
feed catalogue, custom sources, durable feed health, compact News cards, and a
strictly sanitized structured reader. It never polls, schedules, or refreshes in
the background.

Current release: **v0.9.0 beta**. Minimum protoAgent: **0.135.0**.

## Install

Only install plugins you trust. protoAgent plugins run in-process with the agent.

### Console

1. Open **Plugins → Local** and choose **Download**.
2. Enter `https://github.com/RomeoRaven/rss-atom-plugin` and release ref `v0.9.0`.
3. Review the manifest and capabilities, then install.
4. Install the plugin's declared dependencies when protoAgent offers that action.
5. Enable **RSS / Atom Intake**. Restart if protoAgent recommends it for the new
   console views.
6. Open **News → Manage sources**.

### CLI

```sh
protoagent plugin install https://github.com/RomeoRaven/rss-atom-plugin --ref v0.9.0
protoagent plugin install-deps rss_atom
```

CLI installation fetches and pins code but does not enable it. Add `rss_atom` to
`plugins.enabled` through your normal protoAgent configuration path, then restart
or reload the instance as appropriate.

## Choose sources

Open **News → Manage sources**. The selector groups maintained feeds by category,
supports search and checkboxes, and keeps custom feeds in a separate editor.
Saving source choices changes configuration only. It does not download a feed,
create reader content, or alter stored articles.

Custom rows use:

```text
Category | Name | URL | optional max size KiB | optional items per refresh
```

For example:

```text
Community | Example project | https://example.com/releases.atom | 512 | 20
```

Feed-size overrides accept 1–2048 KiB. Item-count overrides accept 1–100; 100 is
the absolute per-source processing ceiling. Omitted values inherit the global
settings.

### Existing v0.8.x configurations

The selector proposes a safe migration from legacy `feeds` rows:

- an exact match to a maintained feed's current or previous URL becomes that
  catalogue selection;
- unmatched rows remain custom feeds with their per-feed bounds;
- legacy rows remain in configuration as compatibility input and are not erased;
- saving the selector activates `selected_feed_ids` plus `custom_feeds`;
- saving, loading, installing, or updating never refreshes a feed.

Maintained feeds use immutable catalogue IDs. If a later plugin release corrects
a selected feed URL, the plugin moves that feed's local validators, health,
articles, and reader bodies to the replacement URL on normal local data access.
That migration atomically unions state from every known historical alias, keeps
current rows when identities overlap, preserves existing reader links, and makes
no network request. New catalogue feeds are never selected automatically.
Historical HTTP aliases may appear in catalogue metadata solely to recognize old
configuration/state; the plugin never fetches those aliases as maintained current
sources.

## Read and refresh

1. Open **News** and choose a category.
2. Click a source name to filter stored articles.
3. Use the eye to include or skip that source for bulk **Refresh selected**.
4. Use the adjacent refresh icon to fetch only that source, regardless of its eye
   state.
5. Click **Refresh selected** only when you want to fetch the included sources in
   that category.
6. Use **Read here** for long structured entries or **Open source** to leave the
   plugin. Short and link-only entries remain source-first.

The eye setting is browser-local and controls only the next bulk/category refresh.
It never changes source configuration or deletes articles. Every network refresh
is explicit and operator-triggered.

Selecting one source shows durable health: last-check time, working/error state,
processed/new/already-stored counts, stored article count, and effective limits.
A successful refresh may insert zero articles when every returned item is already
stored.

## Tools

- `rss_list_feeds(category="")`
- `rss_refresh_feed(name)`
- `rss_recent_entries(limit=20, name="", category="")`
- `rss_feed_status(name)`

The agent can list, query, refresh one configured feed by name, and report health.
It cannot add/remove sources, change the catalogue, purge data, schedule work, or
refresh automatically.

## Reader behavior

News shows at most 400 characters and five visual lines per card. It never sends a
complete structured article body in the scrolling-list API. **Read here** appears
only when an explicit refresh stored meaningful structured content.

The reader preserves safe headings, paragraphs, lists, blockquotes, code,
emphasis, and absolute HTTP(S) links. Existing plain-text rows remain readable as
excerpts and source links without migration-time network access.

The reader's content-free HTML shell can load without an Authorization header so
normal browser navigation works. It waits for protoAgent's authenticated console
handshake before requesting article data. The separate reader API remains
bearer-protected; titles, links, metadata, and sanitized bodies are never placed
in URLs or served anonymously.

## Global settings

The generic plugin Configure dialog controls:

- items processed per fetched document: 1–100, default 100;
- recent items returned when no tool limit is supplied: 1–100, default 20;
- durable entries retained per feed: 1–10000, default 1000;
- maximum decompressed feed size: 1–2048 KiB, default 256 KiB;
- total refresh timeout: 1–60 seconds, default 15 seconds.

Legacy `max_bytes` remains effective until `max_feed_size_kib` is explicitly set;
the KiB setting then takes precedence. State defaults to
`$PROTOAGENT_HOME/rss_atom/feeds.db`; `RSS_ATOM_DATA_DIR` may select another
instance-scoped location.

## Safety and lifecycle

- protoAgent `security.egress.check_url` runs before every request and redirect.
- Redirects are manual and capped at three; HTTPS downgrade is refused.
- Cross-origin redirects do not receive stored ETag/Last-Modified validators.
- The timeout is one total deadline across DNS, redirects, headers, and body.
- Decompressed response size, items processed, and durable entries are bounded.
- Malformed feeds and request failures commit no partial articles.
- Plain-text fallback is UTF-8 bounded to 64 KiB; list excerpts are 400 characters.
- Reader HTML is sanitized by `nh3`/Ammonia with a strict structural allowlist.
  Scripts, styles, SVG, forms, media/embeds, event handlers, relative links, and
  non-HTTP(S) schemes are removed.
- Reader bodies are capped at 128 KiB, 5,000 elements, 1,000 links, and the newest
  20 meaningful bodies per feed. Metadata retention remains independent.
- New reader bodies are created only by explicit refresh. Configuration, page
  loads, catalogue migration, installation, and update do not fetch feeds.
- Refreshes are serialized in-process. Disable stops all activity because there is
  no background process.
- Disable/uninstall retains SQLite data unless the operator deliberately purges
  plugin state through protoAgent.

## Update and rollback

A plugin installed from release tag `v0.9.0` is pinned by protoAgent to the resolved
commit. When a later semver release is available, use the plugin's **Update**
control. Review its release notes before updating; a console view update may need
a protoAgent restart to replace already-mounted routes.

To stay on v0.9.0, keep the recorded tag/commit pin. To roll back, reinstall the
previous trusted release ref. Configuration and SQLite state are retained by a
normal uninstall/reinstall; do not use purge unless you intend to delete plugin
configuration.

Each plugin release must update or deliberately reaffirm both
`protoagent.plugin.yaml:min_protoagent_version` and `compatibility.json`. CI fails
when those records disagree. v0.9.0 is tested against protoAgent 0.135.0.

## Troubleshooting

### A feed exceeds its size limit

Confirm the URL is an RSS/Atom document, not a normal web page. For a trusted feed
whose decompressed XML legitimately exceeds the default, add a conservative
per-feed override in **Manage sources → Custom feeds**. Do not raise the global
limit merely for one source.

### A feed is blocked or fails

The plugin applies protoAgent's egress policy to the original URL and every
redirect. A source may reject automated readers, return a challenge page, redirect
to HTTP, serve malformed XML, or exceed the total timeout. The failure is recorded
for that source; no partial articles are committed.

### Refresh succeeds but no articles appear

Select the source and read its health panel. It distinguishes never checked,
unchanged, empty, no new items, and failed. The eye controls only bulk refresh;
the source refresh icon always tests that source directly.

## Platform evidence

CI runs on Linux, native Windows, and macOS. Each job clones current protoAgent,
loads this repository through its real plugin loader, exercises configuration,
list/status/recent/refresh paths with offline fixtures, and runs parser, storage,
migration, catalogue, and security tests. CI never polls live external feeds.

The v0.9.0 beta release additionally records bounded live validation for every
catalogue candidate and browser acceptance of the selector on Linux. Native CI is
not a claim of full Windows/macOS desktop browser acceptance.

## Post-beta roadmap

These are deliberately outside the stock v0.9.0 beta:

- explicit read/unread state: GitHub issue #2;
- save/clip to a knowledge or documents destination;
- reusable send/share actions.

The beta also excludes crawlers, source-page extraction, web search, scheduling,
background polling, notifications, publishing, credentials, and protoAgent core
changes.

## Support and security

Use GitHub issues for reproducible non-security defects and feed catalogue
corrections. Include the plugin version, protoAgent version, operating system,
source name (not private credentials), expected behavior, and bounded error text.

Do not disclose vulnerabilities publicly. Follow the repository's inherited
security policy at `https://github.com/RomeoRaven/rss-atom-plugin/security/policy`.

## License

[MIT](LICENSE)
