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

Each credential has its own limits: one API key, or one application acting for one user.

| Window | Limit |
| --- | --- |
| Burst | 120 requests per minute |
| Sustained | 10,000 requests per day |

Over either limit, Podium answers 429 with a `Retry-After` header giving seconds to wait.
Wait at least that long before the next request.

Ways to stay well inside the limits:

- Ask for 100 items per page when you want everything.
- Filter on the server: `laps/?event_device=…` rather than every lap and a filter in your code.
- Keep what you have read. A finished session's laps rarely change; re-read them only when you need to.
