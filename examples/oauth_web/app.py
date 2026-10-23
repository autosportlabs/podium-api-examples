"""A server-side web app that connects to a visitor's Podium account.

OAuth 2.0 authorization code with PKCE, as a *confidential* client: the client
secret stays on this server and authenticates every call to the token endpoint.

    flask --app examples/oauth_web/app.py run --host 127.0.0.1 --port 5000

Then open http://127.0.0.1:5000. Register a *confidential* application in
Podium (Settings > OAuth applications) with the redirect URI
http://127.0.0.1:5000/callback, and set PODIUM_WEB_CLIENT_ID and
PODIUM_WEB_CLIENT_SECRET in .env. Registering needs an integration developer
account (see docs/authentication.md).

A real application keeps tokens in its database, encrypted, keyed to its own
user. This demo keeps them in process memory so the OAuth steps stay readable.
"""

import base64
import hashlib
import html
import os
import secrets
import time
from typing import Any
from urllib.parse import urlencode

import httpx
from dotenv import load_dotenv
from flask import Flask, abort, redirect, request, session

load_dotenv()

API_BASE = (os.environ.get("PODIUM_API_BASE") or "https://api.podium.live").rstrip("/")
CLIENT_ID = os.environ.get("PODIUM_WEB_CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("PODIUM_WEB_CLIENT_SECRET", "")
REDIRECT_URI = os.environ.get("PODIUM_WEB_REDIRECT_URI", "http://127.0.0.1:5000/callback")
SCOPES = "event_read event_device_read lap_read"

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY") or secrets.token_hex(32)
# Tokens per browser session. In a real app: a database table, encrypted.
TOKENS: dict[str, dict[str, Any]] = {}


def metadata() -> dict[str, Any]:
    """The authorization server's endpoints (RFC 8414), fetched once and kept."""
    if not hasattr(metadata, "cached"):
        document = httpx.get(f"{API_BASE}/.well-known/oauth-authorization-server", timeout=30)
        document.raise_for_status()
        found = document.json()
        if found["issuer"].rstrip("/") != API_BASE:
            raise RuntimeError(
                f"Issuer {found['issuer']!r} is not {API_BASE!r}; the server is misconfigured."
            )
        metadata.cached = found
    return metadata.cached


def token_request(data: dict[str, str]) -> httpx.Response:
    """POST to the token endpoint, authenticating with HTTP Basic (client_secret_basic)."""
    return httpx.post(metadata()["token_endpoint"], data=data, auth=(CLIENT_ID, CLIENT_SECRET), timeout=30)


def store(tokens: dict[str, Any]) -> None:
    tokens["expires_at"] = time.time() + tokens["expires_in"]
    TOKENS[session["sid"]] = tokens


def current_access_token() -> str | None:
    """This visitor's access token, refreshed when it is about to expire."""
    tokens = TOKENS.get(session.get("sid", ""))
    if not tokens:
        return None
    if tokens["expires_at"] - time.time() < 60:
        response = token_request({"grant_type": "refresh_token", "refresh_token": tokens["refresh_token"]})
        if response.status_code != 200:
            # Revoked in Podium (Settings > Connected apps) or expired: reconnect.
            TOKENS.pop(session["sid"], None)
            return None
        store(response.json())  # Refresh tokens rotate: keep the new one.
    return TOKENS[session["sid"]]["access_token"]


@app.get("/")
def home() -> str:
    token = current_access_token()
    if not token:
        return '<p>Not connected.</p><p><a href="/connect">Connect your Podium account</a></p>'
    response = httpx.get(
        f"{API_BASE}/v1/events/",
        params={"page_size": 10},
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
    )
    if response.status_code != 200:
        return f"<p>Podium answered {response.status_code}: {html.escape(response.text)}</p>"
    rows = "".join(
        f"<li>{html.escape(event['start_at'][:10])} {html.escape(event['name'])}</li>"
        for event in response.json()["results"]
    )
    return f'<p>Connected. Your newest events:</p><ul>{rows}</ul><p><a href="/disconnect">Disconnect</a></p>'


@app.get("/connect")
def connect():
    session.setdefault("sid", secrets.token_urlsafe(16))
    # PKCE is required by Podium for every client, confidential ones included.
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    session["pkce_verifier"] = verifier
    session["oauth_state"] = secrets.token_urlsafe(16)
    query = urlencode(
        {
            "response_type": "code",
            "client_id": CLIENT_ID,
            "redirect_uri": REDIRECT_URI,
            "scope": SCOPES,
            "state": session["oauth_state"],
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
    )
    return redirect(f"{metadata()['authorization_endpoint']}?{query}")


@app.get("/callback")
def callback():
    if request.args.get("state") != session.pop("oauth_state", None):
        abort(400, "State mismatch: this callback was not started here.")
    if "error" in request.args:
        return f"<p>Not connected: {html.escape(request.args['error'])}</p><p><a href='/'>Back</a></p>"
    issuer = request.args.get("iss")
    if issuer and issuer.rstrip("/") != metadata()["issuer"].rstrip("/"):
        abort(400, "The response came from a different authorization server.")
    response = token_request(
        {
            "grant_type": "authorization_code",
            "code": request.args["code"],
            "redirect_uri": REDIRECT_URI,
            "code_verifier": session.pop("pkce_verifier"),
        }
    )
    if response.status_code != 200:
        abort(502, f"Code exchange failed: {response.text}")
    store(response.json())
    return redirect("/")


@app.get("/disconnect")
def disconnect():
    """Revoke the tokens in Podium, then forget them here."""
    tokens = TOKENS.pop(session.get("sid", ""), None)
    if tokens:
        httpx.post(
            metadata()["revocation_endpoint"],
            data={"token": tokens["refresh_token"], "token_type_hint": "refresh_token"},
            auth=(CLIENT_ID, CLIENT_SECRET),
            timeout=30,
        )
    return redirect("/")
