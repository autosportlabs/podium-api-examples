"""Find the best lap of every session at the events matching a search.

This is the "what was my best lap at Thunderhill?" question, answered with an
API key and nothing but HTTP:

    python examples/api_key/best_laps.py Thunderhill
    python examples/api_key/best_laps.py Thunderhill --since 2026-01-01

The key needs the event_read, event_device_read and lap_read scopes. Set it as
PODIUM_API_KEY in .env (see .env.example).

The search covers events your account created. A session at an event another
account organised is not found this way; list /v1/event-devices/ instead.
"""

import argparse
import os
import sys
import time
from collections.abc import Iterator
from datetime import date, datetime
from typing import Any

import httpx
from dotenv import load_dotenv

load_dotenv()

# No default: say which Podium you mean. Staging and production are separate
# systems with separate accounts, and a wrong guess is a confusing failure.
API_BASE = os.environ.get("PODIUM_API_BASE", "").rstrip("/")
if not API_BASE:
    sys.exit("Set PODIUM_API_BASE in .env: https://api.staging.podium.live or https://api.podium.live")
PAGE_SIZE = 100  # The largest page the API serves.
MAX_RATE_LIMIT_WAITS = 5


class PodiumError(Exception):
    """An error response from Podium, with the message it gave."""

    def __init__(self, response: httpx.Response) -> None:
        try:
            detail = response.json().get("detail", response.text)
        except ValueError:
            detail = response.text
        super().__init__(f"{response.status_code} {response.request.url.path}: {detail}")
        self.status_code = response.status_code


def get(client: httpx.Client, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    """One GET. Waits out a 429 for as long as Retry-After asks, then tries again."""
    for _ in range(MAX_RATE_LIMIT_WAITS):
        response = client.get(path, params=params)
        if response.status_code == 429:
            time.sleep(float(response.headers.get("Retry-After", "1")))
            continue
        if response.status_code >= 400:
            raise PodiumError(response)
        return response.json()
    raise PodiumError(response)


def paginate(client: httpx.Client, path: str, params: dict[str, Any] | None = None) -> Iterator[dict]:
    """Every item of a list endpoint, one page at a time.

    Pages are requested by number rather than by following the `next` link.
    `next` is a full URL built by the server; building the request yourself
    guarantees your credential only ever goes to the host you configured.
    """
    page_number = 1
    while True:
        page = get(client, path, {**(params or {}), "page": page_number, "page_size": PAGE_SIZE})
        yield from page["results"]
        if not page["next"]:
            return
        page_number += 1


def format_lap_time(minutes: float) -> str:
    """Lap times are decimal minutes (1.5 is 1:30.000), the RaceCapture convention."""
    total_ms = round(minutes * 60_000)
    return f"{total_ms // 60_000}:{(total_ms % 60_000) / 1000:06.3f}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("search", help="Text to match in event names and notes, e.g. a track name")
    parser.add_argument("--since", type=date.fromisoformat, help="Only events starting on or after this date")
    args = parser.parse_args()

    api_key = os.environ.get("PODIUM_API_KEY")
    if not api_key:
        print("Set PODIUM_API_KEY in .env first (see .env.example).", file=sys.stderr)
        return 2

    client = httpx.Client(
        base_url=f"{API_BASE}/v1/",
        headers={"Authorization": f"Api-Key {api_key}", "Accept": "application/json"},
        timeout=30,
    )
    try:
        events = [
            event
            for event in paginate(client, "events/", {"search": args.search})
            if not args.since or datetime.fromisoformat(event["start_at"]).date() >= args.since
        ]
        if not events:
            print(f"No events match {args.search!r}.")
            return 0

        overall: tuple[float, str] | None = None
        for event in sorted(events, key=lambda e: e["start_at"]):
            print(f"\n{event['name']}  ({event['start_at'][:10]})")
            for session in paginate(client, "event-devices/", {"event": event["id"]}):
                # A lap with no lap_time is an untimed fragment (an out lap or
                # in lap), not a lap: leave it out of "best".
                timed = [
                    lap
                    for lap in paginate(client, "laps/", {"event_device": session["id"]})
                    if lap["lap_time"]
                ]
                number = session["competitor_number"]
                label = f"car {number}" if number else f"session {session['id'][:8]}"
                if not timed:
                    print(f"  {label}: no timed laps")
                    continue
                best = min(timed, key=lambda lap: lap["lap_time"])
                print(
                    f"  {label}: best lap {best['lap_number']} in {format_lap_time(best['lap_time'])}"
                    f" of {len(timed)} timed laps"
                )
                if overall is None or best["lap_time"] < overall[0]:
                    overall = (best["lap_time"], f"{event['name']}, lap {best['lap_number']}")

        if overall:
            print(f"\nBest overall: {format_lap_time(overall[0])} at {overall[1]}")
        return 0
    except PodiumError as error:
        print(f"Podium refused the request: {error}", file=sys.stderr)
        if error.status_code == 401:
            print("The key is wrong, revoked or deleted.", file=sys.stderr)
        elif error.status_code == 403:
            print(
                "The key is valid but not allowed: check the account's plan and the key's scopes.",
                file=sys.stderr,
            )
        return 1
    finally:
        client.close()


if __name__ == "__main__":
    sys.exit(main())
