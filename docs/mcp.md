# MCP server

Podium runs a Model Context Protocol (MCP) server, so an AI agent can answer questions about a
user's Podium data: "what was my best lap at Thunderhill this year?", "which of my cars ran
this weekend?", "give the pit crew a live link for car 42".

| | |
| --- | --- |
| URL | `https://api.podium.live/mcp` |
| Transport | Streamable HTTP |
| Authorization | OAuth 2.0, with discovery and dynamic client registration |
| Access | Read-only, plus creating live stream links |

The agent acts as the user who connected it and sees exactly what that user's API access
sees; see [What a credential sees](api-reference.md#what-a-credential-sees). The same access
conditions apply as for the REST API: see [Getting started](getting-started.md#what-you-need).

## Connecting an agent

Nothing has to be set up in Podium first. The agent registers itself, then sends the user to
Podium's consent screen.

**Claude** (claude.ai and Claude Desktop): Settings > Connectors > Add custom connector. Paste
the URL and connect.

**Claude Code:**

```bash
claude mcp add --transport http podium https://api.podium.live/mcp
```

Then run `/mcp` inside Claude Code to sign in.

**ChatGPT, Cursor, VS Code, Gemini CLI and other MCP clients:** add a remote MCP server with
the URL above. Each client signs in the same way.

**Your own code:** see [`examples/mcp_client`](../examples/mcp_client/podium_mcp.py), which
uses the official MCP Python SDK. To give Claude the tools inside your own application, see
[`examples/claude_api`](../examples/claude_api/ask_podium.py), which uses the Claude API's
MCP connector.

To disconnect an agent, the user removes it under **Settings > Connected apps** in Podium.

## How an agent connects

This is what MCP clients do on their own. It matters only if you are writing one.

1. Call `/mcp` without a token. Podium answers 401 with a `WWW-Authenticate` header naming
   the protected-resource metadata and the scopes the tools need.
2. Read `/.well-known/oauth-protected-resource/mcp` (RFC 9728), which names the authorization
   server, then `/.well-known/oauth-authorization-server` (RFC 8414).
3. Register at `/oauth/register/` (RFC 7591). Send `token_endpoint_auth_method: "none"` for a
   client that cannot keep a secret; otherwise Podium registers a confidential client and
   issues one.
4. Run the authorization code flow with PKCE, with `resource` set to the `/mcp` URL
   (RFC 8707). The token then works on `/mcp` only. See [OAuth 2.0](authentication.md#the-flow)
   and [Resource indicators](authentication.md#resource-indicators).
5. Call `/mcp` with `Authorization: Bearer <token>`.

A self-registered client is limited to the read scopes and live stream links, whatever it
asks for. Rate limits apply per application and user, as for the REST API.

Client ID metadata documents, the newer alternative to registration, are not supported yet.
Clients that prefer them, such as ChatGPT, fall back to registration.

## Tools

A tool appears only when the connection holds its scope. `whoami` is always there.

MCP clients ask for every scope the tools need. On the consent screen the user can untick
any of them; the agent then simply does not see the tools that need it. Unticking
"Live stream links", for example, removes `create_live_stream_link`.

| Tool | Scope | Arguments |
| --- | --- | --- |
| `whoami` | | None. Returns the user, account, client and granted scopes |
| `list_events` | `event_read` | `search`, `time_filter` (`UPCOMING`, `IN_PROGRESS`, `PAST`), `venue`, `venue_name`, `page`, `page_size` |
| `get_event` | `event_read` | `event_id` |
| `list_devices` | `device_read` | `is_private`, `page`, `page_size` |
| `get_device` | `device_read` | `device_id` |
| `list_event_devices` | `event_device_read` | `event`, `device`, `is_archived`, `streaming`, `venue`, `venue_name`, `page`, `page_size` |
| `get_event_device` | `event_device_read` | `event_device_id` |
| `list_laps` | `lap_read` | `event_device`, `event`, `include_aggregates`, `page`, `page_size` |
| `get_lap` | `lap_read` | `lap_id` |
| `get_lap_summary` | `lap_read` | Exactly one of `event` or `event_device`; `channels` |
| `get_best_laps` | `lap_read` | `since`, `until` (UTC dates, YYYY-MM-DD), `venue`, `venue_name`, `search`, `page`, `page_size` (default 100) |
| `create_live_stream_link` | `live_stream_read` | `event_device_id`, `expires_in` (seconds) |

Objects have the same fields as in the [API reference](api-reference.md), apart from a
device's `streaming_key` and a session's `device_sn`, which tokens never receive. Sessions carry
`streaming`, so "which of my cars is on track?" is one call: `list_event_devices` with
`streaming: true`.

Events and sessions carry their `venue` (`id`, `name`, `configuration`), and both list tools
filter by it with `venue` and `venue_name`, the same rules as the [REST filters](api-reference.md#venues).
"Which sessions did I run at Laguna Seca?" is `list_event_devices` with `venue_name: "laguna"`,
and it finds your sessions at events another account organised too. Add `streaming: true` for
"who is on track at Thunderhill right now?".

`list_laps` leaves each lap's per-channel `aggregates` out unless `include_aggregates` is true;
they are most of a lap's size. `get_lap` always includes them.

### Lap summaries

`get_lap_summary` answers lap counts and best laps for an event or a session in one call. Per
session: `lap_count` (timed laps) and `untimed_count`; the best lap's `best_lap_time`,
`best_lap_number` and `best_lap_id`; `last_lap_time`; `first_lap_start` and `last_lap_end`; and
`aggregates`, per channel across the timed laps only: lowest `min`, highest `max`, total
`count`, and an `avg` weighted by each lap's sample count. `channels` limits the aggregates.
The result also has the event's `venue` once, at the top: `id`, `name` and `configuration` (the
layout). It is null when the event has no venue, or when none of your sessions are in the result.

`get_best_laps` returns the best lap of every session in a date range, across events, in the
order they started: "what was my best lap at Thunderhill this year?" in one call. Sessions with
no timed lap are left out, and a lap time of zero never counts as best. `since` and `until` are
inclusive and compared with the day each session started **in UTC**: a Friday-evening session
in California starts on Saturday in UTC, so widen the range by a day when local days matter.
Each row carries its `venue`: `id`, `name` and `configuration`, the layout. Pick a track with:

- `venue_name`: every word must appear in the venue's name or layout. "thunderhill" matches
  every Thunderhill layout; "thunderhill east" only the East one.
- `venue`: a venue ID from a row, for an exact match.

Both only match venues where your own cars have run. Layouts are different tracks, so compare
best laps only between rows with the same `venue.id`. `search` matches the names of events
your account created. A session at an event another account organised comes with its `event`
ID and venue, and a null `event_name`.

List tools return a page:

```json
{"items": [ … ], "page": 1, "page_size": 10, "total": 42, "total_pages": 5, "has_next": true, "next_page": 2}
```

Detail tools return `{"item": { … }}`.

A refused call comes back as a tool error whose message says what was wrong: a bad argument
(such as `streaming: "maybe"` or a malformed date), a missing scope, or live status being
unavailable for `streaming`. Agents read the message and adjust, as for any tool error.

## What agents should know about the data

Agents get these from the tool descriptions, but they are worth putting in your own prompts:

- Lap times are decimal minutes. `1.9433` is 1:56.598.
- A lap with no `lap_time` is an untimed out lap, in lap or transit, not a lap.
- For counts, best laps and session statistics, use `get_lap_summary` and `get_best_laps`
  rather than paging through `list_laps`.
- A session's `streaming` is null when Podium cannot tell, which is not the same as false.
- Layouts of one venue are different tracks. Compare lap times only within one `venue.id`.
- To pick a track, use `venue_name` or `venue`, not an event name search.
