# Authentication

Every request carries one credential in the `Authorization` header:

| Credential | Header | Acts as |
| --- | --- | --- |
| API key | `Authorization: Api-Key <key>` | The user who created the key |
| OAuth 2.0 access token | `Authorization: Bearer <token>` | The user who approved your application |

Either way, a request sees only what that user's account owns: its events, its devices, the
sessions of those devices and their laps. Public data of other accounts is not on this API.

## Scopes

A credential carries scopes, and each endpoint needs one. A request without the scope answers
403 `Credential lacks the required scope.`

| Scope | Allows |
| --- | --- |
| `event_read` | List and retrieve the account's events |
| `event_device_read` | List and retrieve sessions (a device taking part in an event) |
| `device_read` | List and retrieve the account's devices |
| `lap_read` | List and retrieve laps |
| `live_stream_read` | Create signed links to a session's live telemetry |

Ask for the fewest scopes your program uses. The API is read-only today; there are no write
scopes yet.

## API keys

Create keys in the web app under **Settings > API keys**, choosing scopes for each.

- **Copy the key when it is shown.** Podium stores only a hash and cannot show it again.
- **A key acts as the user who created it,** for as long as that user and the account's access
  last. Treat it like a password: keep it in a secret store, never in source control.
- **Revoke a key** from the same page. It stops working on the next request.
- **One thing only a key sees:** a device's `streaming_key`, its credential for sending
  telemetry. OAuth tokens never receive it, so a third-party application cannot pose as a device.

Example: [`examples/api_key/best_laps.py`](../examples/api_key/best_laps.py).

## OAuth 2.0

Use OAuth when other Podium users connect your application to their accounts. You never see
their password. They approve on Podium's consent screen, and you receive a token that acts as
them, limited to the scopes they approved. They can disconnect your application at any time
under **Settings > Connected apps**.

### Register your application

Registering an application is for integration partners: your account needs to have *integration
developer* enabled, whatever its plan. Ask Autosport Labs at
[support@podium.live](mailto:support@podium.live). The same goes for changing an application's
name, redirect URIs, scopes or allowed origins later; disabling and deleting it do not need it.
AI agents and MCP clients do not register here at all: they register themselves (see
[MCP server](mcp.md#how-an-agent-connects)).

In the web app, open **Settings > OAuth applications** and register your application. Choose
the client type to match where your code runs:

| Client type | For | Authenticates with |
| --- | --- | --- |
| **Confidential** | A server you control, such as a web app back end | Client ID and client secret, sent with HTTP Basic or in the form body |
| **Public** | Code on the user's machine: desktop, command line, mobile, single-page app | Client ID only, plus PKCE. It cannot keep a secret, so it has none |

The client secret is shown once, when you register. Store it like a password.

**Redirect URIs** must match exactly, and must use `https`. The one exception is a program on
the user's own machine, which may use plain `http` on a loopback address (RFC 8252):
`http://127.0.0.1:<port>/callback` or `http://[::1]:<port>/callback`. `localhost` is accepted
too, but prefer the IP address: RFC 8252 recommends it, because `localhost` can resolve to
something other than the loopback interface.

### Endpoints

Read them from the authorization server metadata (RFC 8414) rather than hard-coding them:

```
GET https://api.podium.live/.well-known/oauth-authorization-server
```

| Endpoint | Path |
| --- | --- |
| Authorization | `/oauth/authorize/` |
| Token | `/oauth/token/` |
| Revocation | `/oauth/revoke_token/` |
| Dynamic client registration | `/oauth/register/` (see [MCP server](mcp.md#how-an-agent-connects)) |

Check that the document's `issuer` is the base URL you configured before trusting the rest.

### The flow

Podium supports the **authorization code** grant with **PKCE**, and nothing else. PKCE is
required for every client, confidential ones included, and only the `S256` method is accepted.

1. **Send the user to the authorization endpoint** with these query parameters:

   | Parameter | Value |
   | --- | --- |
   | `response_type` | `code` |
   | `client_id` | Your client ID |
   | `redirect_uri` | One of your registered redirect URIs, exactly |
   | `scope` | Space-separated scopes. Required: a request without one is refused |
   | `state` | A random value you check when the user comes back |
   | `code_challenge` | `BASE64URL(SHA256(code_verifier))`, without padding |
   | `code_challenge_method` | `S256` |
   | `resource` | Optional (RFC 8707). Binds the token to one surface; see [Resource indicators](#resource-indicators) |

   Podium redirects the user to the consent screen in the web app. They sign in if they need
   to, and approve or deny.

2. **Handle the redirect.** Podium sends the user back to your redirect URI with `code`,
   `state` and `iss`. Check that `state` is the value you sent, and that `iss` is the issuer
   from the metadata (RFC 9207). On a denial you get `error=access_denied` instead.

3. **Exchange the code** within 60 seconds:

   ```
   POST /oauth/token/
   Content-Type: application/x-www-form-urlencoded

   grant_type=authorization_code&code=…&redirect_uri=…&code_verifier=…&client_id=…
   ```

   A confidential client also authenticates with its secret. The answer:

   ```json
   {
     "access_token": "…",
     "token_type": "Bearer",
     "expires_in": 3600,
     "refresh_token": "…",
     "scope": "event_read lap_read"
   }
   ```

   `scope` is what the user approved, which may be less than you asked for: see
   [When the user approves less](#when-the-user-approves-less).

### Resource indicators

A token can be bound to one part of the API by naming it as `resource` on the authorization
request (RFC 8707). Podium serves two:

| Resource | Surface |
| --- | --- |
| `https://api.podium.live/v1` | The REST API |
| `https://api.podium.live/mcp` | The MCP server |

- **A token that names a resource works only there.** One issued for `/mcp` is refused on
  `/v1/`, and the other way round.
- **A token that names none works on both.** Leave `resource` out unless you want the binding.
- **Any other value is refused** at consent with `invalid_target`, sent back to your redirect URI.
- The token request may repeat the resource or omit it; it cannot name a different one.

MCP clients send `resource` on their own, naming the `/mcp` URL.

### When the user approves less

The consent screen lists each scope you asked for with a checkbox, all ticked. The user can
untick any of them before approving. Your application then holds less than it asked for, and
must cope:

- **Read `scope` in the token response.** It lists what the user approved. Turn off the
  features that need anything missing, rather than calling and failing.
- **Handle a 403 that names the missing scope.** A request that needs a scope the token lacks
  answers:

  ```json
  {"detail": "Credential lacks the required scope.", "required_scopes": ["live_stream_read"]}
  ```

  Tell the user what they withheld, and offer to connect again to grant it.

Ask only for scopes you use. The fewer you ask for, the less there is to untick.

### Tokens

| Token | Lifetime | Notes |
| --- | --- | --- |
| Authorization code | 60 seconds | Single use |
| Access token | 1 hour | Send as `Authorization: Bearer <token>` |
| Refresh token | 30 days | **Rotates:** every refresh returns a new refresh token and retires the old one |

To refresh:

```
POST /oauth/token/
grant_type=refresh_token&refresh_token=…&client_id=…
```

Save the new refresh token every time. Using a retired one fails with `invalid_grant`, and
the user has to approve again.

To sign out, revoke the refresh token. That also ends its access token:

```
POST /oauth/revoke_token/
token=…&token_type_hint=refresh_token&client_id=…
```

A token also stops working when the user disconnects your application under
**Settings > Connected apps**, when their account loses access to the API, or when your
application is disabled. Expect a 401 at any time, and send the user through the flow again.

Examples: [`examples/oauth_web/app.py`](../examples/oauth_web/app.py) (confidential),
[`examples/oauth_cli/podium_oauth.py`](../examples/oauth_cli/podium_oauth.py) (public).
