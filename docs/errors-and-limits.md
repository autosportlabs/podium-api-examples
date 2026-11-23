# Errors and rate limits

## Errors

Errors are JSON. Most carry a `detail` message; a bad filter names the field instead.

| Status | Example body | Meaning | What to do |
| --- | --- | --- | --- |
| 400 | `{"time_filter": ["Select a valid choice. …"]}` | A filter or body value is invalid | Fix the request |
| 400 | `{"is_streaming": ["Use true or false."]}` | A boolean filter got something other than `true`, `false`, `1` or `0` | Fix the request |
| 400 | `{"venue": ["Enter a valid UUID."]}` | An ID filter got something that is not an ID | Fix the request |
| 400 | `{"venue_name": ["Ensure this value has at most 200 characters (it has 240)."]}` | A text filter is too long | Shorten it: a track name needs a few words |
| 401 | `{"detail": "Authentication credentials were not provided."}` | No credential | Send one |
| 401 | `{"detail": "Invalid or inactive API key."}` | Wrong, revoked or deleted key | Use a current key |
| 401 | `{"detail": "Invalid or expired access token."}` | Expired, revoked or disconnected token | Refresh, or sign the user in again |
| 403 | `{"detail": "Upgrade to a paid plan to be able to use the REST API"}` | The account behind the credential no longer has API access | Not fixable from your side; tell the user |
| 403 | `{"detail": "The API is not enabled for this user."}` | The API is not turned on for this user (early access) | Ask Autosport Labs |
| 403 | `{"detail": "Credential lacks the required scope.", "required_scopes": ["…"]}` | The endpoint needs a scope you did not get, or the user unticked | Ask for the scope, or skip the call |
| 404 | `{"detail": "Not found."}` | No such object, **or one this credential may not see** | Treat both the same |
| 429 | `{"detail": "Request was throttled. Expected available in 12 seconds."}` | Rate limited | Wait for `Retry-After` seconds, then retry |
| 503 | `{"detail": "Live status is unavailable right now. Try again shortly, or list without the is_streaming filter."}` | The `is_streaming` filter could not check which devices are live | Wait for `Retry-After` seconds (5), or list without the filter: the sessions then say `is_streaming: null` |
| 5xx | | A fault at Podium | Retry later with backoff |

Podium answers 404, not 403, for another account's objects, so a response never confirms
that something you may not see exists.

## Rate limits

### Who shares a limit

Requests are counted per credential, not per device, endpoint or IP address:

| Credential | What is counted together |
| --- | --- |
| API key | Every request made with that key, from any machine |
| OAuth access token | Every request your application makes for one user |

For an OAuth application this means:

- Each user who connects your application has their own allowance. One user's traffic never
  counts against another's.
- Everything you request for a user counts against that user's allowance, however many
  devices they have and however many of your servers send the requests.
- Refreshing the access token does not reset the count.
- `/v1/` and `/mcp` count together.

### The limits

| Window | Limit |
| --- | --- |
| Burst | 120 requests in any minute |
| Sustained | 10,000 requests in any 24 hours |

A request has to be inside both. If you poll around the clock, the daily limit is the one
that matters: it averages about 7 requests a minute for each user. A user with two devices
leaves about 3.5 requests a minute for each device.

Over either limit, Podium answers 429 with a `Retry-After` header giving seconds to wait.
Wait at least that long before the next request for that credential. Other users'
requests are not affected.

### Limits on signing in

These are counted per IP address, because they apply before Podium knows who is asking:

| Request | Limit |
| --- | --- |
| `POST /oauth/token/` and `POST /oauth/revoke_token/` | 60 a minute |
| Requests to `/v1/` or `/mcp` with a missing, wrong or expired credential | 60 failures a minute |
| `POST /oauth/register/` | 10 an hour |

Past a limit the address gets 429 with `Retry-After`, until the window ends. After 60
failed requests in a minute that covers every request from the address, including ones
with a good credential. The body of a 429 differs between `/v1/`, `/mcp` and `/oauth/`, so
go by the status and the header.

If one server works for many users:

- Spread token refreshes out. Refreshing every user's token at the same moment runs into
  the 60 a minute limit.
- Refresh an access token before its hour is up, and stop sending a token once it gets a
  401. Retrying a dead token in a loop can get the whole server refused for a minute.

### Staying inside the limits

- Ask for 100 items per page when you want everything.
- Filter on the server: `laps/?event_device=…` rather than every lap and a filter in your code.
- Keep what you have read. A finished session's laps rarely change; re-read them only when you need to.
- Poll no more often than the data changes.
