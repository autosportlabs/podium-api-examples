"""Sign a Podium user in from a command-line or desktop program: OAuth 2.0
authorization code with PKCE, as a public client (no client secret).

    python examples/oauth_cli/podium_oauth.py login     # opens the browser, saves tokens
    python examples/oauth_cli/podium_oauth.py events    # calls the API with the saved token
    python examples/oauth_cli/podium_oauth.py refresh   # swaps the refresh token for new tokens
    python examples/oauth_cli/podium_oauth.py logout    # revokes the tokens and deletes them

Register a *public* application in Podium (Settings > OAuth applications) with
the redirect URI http://127.0.0.1:8765/callback, and set its client ID as
PODIUM_PUBLIC_CLIENT_ID in .env.

Tokens are saved beside this repository's README, in .podium-tokens.<host>.json.
That file is a credential: it is git-ignored and readable only by you.
"""

import argparse
import base64
import hashlib
import http.server
import json
import os
import secrets
import sys
import threading
import time
import webbrowser
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
from dotenv import load_dotenv

load_dotenv()

API_BASE = (os.environ.get("PODIUM_API_BASE") or "https://api.podium.live").rstrip("/")
CLIENT_ID = os.environ.get("PODIUM_PUBLIC_CLIENT_ID", "")
# RFC 8252: a native app receives the redirect on a loopback address over plain
# http, the only place Podium accepts http. Use the IP rather than "localhost",
# which can resolve to something other than the loopback interface.
REDIRECT_HOST, REDIRECT_PORT = "127.0.0.1", 8765
REDIRECT_URI = f"http://{REDIRECT_HOST}:{REDIRECT_PORT}/callback"
# Ask only for what the program uses. The user sees this list on the consent screen.
SCOPES = "event_read event_device_read device_read lap_read"
# One file per Podium host, so sign-ins to different servers never mix.
TOKEN_FILE = Path(__file__).resolve().parents[2] / f".podium-tokens.{urlsplit(API_BASE).hostname}.json"


def discover() -> dict[str, Any]:
    """The authorization server's endpoints, from its metadata document (RFC 8414).

    Reading them rather than hard-coding them keeps the program working if
    Podium's endpoints move. The issuer check catches a server
    that is misconfigured, or a metadata document that is not Podium's.
    """
    metadata = (
        httpx.get(f"{API_BASE}/.well-known/oauth-authorization-server", timeout=30).raise_for_status().json()
    )
    if metadata["issuer"].rstrip("/") != API_BASE:
        sys.exit(
            f"The server says its issuer is {metadata['issuer']!r}, not {API_BASE!r}. Refusing to continue."
        )
    return metadata


def pkce_pair() -> tuple[str, str]:
    """(code_verifier, code_challenge) for the S256 method, the only one Podium accepts."""
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def wait_for_redirect(expected_state: str) -> dict[str, str]:
    """Serve one request on the loopback redirect URI and return its query parameters."""
    received: dict[str, str] = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - the name http.server calls
            url = urlsplit(self.path)
            if url.path != "/callback":
                self.send_error(404)
                return
            received.update({key: values[0] for key, values in parse_qs(url.query).items()})
            ok = received.get("state") == expected_state and "code" in received
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            message = "Signed in. You can close this tab." if ok else "Sign-in did not complete."
            self.wfile.write(f"<p>{message}</p>".encode())

        def log_message(self, *args: Any) -> None:
            pass

    server = http.server.HTTPServer((REDIRECT_HOST, REDIRECT_PORT), Handler)
    thread = threading.Thread(target=server.handle_request, daemon=True)
    thread.start()
    thread.join(timeout=300)
    server.server_close()
    return received


def save_tokens(tokens: dict[str, Any]) -> None:
    tokens["expires_at"] = time.time() + tokens.get("expires_in", 0)
    TOKEN_FILE.write_text(json.dumps(tokens, indent=2))
    TOKEN_FILE.chmod(0o600)


def load_tokens() -> dict[str, Any]:
    if not TOKEN_FILE.exists():
        sys.exit("No saved tokens. Run `login` first.")
    return json.loads(TOKEN_FILE.read_text())


def login(open_browser: bool) -> None:
    metadata = discover()
    verifier, challenge = pkce_pair()
    state = secrets.token_urlsafe(16)
    authorize_url = (
        metadata["authorization_endpoint"]
        + "?"
        + urlencode(
            {
                "response_type": "code",
                "client_id": CLIENT_ID,
                "redirect_uri": REDIRECT_URI,
                "scope": SCOPES,
                "state": state,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            }
        )
    )
    print(f"Sign in to Podium and approve the request:\n\n  {authorize_url}\n")
    if open_browser:
        webbrowser.open(authorize_url)

    params = wait_for_redirect(state)
    if params.get("state") != state:
        sys.exit("The redirect did not carry our state value. Not continuing.")
    if "error" in params:
        sys.exit(f"Podium did not authorize: {params['error']} {params.get('error_description', '')}")
    # RFC 9207: the server names itself on the redirect, which defeats a
    # mix-up attack where another server's response is fed to this client.
    if params.get("iss") and params["iss"].rstrip("/") != metadata["issuer"].rstrip("/"):
        sys.exit(f"The redirect came from issuer {params['iss']!r}, not {metadata['issuer']!r}.")

    response = httpx.post(
        metadata["token_endpoint"],
        data={
            "grant_type": "authorization_code",
            "code": params["code"],
            "redirect_uri": REDIRECT_URI,
            "client_id": CLIENT_ID,
            "code_verifier": verifier,
        },
        timeout=30,
    )
    if response.status_code != 200:
        sys.exit(f"Code exchange failed: {response.status_code} {response.text}")
    tokens = response.json()
    save_tokens(tokens)
    print(f"Signed in with scopes: {tokens['scope']}")
    print(f"The access token lasts {tokens['expires_in'] // 60} minutes; `refresh` renews it.")


def refresh() -> dict[str, Any]:
    """Trade the refresh token for a new pair.

    Podium rotates refresh tokens: every refresh returns a new one and retires
    the old. Save the new one each time, or the next refresh fails.
    """
    tokens = load_tokens()
    response = httpx.post(
        discover()["token_endpoint"],
        data={
            "grant_type": "refresh_token",
            "refresh_token": tokens["refresh_token"],
            "client_id": CLIENT_ID,
        },
        timeout=30,
    )
    if response.status_code != 200:
        sys.exit(f"Refresh failed ({response.status_code} {response.text}). Run `login` again.")
    new_tokens = response.json()
    save_tokens(new_tokens)
    print("Tokens refreshed.")
    return new_tokens


def access_token() -> str:
    """The saved access token, refreshed first when it has under a minute left."""
    tokens = load_tokens()
    if tokens.get("expires_at", 0) - time.time() < 60:
        tokens = refresh()
    return tokens["access_token"]


def events() -> None:
    response = httpx.get(
        f"{API_BASE}/v1/events/",
        params={"page_size": 5},
        headers={"Authorization": f"Bearer {access_token()}"},
        timeout=30,
    )
    if response.status_code != 200:
        sys.exit(f"{response.status_code}: {response.text}")
    page = response.json()
    print(f"{page['count']} events. The newest:")
    for event in page["results"]:
        print(f"  {event['start_at'][:10]}  {event['name']}")


def logout() -> None:
    """Revoke the refresh token, which also ends its access token, then forget both."""
    tokens = load_tokens()
    response = httpx.post(
        discover()["revocation_endpoint"],
        data={"token": tokens["refresh_token"], "token_type_hint": "refresh_token", "client_id": CLIENT_ID},
        timeout=30,
    )
    TOKEN_FILE.unlink()
    print("Signed out." if response.status_code == 200 else f"Revocation answered {response.status_code}.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("command", choices=["login", "events", "refresh", "logout"])
    parser.add_argument(
        "--no-browser", action="store_true", help="Print the sign-in URL instead of opening it"
    )
    args = parser.parse_args()
    if not CLIENT_ID:
        sys.exit("Set PODIUM_PUBLIC_CLIENT_ID in .env first (see .env.example).")
    {"login": lambda: login(not args.no_browser), "events": events, "refresh": refresh, "logout": logout}[
        args.command
    ]()


if __name__ == "__main__":
    main()
