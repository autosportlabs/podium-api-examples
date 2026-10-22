# API reference

Base URL: `https://api.podium.live/v1/`.
Every request needs a credential; see [Authentication](authentication.md).

## What a credential sees

A credential sees one account's data, the account of the user it acts for:

- **Events** that account created.
- **Devices** that account owns, except deleted ones.
- **Sessions** of that account's devices, wherever they ran. That includes sessions at events
  another account organised, such as a series race. Those events are not readable: their
  sessions list the event's ID and venue, but `GET /v1/events/{id}/` answers 404 and an event
  search does not find them.
- **Laps** of those sessions.

## Conventions

- **JSON** in and out.
- **Identifiers** are UUIDs. A field that refers to another object (`account`, `event`,
  `device`, `event_device`) holds that object's ID.
- **Times** are ISO 8601 in UTC, e.g. `2026-10-15T09:00:00Z`.
- **Lap times** are decimal minutes: `1.9433` is 1:56.598.
- **Paths end in a slash.** `/v1/events/`, not `/v1/events`.
- **Read-only,** apart from creating live stream links.
- **Boolean filters** take `true`, `false`, `1` or `0`, in any case. Anything else answers
  `400`, rather than being ignored and returning an unfiltered list.

## Venues

Events and sessions carry the track they ran at as `venue`, or `null` when the event has none:

```json
{"id": "3a1ed453-e952-4b7a-acba-044ca67980b3", "name": "Thunderhill Raceway Park", "configuration": "East"}
```

`configuration` is the layout, an empty string when the venue has none. **Layouts are different
tracks:** East, West and the full course at one venue have different lap times, so compare laps
only within one `venue.id`.

The event and session lists filter by venue:

| Filter | Meaning |
| --- | --- |
| `venue` | Venue ID, for an exact match |
| `venue_name` | Every word must appear in the venue's name or layout. `thunderhill` matches every Thunderhill layout; `thunderhill east` only the East one |

Both only narrow your own events and sessions, so they match only venues where you have
been. A malformed `venue` answers `400`.

## Pagination

Every list is paged:

| Parameter | Default | Maximum |
| --- | --- | --- |
| `page` | 1 | |
| `page_size` | 10 | 100 |

```json
{
  "count": 42,
  "next": "…/v1/laps/?page=2&page_size=10",
  "previous": null,
  "results": [ … ]
}
```

`count` is the total across all pages. `next` and `previous` are null at the ends.

Request each page yourself, by number, and stop when `next` is null. Do not send your
credential to the URL in `next` without checking that it is the host you configured.

## Events

An event is a date range at a place: a race weekend, a test day.

| Method and path | Scope |
| --- | --- |
| `GET /v1/events/` | `event_read` |
| `GET /v1/events/{id}/` | `event_read` |

Newest created first.

| Filter | Meaning |
| --- | --- |
| `search` | Text in the name or notes |
| `time_filter` | `UPCOMING`, `IN_PROGRESS` or `PAST` |
| `venue`, `venue_name` | The event's venue; see [Venues](#venues) |

| Field | Type | Notes |
| --- | --- | --- |
| `id` | UUID | |
| `account` | UUID | The owning account |
| `created_by` | UUID | The user who created it |
| `name` | string | |
| `notes` | string | |
| `start_at`, `end_at` | time | |
| `is_private` | boolean | |
| `time_range_auto_adjustable` | boolean | Podium widens the range to fit the telemetry it receives |
| `venue` | object or null | Where it was held; see [Venues](#venues) |
| `created_at`, `updated_at` | time | |

## Devices

A device is a logger: a RaceCapture unit, a PodiumConnect, or another data system.

| Method and path | Scope |
| --- | --- |
| `GET /v1/devices/` | `device_read` |
| `GET /v1/devices/{id}/` | `device_read` |

Newest first. Deleted devices are not listed.

| Filter | Meaning |
| --- | --- |
| `is_private` | Boolean |

| Field | Type | Notes |
| --- | --- | --- |
| `id` | UUID | |
| `account`, `created_by` | UUID | |
| `name`, `notes` | string | |
| `streaming_key` | string | The device's credential for sending telemetry. **API keys only:** absent for OAuth tokens |
| `is_private` | boolean | |
| `deleted` | boolean | |
| `created_at`, `updated_at` | time | |

## Sessions (event devices)

A session is one device taking part in one event. Laps and live telemetry belong to sessions.

| Method and path | Scope |
| --- | --- |
| `GET /v1/event-devices/` | `event_device_read` |
| `GET /v1/event-devices/{id}/` | `event_device_read` |
| `POST /v1/event-devices/{id}/live-stream-url/` | `live_stream_read` |

Newest first, by start time. A session of a device that has since been deleted is still listed.

| Filter | Meaning |
| --- | --- |
| `event` | Event ID |
| `device` | Device ID |
| `is_archived` | Boolean |
| `streaming` | Boolean: only the sessions whose device is, or is not, streaming right now. If Podium cannot check, the list answers `503` with `Retry-After: 5`, rather than a wrong list |
| `venue`, `venue_name` | Where the session ran; see [Venues](#venues). This finds your sessions at events another account organised, which an event search cannot |

| Field | Type | Notes |
| --- | --- | --- |
| `id` | UUID | |
| `event`, `device` | UUID | |
| `start_time`, `end_time` | time | The span of telemetry received |
| `metadata` | array | The channels the device logged: names, units, sample rates |
| `is_archived` | boolean | The session's telemetry has moved to long-term storage |
| `synchronized_at` | time or null | When the device finished sending everything it logged |
| `source`, `source_ver` | string | The data system that produced the session, and its version, when known |
| `device_sn` | string | The logger's serial number, when known. Empty string if not. **API keys only:** absent for OAuth tokens, because it is usually the device's streaming key |
| `competitor_number` | string | The car number, when known. Empty string if not |
| `streaming` | boolean or null | `true` while the device is streaming this session right now, `false` when it is not, `null` when Podium cannot tell |
| `venue` | object or null | Where it ran, also at events another account organised; see [Venues](#venues) |

### Live stream links

```
POST /v1/event-devices/{id}/live-stream-url/
{"expires_in": 600}
```

`expires_in` is optional: seconds, from 60 to 86400, default 3600. The answer is `201 Created`:

| Field | Notes |
| --- | --- |
| `url` | Opens the session's live telemetry stream |
| `token` | The signed credential inside the link |
| `expires_at` | When it stops working |
| `event_device` | The session |

The link is a credential: anyone with it can watch until it expires. Do not log it.

**Check `streaming` first** if you only want a live session: it saves a connection to a device
that is not sending. A link to a session that is not streaming still works, and the stream's
status event says when the device starts.

**Request a new link every time you connect, and do not reuse one.** Treat the URL as opaque:
never cache it, store it, or build a broadcaster address yourself. The host a session streams
from can change, and a fresh link always names the right one. Each call creates a new link;
earlier ones stay valid until they expire, but nothing should depend on that.

### Receiving the stream

The `url` points at a Socket.IO server (the broadcaster), for example
`wss://<broadcaster host>:5000/?token=…`. Connect a Socket.IO client to that URL
exactly as given, on the default namespace.

- **Use the URL form.** The token can also go in the Socket.IO `auth` payload as
  `streamToken`, but the broadcaster refuses a WebSocket handshake that has an `Origin` header
  and no token in its URL. Python's WebSocket client sends one, so the `auth` form fails there.
- **The token only gates the connection.** An open stream stays open after `expires_at`.
- **A refused link,** expired or for a session the credential cannot see, is disconnected by the
  broadcaster straight after the handshake.
- **The stream is receive only.**

The broadcaster emits these events, named after the session ID (`{edid}`):

| Event | Payload |
| --- | --- |
| `{edid}/status` | Whether the device is streaming. Sent first on every connection, then whenever it changes; see below |
| `{edid}/telemetry` | One sample: a JSON object of channel name to value, including `Utc` in milliseconds |
| `{edid}/telemetry_backfill` | A JSON array of recent samples, in answer to `backfill` |
| `{edid}/lap` | A lap the session just completed |
| `{edid}/alert_message`, `{edid}/alert_message_ack`, `{edid}/alert_message_reply` | Pit-to-car messages and their replies |

Payloads are JSON text. Two things to expect about samples:

- **Each sample carries only the channels read at that moment.** A logger reads channels at
  different rates, for example accelerometer 25 times a second and GPS 10, so channels with no
  reading in that sample arrive as `null`.
- **Order is close to, but not strictly, time order.** A sample can arrive after newer ones, for
  example when a gap is repaired; in one measurement 0.6% did, up to about 5 seconds late. Use
  `Utc`, not arrival order, when order matters.

A sample looks like this:

```json
{"Utc": 1791928800123, "Speed": 112.42, "RPM": 6120, "Latitude": 36.584512, "Longitude": -121.753901}
```

To catch up when you connect mid-session, emit `backfill` with `{"edid": "<session ID>"}`. The
broadcaster answers with the recent samples it holds in memory, which may be none.

#### Status

The first event on every connection is the session's status, before any telemetry. It is sent
again whenever the device starts or stops streaming:

```json
{"state": "streaming", "utc": 1791935060456}
{"state": "offline", "reason": "disconnected", "utc": 1791935000123}
{"state": "unknown"}
```

| `state` | Meaning |
| --- | --- |
| `streaming` | The device is sending this session's data |
| `offline` | It stopped, or has not started. The socket stays open: it will say `streaming` again when the device comes back |
| `unknown` | Sent only on connect, when Podium has not heard from the session lately: it has just restarted (it knows within about a minute), or the device has been silent for three minutes. It says `streaming` as soon as it hears again |

`reason`, on `offline`, is there when Podium knows why: `disconnected` (the device's connection
closed), `eventdevice_deleted`, or `session_ended` (the device moved on to another session).
`utc` is Podium's clock in milliseconds since the epoch.

Nothing else arrives while the device is offline. A socket that closes is different: that is the
connection lost, not the device. Request a new link before reconnecting.

Example: [`examples/live_stream/live_telemetry.py`](../examples/live_stream/live_telemetry.py)
prints each sample as `channel=value;channel=value`, and reports the status as it changes.

## Laps

| Method and path | Scope |
| --- | --- |
| `GET /v1/laps/` | `lap_read` |
| `GET /v1/laps/{id}/` | `lap_read` |

In the order they were driven.

| Filter | Meaning |
| --- | --- |
| `event_device` | Session ID |
| `event` | Event ID: every session at that event |

| Field | Type | Notes |
| --- | --- | --- |
| `id` | UUID | |
| `event_device` | UUID | The session |
| `start_time`, `end_time` | time | |
| `lap_time` | number or null | **Decimal minutes.** Null for an untimed fragment |
| `lap_number` | integer or null | Counts timed laps only. Null for a fragment |
| `aggregates` | object | Per channel: `min`, `max`, `avg` and sample `count` over the lap |

A lap with a null `lap_time` is an untimed fragment: an out lap, an in lap, or a stage transit.
Leave it out of lap counts and best laps. A `lap_time` of `0` is a timing glitch: it is still a
timed lap, counted and numbered like the others, but never the best lap. Podium's own lap
summaries treat it that way, and so should yours.

`aggregates` looks like this:

```json
{"Speed": {"min": 40.0, "max": 120.5, "avg": 88.1, "count": 1200}}
```
