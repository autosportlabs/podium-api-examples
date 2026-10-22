"""Print a session's live telemetry to the console, one sample per line:

    Utc=1791936000123;Speed=112.4;RPM=6120;...

    python examples/live_stream/live_telemetry.py <event-device-id>
    python examples/live_stream/live_telemetry.py --latest                 # the account's newest session
    python examples/live_stream/live_telemetry.py --latest --backfill      # recent samples first

How it works:

1. Check the session's `streaming` field, then ask the API for a live stream link
   (POST /v1/event-devices/{id}/live-stream-url/). The API key needs the
   live_stream_read scope, and event_device_read for the check and for --latest.
2. Connect to the broadcaster at the link's URL, over Socket.IO on a WebSocket.
   The URL carries the token as `?token=`; the broadcaster redacts it from its
   logs. Treat the URL as a credential until it expires.
3. Print each `{edid}/telemetry` message as one line. The broadcaster sends the
   session's `{edid}/status` first, and again whenever it changes, so a stopped
   device is reported rather than guessed from silence.

Only telemetry goes to standard output, so it can be piped or redirected. Status,
laps and alerts go to standard error.

The token only gates the connect: once connected, the stream stays open after
the link expires. A refused link (expired, unknown session) is disconnected by
the broadcaster straight after the handshake.
"""

import argparse
import json
import os
import sys
import threading
import time
from typing import Any

import httpx
import socketio
from dotenv import load_dotenv

load_dotenv()

# No default: say which Podium you mean. Staging and production are separate
# systems with separate accounts, and a wrong guess is a confusing failure.
API_BASE = os.environ.get("PODIUM_API_BASE", "").rstrip("/")
if not API_BASE:
    sys.exit("Set PODIUM_API_BASE in .env: https://api.staging.podium.live or https://api.podium.live")

QUIET_WARNING_SECONDS = 15


def note(message: str) -> None:
    """Status for the person watching. Standard error keeps standard output pure data."""
    print(f"# {message}", file=sys.stderr, flush=True)


def api(api_key: str) -> httpx.Client:
    return httpx.Client(
        base_url=f"{API_BASE}/v1/", headers={"Authorization": f"Api-Key {api_key}"}, timeout=30
    )


def newest_session(client: httpx.Client) -> str:
    """The account's most recent session; the list is newest first."""
    response = client.get("event-devices/", params={"page_size": 1})
    if response.status_code != 200:
        sys.exit(f"Could not list sessions: {response.status_code} {response.text}")
    results = response.json()["results"]
    if not results:
        sys.exit("This account has no sessions.")
    return str(results[0]["id"])


def live_stream_link(client: httpx.Client, event_device_id: str, expires_in: int) -> dict[str, Any]:
    response = client.post(
        f"event-devices/{event_device_id}/live-stream-url/", json={"expires_in": expires_in}
    )
    if not response.is_success:  # 201 Created on success
        # Never echo a response body wholesale: a successful one carries the link's token.
        try:
            detail = response.json().get("detail", "")
        except ValueError:
            detail = ""
        sys.exit(f"Podium refused the link: {response.status_code} {detail}")
    return dict(response.json())


def streaming_now(client: httpx.Client, edid: str) -> bool | None:
    """The session's `streaming` field: True, False, or None when Podium cannot tell.

    Best effort: a key without event_device_read just skips the check.
    """
    response = client.get(f"event-devices/{edid}/")
    return response.json().get("streaming") if response.status_code == 200 else None


def describe_status(status: Any) -> str:
    """One line for a `{edid}/status` message: streaming, offline (with why, when known), or unknown."""
    state = status.get("state") if isinstance(status, dict) else None
    if state == "streaming":
        return "the device is streaming"
    if state == "offline":
        reason = status.get("reason")
        return f"the device stopped streaming ({reason})" if reason else "the device is not streaming"
    # Only ever sent on connect: the broadcaster has just restarted, or has not
    # heard from the device for three minutes. A `streaming` follows when it does.
    return "Podium has not heard from the device lately; it will say when it does"


def format_sample(sample: dict[str, Any]) -> str:
    """Every channel in the sample, as `channel=value;channel=value`, in the order received.

    A logger samples channels at different rates (accelerometer every tick, GPS
    less often), so most samples carry values for only some channels and send
    null for the rest. Those are left out rather than printed as empty.
    """
    return ";".join(f"{name}={value}" for name, value in sample.items() if value is not None)


def decode(payload: Any) -> Any:
    """The broadcaster sends JSON text; a client library may already have decoded it."""
    if isinstance(payload, str):
        try:
            return json.loads(payload)
        except ValueError:
            return payload
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("event_device_id", nargs="?", help="The session (event device) to watch")
    target.add_argument("--latest", action="store_true", help="Watch the account's newest session")
    parser.add_argument(
        "--backfill", action="store_true", help="Print the broadcaster's recent samples first"
    )
    parser.add_argument("--expires-in", type=int, default=600, help="Link lifetime in seconds (60 to 86400)")
    args = parser.parse_args()

    api_key = os.environ.get("PODIUM_API_KEY")
    if not api_key:
        sys.exit("Set PODIUM_API_KEY in .env first (see .env.example).")

    with api(api_key) as client:
        edid = newest_session(client) if args.latest else args.event_device_id
        streaming = streaming_now(client, edid)
        if streaming is False:
            note("the session is not streaming right now; the status will say when it starts")
        elif streaming is None:
            note("Podium could not tell whether the session is streaming; the status will say")
        link = live_stream_link(client, edid, args.expires_in)

    received = threading.Event()
    last_state: dict[str, Any] = {"state": None}
    sio = socketio.Client(reconnection=False)

    @sio.event
    def connect() -> None:
        note(f"connected to {link['url'].split('?')[0]} for session {edid}; waiting for telemetry")
        if args.backfill:
            sio.emit("backfill", json.dumps({"edid": edid}))

    @sio.event
    def connect_error(data: Any) -> None:
        note(f"could not connect: {data}")

    @sio.event
    def disconnect(*reason: Any) -> None:
        why = str(reason[0]) if reason else ""
        if why == sio.reason.SERVER_DISCONNECT:
            note("the broadcaster closed the connection; right after connecting, it refused the link")
        else:
            note(f"disconnected ({why or 'unknown reason'})")

    @sio.on(f"{edid}/telemetry")
    def telemetry(payload: Any) -> None:
        sample = decode(payload)
        if isinstance(sample, dict):
            received.set()
            print(format_sample(sample), flush=True)

    @sio.on(f"{edid}/telemetry_backfill")
    def telemetry_backfill(payload: Any) -> None:
        samples = decode(payload)
        samples = samples if isinstance(samples, list) else []
        note(f"backfill: {len(samples)} recent sample(s)")
        for sample in samples:
            if isinstance(sample, dict):
                print(format_sample(sample), flush=True)

    @sio.on(f"{edid}/status")
    def status(payload: Any) -> None:
        current = decode(payload)
        last_state["state"] = current.get("state") if isinstance(current, dict) else None
        note(describe_status(current))

    @sio.on("*")
    def anything_else(event: str, payload: Any) -> None:
        # Laps and alert messages for this session; not telemetry, so not on standard output.
        note(f"{event.removeprefix(edid + '/')}: {json.dumps(decode(payload))}")

    def warn_if_quiet() -> None:
        # The status already says when the device is off. Silence from a device
        # that says it is streaming is the case worth a warning.
        if not received.wait(QUIET_WARNING_SECONDS) and sio.connected and last_state["state"] == "streaming":
            note(f"the device says it is streaming, but no telemetry in {QUIET_WARNING_SECONDS} s")

    try:
        # The link's URL as the API returns it, token included. Python's WebSocket
        # client sends an Origin header, and the broadcaster only admits one
        # without the web app's origin when the URL carries the token.
        sio.connect(link["url"], transports=["websocket"])
        threading.Thread(target=warn_if_quiet, daemon=True).start()
        # A short sleep loop rather than sio.wait(): that blocks in a thread
        # join that Ctrl-C cannot interrupt, so the program would not stop.
        while sio.connected:
            time.sleep(0.25)
    except socketio.exceptions.ConnectionError as error:
        note(f"could not connect to the broadcaster: {error}")
        return 1
    except KeyboardInterrupt:
        note("stopping")
    finally:
        if sio.connected:
            # Bounded, so a stalled socket cannot keep the program alive.
            closer = threading.Thread(target=sio.disconnect, daemon=True)
            closer.start()
            closer.join(timeout=3)
    return 0


if __name__ == "__main__":
    sys.exit(main())
