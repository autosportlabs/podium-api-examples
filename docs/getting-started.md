# Getting started

## What you need

The API is in early access. To use it, your Podium user needs two things:

1. **The REST API turned on for your user.** Ask Autosport Labs. Until it is on, the API
   settings pages do not appear and every request answers 403.
2. **A plan that includes the REST API, or the integration developer attribute.** Pro and
   Pro Plus include it. A partner building an integration can be given the *integration
   developer* attribute instead, which opens the API on any plan.

The same two conditions apply to every Podium user who connects to your application through
OAuth, because a token acts as that user. Without the first, the consent screen does not load.
With the first but not the second, the user can approve, but every request made with their
token answers 403 with a message about the plan.

## Environments

| | API base URL |
| --- | --- |
| Production | `https://api.podium.live` |
| Staging | `https://api.staging.podium.live` |

Build and test against staging. It is a separate Podium with its own sign-ins, so create your
account, keys and applications there. Every example requires `PODIUM_API_BASE`, with no default,
so it always says which one it is talking to.

## Your first request

1. Sign in to the Podium web app and open **Settings > API keys**.
2. Create a key. Choose the scopes it needs; for the examples, all of the *read* scopes.
3. Copy the key now. Podium stores only a hash and cannot show it again.
4. Call the API:

```bash
curl https://api.staging.podium.live/v1/events/ \
  -H "Authorization: Api-Key YOUR_KEY"
```

The answer is one page of your events:

```json
{
  "count": 2,
  "next": null,
  "previous": null,
  "results": [
    {"id": "…", "name": "Thunderhill 25 Hours", "start_at": "2026-10-15T09:00:00Z", "…": "…"}
  ]
}
```

## Which way in

- **Your own data, from your own code:** an API key. Simplest. See [Authentication](authentication.md#api-keys).
- **Other people's data, with their permission:** OAuth 2.0. Your application sends them to
  Podium to approve, and gets a token that acts as them. See [Authentication](authentication.md#oauth-20).
- **An AI agent:** the MCP server. Claude, ChatGPT and other agents connect with nothing to
  set up in Podium first. See [MCP server](mcp.md).

## Things that catch people out

- **Lap times are decimal minutes,** not seconds: `1.9433` is 1:56.598. This is the
  RaceCapture convention.
- **A lap with no `lap_time` is not a lap.** It is an untimed fragment: an out lap, an in lap,
  or a stage transit. Leave it out of "best lap" and lap counts.
- **Lists are paged.** Ten items by default, at most 100. See [pagination](api-reference.md#pagination).
- **URLs end in a slash:** `/v1/events/`. Without it, Podium answers a redirect (301), which
  most HTTP clients do not follow for you, and never for a POST.
