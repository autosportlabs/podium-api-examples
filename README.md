# Podium API examples

Working examples and documentation for building on [Podium](https://podium.live): the public
REST API, OAuth 2.0 sign-in for third-party applications, and the MCP server for AI agents.

Every example is a short Python script you can read top to bottom. They use plain HTTP
(`httpx`) wherever they can, so the requests carry over to any language.

## Three ways in

| You are building | Use | Example |
| --- | --- | --- |
| A script or service that reads **your own** account's data | An **API key** | [`examples/api_key`](examples/api_key) |
| An application that **other Podium users** connect their accounts to | **OAuth 2.0** | [`examples/oauth_web`](examples/oauth_web/app.py), [`examples/oauth_cli`](examples/oauth_cli/podium_oauth.py) |
| An AI agent or assistant that answers questions about a user's data | The **MCP server** | [`examples/mcp_client`](examples/mcp_client/podium_mcp.py), [`examples/claude_api`](examples/claude_api/ask_podium.py) |

All three read the same data with the same permissions. The difference is whose account the
request acts for and who holds the credential.

## Quick start

```bash
make venv
cp .env.example .env      # then add the credentials the example you want needs
.venv/bin/python examples/api_key/best_laps.py thunderhill
```

Start with [Getting started](docs/getting-started.md). It covers access and the first request.

## Documentation

- [Getting started](docs/getting-started.md): access, addresses, your first request
- [Authentication](docs/authentication.md): API keys, OAuth 2.0, scopes, tokens
- [API reference](docs/api-reference.md): every endpoint, field and filter
- [Errors and rate limits](docs/errors-and-limits.md): what each failure means and what to do
- [MCP server](docs/mcp.md): connecting Claude, ChatGPT and other agents

## The examples

| Script | What it shows |
| --- | --- |
| [`api_key/best_laps.py`](examples/api_key/best_laps.py) | Find every session at a track by venue, then its best lap, grouped by layout, over plain REST. Pagination, rate limits, errors. (An agent does this in one MCP call, `get_best_laps`.) |
| [`api_key/live_stream_link.py`](examples/api_key/live_stream_link.py) | Check whether a session is streaming, then create a short-lived link to its live telemetry. |
| [`live_stream/live_telemetry.py`](examples/live_stream/live_telemetry.py) | Print a session's live telemetry to the console as `channel=value;channel=value`. Status goes to standard error, so the output pipes cleanly. |
| [`oauth_cli/podium_oauth.py`](examples/oauth_cli/podium_oauth.py) | Sign-in for a desktop or command-line program: a public client with PKCE and a loopback redirect. Refresh and sign-out. |
| [`oauth_web/app.py`](examples/oauth_web/app.py) | Sign-in for a server-side web app: a confidential client with a secret. Connect and disconnect. |
| [`mcp_client/podium_mcp.py`](examples/mcp_client/podium_mcp.py) | Connect as an MCP client with the official SDK: discovery, self-registration, consent, tool calls. Lists the cars on track now, and with `--track`, this year's best laps at a track by layout. |
| [`claude_api/ask_podium.py`](examples/claude_api/ask_podium.py) | Ask Claude a question with Podium's MCP server attached through the Claude API. |
| [`scripts/smoke.py`](scripts/smoke.py) | Integration checks against a deployment, from the outside. `make smoke`. |

## Addresses

| | URL |
| --- | --- |
| API | `https://api.podium.live` |
| Web app | `https://app.podium.live` |

The examples talk to `https://api.podium.live`.
