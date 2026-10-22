"""Create a short-lived link that receives a session's live telemetry.

    python examples/api_key/live_stream_link.py <event-device-id> [--expires-in 600]

The key needs the live_stream_read scope, and event_device_read to check first
whether the session is streaming. The link is a credential in its own right:
anyone holding it can watch the stream until it expires, so hand it only to the
program that opens the stream.
"""

import argparse
import os
import sys

import httpx
from dotenv import load_dotenv

load_dotenv()

# No default: say which Podium you mean. Staging and production are separate
# systems with separate accounts, and a wrong guess is a confusing failure.
API_BASE = os.environ.get("PODIUM_API_BASE", "").rstrip("/")
if not API_BASE:
    sys.exit("Set PODIUM_API_BASE in .env: https://api.staging.podium.live or https://api.podium.live")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("event_device_id", help="The session (event device) to stream")
    parser.add_argument("--expires-in", type=int, help="Seconds, 60 to 86400. The server default is 3600.")
    args = parser.parse_args()

    api_key = os.environ.get("PODIUM_API_KEY")
    if not api_key:
        print("Set PODIUM_API_KEY in .env first (see .env.example).", file=sys.stderr)
        return 2

    headers = {"Authorization": f"Api-Key {api_key}"}

    # A link to a session that is not streaming still works, and its stream
    # says when the device starts; checking first just tells you what to expect.
    # Best effort: a key without event_device_read skips it.
    session = httpx.get(f"{API_BASE}/v1/event-devices/{args.event_device_id}/", headers=headers, timeout=30)
    if session.status_code == 200:
        streaming = session.json().get("streaming")
        if streaming is False:
            print("Note: the session is not streaming right now.", file=sys.stderr)
        elif streaming is None:
            print("Note: Podium could not tell whether the session is streaming.", file=sys.stderr)

    body = {"expires_in": args.expires_in} if args.expires_in else {}
    response = httpx.post(
        f"{API_BASE}/v1/event-devices/{args.event_device_id}/live-stream-url/",
        headers=headers,
        json=body,
        timeout=30,
    )
    if response.status_code >= 400:
        print(f"Podium refused the request: {response.status_code} {response.text}", file=sys.stderr)
        return 1

    link = response.json()
    print(f"URL:     {link['url']}")
    print(f"Expires: {link['expires_at']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
