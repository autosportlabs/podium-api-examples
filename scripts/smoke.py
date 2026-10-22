"""Integration checks for a Podium deployment, from the outside, as a client.

    python scripts/smoke.py                                     # against https://api.podium.live
    PODIUM_API_BASE=http://localhost:8000 python scripts/smoke.py   # or another server

Runs what it can with the credentials it finds and skips the rest:

- always: the OAuth and MCP discovery documents and the unauthenticated answers;
- with PODIUM_API_KEY: every /v1/ list endpoint, filters (including `is_streaming`,
  the venue filters and the boolean filters) and pagination;
- with tokens saved by examples/oauth_cli (and PODIUM_PUBLIC_CLIENT_ID): a
  refresh, a Bearer call to /v1/, and a tools/list on /mcp.

Exits 1 if any check fails. Nothing here writes to Podium except the refresh,
which rotates the saved tokens exactly as the example does.
"""

import json
import os
import re
import sys
import time
from collections.abc import Callable
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from dotenv import load_dotenv

load_dotenv()

API_BASE = (os.environ.get("PODIUM_API_BASE") or "https://api.podium.live").rstrip("/")
API_KEY = os.environ.get("PODIUM_API_KEY", "")
PUBLIC_CLIENT_ID = os.environ.get("PODIUM_PUBLIC_CLIENT_ID", "")
# The token examples/oauth_cli saved for this Podium.
TOKEN_FILE = Path(__file__).resolve().parents[1] / f".podium-tokens.{urlsplit(API_BASE).hostname}.json"
MCP_ACCEPT = "application/json, text/event-stream"

results: list[tuple[str, str, str]] = []
http = httpx.Client(timeout=30)


def check(name: str, test: Callable[[], str | None], warn_only: bool = False) -> None:
    """Run one check. [test] returns None or a note on success, and raises AssertionError on failure."""
    try:
        note = test() or ""
        results.append(("PASS", name, note))
    except AssertionError as failure:
        results.append(("WARN" if warn_only else "FAIL", name, str(failure)))
    except httpx.HTTPError as error:
        results.append(("FAIL", name, f"{type(error).__name__}: {error}"))


def skip(name: str, why: str) -> None:
    results.append(("SKIP", name, why))


def same_origin(url: str) -> bool:
    expected, actual = urlsplit(API_BASE), urlsplit(url)
    return (actual.scheme, actual.netloc) == (expected.scheme, expected.netloc)


# --- discovery, no credentials -----------------------------------------

metadata: dict = {}


def authorization_server_metadata() -> str:
    metadata.update(http.get(f"{API_BASE}/.well-known/oauth-authorization-server").raise_for_status().json())
    assert metadata["issuer"].rstrip("/") == API_BASE, f"issuer is {metadata['issuer']!r}"
    for key in ("authorization_endpoint", "token_endpoint", "revocation_endpoint", "registration_endpoint"):
        assert same_origin(metadata[key]), f"{key} is {metadata[key]!r}"
    return f"{len(metadata['scopes_supported'])} scopes"


def public_clients_advertised() -> None:
    methods = metadata.get("token_endpoint_auth_methods_supported", [])
    assert "none" in methods, (
        f"token_endpoint_auth_methods_supported is {methods}; public PKCE clients use 'none'"
    )


def only_usable_grants_advertised() -> None:
    grants = metadata.get("grant_types_supported", [])
    assert set(grants) == {"authorization_code", "refresh_token"}, f"grant_types_supported is {grants}"


def protected_resource_metadata() -> None:
    document = http.get(f"{API_BASE}/.well-known/oauth-protected-resource/mcp").raise_for_status().json()
    assert document["resource"] == f"{API_BASE}/mcp", f"resource is {document['resource']!r}"
    assert [s.rstrip("/") for s in document["authorization_servers"]] == [API_BASE], (
        f"authorization_servers is {document['authorization_servers']}"
    )


def mcp_challenge() -> None:
    response = http.post(
        f"{API_BASE}/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        headers={"Accept": MCP_ACCEPT},
    )
    assert response.status_code == 401, f"status {response.status_code}"
    challenge = response.headers.get("WWW-Authenticate", "")
    match = re.search(r'resource_metadata="([^"]+)"', challenge)
    assert match, f"no resource_metadata in {challenge!r}"
    assert same_origin(match.group(1)), f"resource_metadata points at {match.group(1)!r}"


def v1_requires_credentials() -> None:
    response = http.get(f"{API_BASE}/v1/events/")
    assert response.status_code == 401, f"status {response.status_code}"


check("Authorization server metadata: issuer and endpoints are this host", authorization_server_metadata)
check(
    "Authorization server metadata: advertises public clients ('none')",
    public_clients_advertised,
    warn_only=True,
)
check(
    "Authorization server metadata: advertises only usable grants",
    only_usable_grants_advertised,
    warn_only=True,
)
check("MCP protected-resource metadata names this host", protected_resource_metadata)
check("MCP 401 challenge points at this host's metadata", mcp_challenge)
check("/v1/ refuses a request with no credentials", v1_requires_credentials)

# --- API key -----------------------------------------

if API_KEY:
    key_headers = {"Authorization": f"Api-Key {API_KEY}"}
    found: dict[str, str] = {}

    def list_endpoint(path: str, params: dict | None = None) -> Callable[[], str]:
        def test() -> str:
            response = http.get(
                f"{API_BASE}/v1/{path}", params={"page_size": 1, **(params or {})}, headers=key_headers
            )
            assert response.status_code == 200, f"status {response.status_code}: {response.text[:200]}"
            page = response.json()
            assert set(page) >= {"count", "next", "previous", "results"}, f"keys {sorted(page)}"
            if page["results"]:
                found[path] = page["results"][0]["id"]
            if page["next"]:
                # Following `next` must not send the credential anywhere else, plain http included.
                assert same_origin(page["next"]), f"next link is {page['next']!r}"
            return f"count {page['count']}"

        return test

    check("API key: GET /v1/events/", list_endpoint("events/"))
    check("API key: GET /v1/devices/", list_endpoint("devices/"))
    check("API key: GET /v1/event-devices/", list_endpoint("event-devices/"))
    check("API key: GET /v1/laps/", list_endpoint("laps/"))
    if "events/" in found:
        check(
            "API key: event-devices filtered by event",
            list_endpoint("event-devices/", {"event": found["events/"]}),
        )
    if "event-devices/" in found:
        check(
            "API key: laps filtered by event device",
            list_endpoint("laps/", {"event_device": found["event-devices/"]}),
        )

    def sessions_carry_streaming() -> str:
        response = http.get(f"{API_BASE}/v1/event-devices/", params={"page_size": 1}, headers=key_headers)
        assert response.status_code == 200, f"status {response.status_code}"
        results = response.json()["results"]
        if not results:
            return "no sessions to look at"
        assert "is_streaming" in results[0], f"fields {sorted(results[0])}"
        assert results[0]["is_streaming"] in (True, False, None), (
            f"is_streaming is {results[0]['is_streaming']!r}"
        )
        return f"is_streaming {results[0]['is_streaming']}"

    def streaming_filter() -> str:
        response = http.get(
            f"{API_BASE}/v1/event-devices/",
            params={"is_streaming": "true", "page_size": 100},
            headers=key_headers,
        )
        if response.status_code == 503:
            # The documented answer when Podium cannot read live status, so not a failure.
            assert "Retry-After" in response.headers, "503 without Retry-After"
            return f"503: live status unavailable, Retry-After {response.headers['Retry-After']}"
        assert response.status_code == 200, f"status {response.status_code}: {response.text[:200]}"
        live = response.json()["results"]
        assert all(session.get("is_streaming") is True for session in live), (
            "a listed session is not streaming"
        )
        return f"{len(live)} streaming now"

    def boolean_filters_are_strict() -> None:
        for value, expected in (("1", 200), ("FALSE", 200), ("maybe", 400)):
            response = http.get(
                f"{API_BASE}/v1/event-devices/",
                params={"is_archived": value, "page_size": 1},
                headers=key_headers,
            )
            assert response.status_code == expected, f"is_archived={value}: status {response.status_code}"

    def rows_carry_venue() -> str:
        notes = []
        for path in ("events/", "event-devices/"):
            response = http.get(f"{API_BASE}/v1/{path}", params={"page_size": 100}, headers=key_headers)
            assert response.status_code == 200, f"{path} status {response.status_code}"
            rows = response.json()["results"]
            assert all("venue" in row for row in rows), f"{path} row without venue"
            venues = [row["venue"] for row in rows if row["venue"]]
            # Only the three public fields: never location, geometry or creator.
            assert all(set(venue) == {"id", "name", "configuration"} for venue in venues), (
                f"{path} venue keys"
            )
            notes.append(f"{path} {len(venues)}/{len(rows)} with a venue")
            found.setdefault("venue_word", next((venue["name"].split()[0] for venue in venues), ""))
        return "; ".join(notes)

    def venue_filters() -> str:
        bad = http.get(f"{API_BASE}/v1/event-devices/", params={"venue": "not-a-venue"}, headers=key_headers)
        assert bad.status_code == 400 and "venue" in bad.json(), f"bad venue id: status {bad.status_code}"
        word = found.get("venue_word")
        if not word:
            return "bad id refused; no venue to filter by"
        response = http.get(
            f"{API_BASE}/v1/event-devices/",
            params={"venue_name": word, "page_size": 100},
            headers=key_headers,
        )
        assert response.status_code == 200, f"status {response.status_code}"
        rows = response.json()["results"]

        def venue_text(venue: dict) -> str:
            return f"{venue['name']} {venue['configuration']}".lower()

        assert rows and all(word.lower() in venue_text(row["venue"]) for row in rows), "a row does not match"
        return f"bad id refused; venue_name={word!r} -> {len(rows)}"

    check("API key: sessions carry `is_streaming`", sessions_carry_streaming)
    check("API key: events and sessions carry `venue`, three fields only", rows_carry_venue)
    check("API key: venue filters narrow, and a bad venue id is refused", venue_filters)
    check("API key: the is_streaming filter lists only live sessions", streaming_filter)
    check(
        "API key: a boolean filter takes true/false/1/0 and refuses anything else", boolean_filters_are_strict
    )

    def bad_key_refused() -> None:
        response = http.get(f"{API_BASE}/v1/events/", headers={"Authorization": "Api-Key not-a-real-key"})
        assert response.status_code == 401, f"status {response.status_code}"

    check("API key: an unknown key is refused with 401", bad_key_refused)
else:
    skip("API key checks", "PODIUM_API_KEY is not set")

# --- OAuth token (saved by examples/oauth_cli) -----------------------------------------

if TOKEN_FILE.exists() and PUBLIC_CLIENT_ID and metadata:
    tokens = json.loads(TOKEN_FILE.read_text())

    def refresh() -> None:
        response = http.post(
            metadata["token_endpoint"],
            data={
                "grant_type": "refresh_token",
                "refresh_token": tokens["refresh_token"],
                "client_id": PUBLIC_CLIENT_ID,
            },
        )
        assert response.status_code == 200, f"status {response.status_code}: {response.text[:200]}"
        new = response.json()
        assert new["refresh_token"] != tokens["refresh_token"], "refresh token did not rotate"
        new["expires_at"] = time.time() + new["expires_in"]
        tokens.update(new)
        TOKEN_FILE.write_text(json.dumps(tokens, indent=2))

    # The user may approve only some of what the client asked for (the consent
    # screen lets them untick permissions), so check against what the token holds.
    granted = set(tokens.get("scope", "").split())
    surfaces = [
        ("event_read", "events/"),
        ("event_device_read", "event-devices/"),
        ("device_read", "devices/"),
        ("lap_read", "laps/"),
    ]
    readable = [(scope, path) for scope, path in surfaces if scope in granted]
    withheld = [(scope, path) for scope, path in surfaces if scope not in granted]
    # Which MCP tools each scope opens; whoami needs none.
    tool_scopes = {
        "whoami": None,
        "list_events": "event_read",
        "get_event": "event_read",
        "list_devices": "device_read",
        "get_device": "device_read",
        "list_event_devices": "event_device_read",
        "get_event_device": "event_device_read",
        "create_live_stream_link": "live_stream_read",
        "list_laps": "lap_read",
        "get_lap": "lap_read",
        "get_lap_summary": "lap_read",
        "get_best_laps": "lap_read",
    }

    def bearer_get(path: str) -> httpx.Response:
        return http.get(
            f"{API_BASE}/v1/{path}",
            params={"page_size": 1},
            headers={"Authorization": f"Bearer {tokens['access_token']}"},
        )

    def bearer_reads(path: str) -> Callable[[], None]:
        def test() -> None:
            response = bearer_get(path)
            assert response.status_code == 200, f"status {response.status_code}: {response.text[:200]}"

        return test

    def bearer_refused(scope: str, path: str) -> Callable[[], None]:
        def test() -> None:
            response = bearer_get(path)
            assert response.status_code == 403, f"status {response.status_code}: {response.text[:200]}"
            assert scope in response.json().get("required_scopes", []), f"body {response.text[:200]}"

        return test

    def bearer_mcp() -> str:
        response = http.post(
            f"{API_BASE}/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            headers={"Authorization": f"Bearer {tokens['access_token']}", "Accept": MCP_ACCEPT},
        )
        assert response.status_code == 200, f"status {response.status_code}: {response.text[:200]}"
        names = {tool["name"] for tool in response.json()["result"]["tools"]}
        expected = {name for name, scope in tool_scopes.items() if scope is None or scope in granted}
        assert names == expected, f"tools {sorted(names)}, expected {sorted(expected)}"
        return ", ".join(sorted(names))

    check("OAuth: refresh token rotates", refresh)
    results.append(("INFO", "OAuth: the user approved", " ".join(sorted(granted)) or "(nothing)"))
    if readable:
        scope, path = readable[0]
        check(f"OAuth: Bearer token reads /v1/{path} ({scope})", bearer_reads(path))
    if withheld:
        scope, path = withheld[0]
        check(f"OAuth: /v1/{path} is refused without {scope}, naming it", bearer_refused(scope, path))
    check("OAuth: MCP offers exactly the tools the approved scopes open", bearer_mcp)
else:
    skip("OAuth token checks", "run examples/oauth_cli/podium_oauth.py login first")

# --- report -----------------------------------------

print(f"Podium at {API_BASE}\n")
for status, name, note in results:
    print(
        f"  {status:4}  {name}"
        + (
            f"\n          {note}"
            if note and status not in ("PASS", "INFO")
            else f"  ({note})"
            if note
            else ""
        )
    )
failures = sum(status == "FAIL" for status, _, _ in results)
print(f"\n{failures} failed" if failures else "\nAll checks that ran passed.")
sys.exit(1 if failures else 0)
