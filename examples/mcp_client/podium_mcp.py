"""Connect to Podium's MCP server the way an AI agent does, using the official
MCP Python SDK.

    python examples/mcp_client/podium_mcp.py                       # tools, whoami, events, cars on track
    python examples/mcp_client/podium_mcp.py --track thunderhill   # and this year's best laps there
    python examples/mcp_client/podium_mcp.py --forget              # drop the saved registration and tokens

There is nothing to set up in Podium first. On the first run the SDK:

1. calls /mcp, gets a 401 that points at the protected-resource metadata;
2. reads that, then the authorization server's metadata;
3. registers itself as an OAuth client (dynamic client registration, RFC 7591);
4. opens the consent screen in your browser and waits for the redirect;
5. exchanges the code for tokens bound to the /mcp resource (RFC 8707).

The registration and tokens are saved to .podium-mcp.<host>.json, one per
Podium, so later runs go
straight to step 5's tokens and refresh them as needed. That file is a
credential: it is git-ignored and readable only by you.
"""

import argparse
import asyncio
import http.server
import json
import os
import sys
import threading
import time
import webbrowser
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import anyio
import httpx
import httpx2
from dotenv import load_dotenv
from mcp import Client
from mcp.client.auth import AuthorizationCodeResult, OAuthClientProvider, TokenStorage
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.auth import OAuthClientInformationFull, OAuthClientMetadata, OAuthToken

load_dotenv()

# No default: say which Podium you mean. Staging and production are separate
# systems with separate accounts, and a wrong guess is a confusing failure.
API_BASE = os.environ.get("PODIUM_API_BASE", "").rstrip("/")
if not API_BASE:
    sys.exit("Set PODIUM_API_BASE in .env: https://api.staging.podium.live or https://api.podium.live")
MCP_URL = f"{API_BASE}/mcp"
REDIRECT_HOST, REDIRECT_PORT = "127.0.0.1", 8766
# One file per Podium host, so staging and production sign-ins never mix.
STATE_FILE = Path(__file__).resolve().parents[2] / f".podium-mcp.{urlsplit(API_BASE).hostname}.json"


class FileTokenStorage(TokenStorage):
    """Keeps the client registration and tokens between runs, in one private file."""

    def _read(self) -> dict[str, Any]:
        return json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}

    def _write(self, key: str, value: dict[str, Any]) -> None:
        state = self._read()
        state[key] = value
        STATE_FILE.write_text(json.dumps(state, indent=2))
        STATE_FILE.chmod(0o600)

    async def get_tokens(self) -> OAuthToken | None:
        saved = self._read().get("tokens")
        return OAuthToken.model_validate(saved) if saved else None

    async def set_tokens(self, tokens: OAuthToken) -> None:
        self._write("tokens", tokens.model_dump(mode="json", exclude_none=True))
        if tokens.expires_in:
            self._write("expires_at", time.time() + tokens.expires_in)

    async def get_client_info(self) -> OAuthClientInformationFull | None:
        saved = self._read().get("client")
        return OAuthClientInformationFull.model_validate(saved) if saved else None

    async def set_client_info(self, client_info: OAuthClientInformationFull) -> None:
        self._write("client", client_info.model_dump(mode="json", exclude_none=True))


async def open_consent_screen(authorization_url: str) -> None:
    print(f"Approve the connection in your browser:\n\n  {authorization_url}\n")
    webbrowser.open(authorization_url)


async def wait_for_redirect() -> AuthorizationCodeResult:
    """Serve the loopback redirect once and hand its parameters back to the SDK.

    The SDK checks `state` and `iss` itself; this only has to collect them.
    """
    received: dict[str, str] = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - the name http.server calls
            url = urlsplit(self.path)
            received.update({key: values[0] for key, values in parse_qs(url.query).items()})
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"<p>Connected. You can close this tab.</p>")

        def log_message(self, *args: Any) -> None:
            pass

    def serve_once() -> None:
        server = http.server.HTTPServer((REDIRECT_HOST, REDIRECT_PORT), Handler)
        thread = threading.Thread(target=server.handle_request, daemon=True)
        thread.start()
        thread.join(timeout=300)
        server.server_close()

    await anyio.to_thread.run_sync(serve_once)
    if "error" in received:
        raise SystemExit(f"Podium did not authorize: {received['error']}")
    return AuthorizationCodeResult(
        code=received["code"], state=received.get("state"), iss=received.get("iss")
    )


def refresh_saved_tokens_if_expired() -> None:
    """Renew saved tokens before the SDK starts, when the access token has expired.

    The SDK does not do this itself for tokens loaded from storage: it does not
    restore their expiry, and before discovery it would send the refresh to
    /token rather than the server's token endpoint. It sends the expired token,
    gets a 401 and starts a whole new sign-in. Refreshing here means the user
    approves once, and later runs reuse the refresh token, as a real agent does.
    """
    state = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}
    tokens, client = state.get("tokens") or {}, state.get("client") or {}
    if not tokens.get("refresh_token") or not client.get("client_id"):
        return
    expires_at = state.get("expires_at")
    if isinstance(expires_at, (int, float)) and expires_at - time.time() > 60:
        return
    metadata = httpx.get(f"{API_BASE}/.well-known/oauth-authorization-server", timeout=30).json()
    data = {
        "grant_type": "refresh_token",
        "refresh_token": tokens["refresh_token"],
        "client_id": client["client_id"],
        "resource": MCP_URL,  # RFC 8707: keep the token bound to /mcp
    }
    if client.get("client_secret"):
        data["client_secret"] = client["client_secret"]
    response = httpx.post(metadata["token_endpoint"], data=data, timeout=30)
    if response.status_code != 200:
        # Revoked in Connected apps, or expired: forget the tokens and sign in again.
        print("Saved sign-in is no longer valid; asking for consent again.", file=sys.stderr)
        state.pop("tokens", None)
        state.pop("expires_at", None)
    else:
        renewed = response.json()
        state["tokens"] = renewed  # Refresh tokens rotate: keep the new one.
        state["expires_at"] = time.time() + renewed.get("expires_in", 0)
    STATE_FILE.write_text(json.dumps(state, indent=2))
    STATE_FILE.chmod(0o600)


def oauth_provider() -> OAuthClientProvider:
    provider = OAuthClientProvider(
        server_url=MCP_URL,
        client_metadata=OAuthClientMetadata(
            client_name="Podium MCP example",
            redirect_uris=[f"http://{REDIRECT_HOST}:{REDIRECT_PORT}/callback"],
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
            # A program on a user's machine cannot keep a secret. "none" makes it
            # a public client that proves itself with PKCE instead. Without it,
            # Podium registers a confidential client and issues a secret.
            token_endpoint_auth_method="none",
            scope="event_read event_device_read device_read lap_read",
        ),
        storage=FileTokenStorage(),
        redirect_handler=open_consent_screen,
        callback_handler=wait_for_redirect,
    )
    return provider


def format_lap_time(minutes: float) -> str:
    """Lap times are decimal minutes (1.5 is 1:30.000), the RaceCapture convention."""
    total_ms = round(minutes * 60_000)
    return f"{total_ms // 60_000}:{(total_ms % 60_000) / 1000:06.3f}"


def describe_venue(venue: dict[str, Any] | None) -> str:
    if not venue:
        return "no venue"
    return f"{venue['name']} {venue['configuration']}".strip()


async def run(track: str | None) -> None:
    refresh_saved_tokens_if_expired()
    http_client = httpx2.AsyncClient(auth=oauth_provider(), timeout=60)
    async with Client(streamable_http_client(MCP_URL, http_client=http_client)) as client:
        tools = await client.list_tools()
        print("Tools offered to this connection:")
        for tool in tools.tools:
            print(f"  {tool.name}: {(tool.description or '').splitlines()[0]}")

        whoami = await client.call_tool("whoami", {})
        print("\nwhoami:", json.dumps(whoami.structured_content, indent=2))

        events = await client.call_tool("list_events", {"page_size": 5})
        print("\nNewest events:")
        for event in (events.structured_content or {}).get("items", []):
            print(f"  {event['start_at'][:10]}  {event['name']}")

        # "Which of my cars is on track?" is one call. `streaming` is null on a
        # session when Podium cannot tell; with the filter, Podium answers an
        # error rather than a list it cannot vouch for.
        live = await client.call_tool("list_event_devices", {"streaming": True})
        if live.is_error:
            print("\nCould not check which cars are on track:", live.content[0].text if live.content else "")
        else:
            on_track = (live.structured_content or {}).get("items", [])
            print(f"\nOn track now: {len(on_track)} session(s)")
            for session in on_track:
                print(f"  car {session['competitor_number'] or '?'}  session {session['id']}")

        if track:
            await best_laps_at(client, track)


async def best_laps_at(client: Client, track: str) -> None:
    """This year's best lap of every session at a track, in one call, grouped by layout.

    `venue_name` matches every word against the venue's name and layout, so
    "thunderhill" covers every Thunderhill layout. Layouts are different
    tracks, so best laps are compared only within one `venue.id`.
    """
    since = f"{time.gmtime().tm_year}-01-01"  # UTC days, as the tool compares them
    rows: list[dict[str, Any]] = []
    page = 1
    while True:
        result = await client.call_tool("get_best_laps", {"venue_name": track, "since": since, "page": page})
        if result.is_error:
            print("\nget_best_laps failed:", result.content[0].text if result.content else "")
            return
        content = result.structured_content or {}
        rows.extend(content.get("items", []))
        if not content.get("has_next"):
            break
        page = content["next_page"]

    print(f"\nBest laps at {track!r} since {since}: {len(rows)} session(s)")
    by_layout: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        by_layout.setdefault(row["venue"]["id"] if row["venue"] else "", []).append(row)
    for layout_rows in by_layout.values():
        best = min(layout_rows, key=lambda row: row["best_lap_time"])
        print(
            f"  {describe_venue(best['venue'])}: {format_lap_time(best['best_lap_time'])}"
            f" (lap {best['best_lap_number']}, {best['start_time'][:10]},"
            f" {best['event_name'] or 'an event another account organised'})"
            f" best of {len(layout_rows)} session(s)"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--forget", action="store_true", help="Delete the saved registration and tokens")
    parser.add_argument("--track", help="Also show this year's best laps at a track, e.g. thunderhill")
    args = parser.parse_args()
    if args.forget:
        STATE_FILE.unlink(missing_ok=True)
        print("Forgotten. The next run registers again and asks for consent.")
        return
    asyncio.run(run(args.track))


if __name__ == "__main__":
    main()
