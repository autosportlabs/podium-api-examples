"""Find the best lap of every session you ran at a track.

This is the "what was my best lap at Thunderhill?" question, answered with an
API key and nothing but HTTP:

    python examples/api_key/best_laps.py thunderhill
    python examples/api_key/best_laps.py "thunderhill east" --since 2026-01-01

Sessions are found by venue (`venue_name` on /v1/event-devices/), so this
includes sessions at events another account organised, such as a series race.
Layouts are different tracks, so the results are grouped by layout.

The key needs the event_device_read and lap_read scopes, and event_read for the
names of your own events. Set it as PODIUM_API_KEY in .env (see .env.example).

An agent answers the same question in one MCP call, get_best_laps.
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

API_BASE = (os.environ.get("PODIUM_API_BASE") or "https://api.podium.live").rstrip("/")
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
    parser.add_argument(
        "track", help='Words in the venue name or layout, e.g. "thunderhill" or "thunderhill east"'
    )
    parser.add_argument(
        "--since", type=date.fromisoformat, help="Only sessions starting on or after this date (UTC)"
    )
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
        sessions = [
            session
            for session in paginate(client, "event-devices/", {"venue_name": args.track})
            if not args.since or datetime.fromisoformat(session["start_time"]).date() >= args.since
        ]
        if not sessions:
            print(f"No sessions at a venue matching {args.track!r}.")
            return 0
        # Names of your own events. An event another account organised is not
        # readable, so its sessions show only where they ran.
        event_names = {
            event["id"]: event["name"] for event in paginate(client, "events/", {"venue_name": args.track})
        }

        layouts: dict[str, list[dict[str, Any]]] = {}
        for session in sessions:
            layouts.setdefault(session["venue"]["id"], []).append(session)

        for layout_sessions in layouts.values():
            venue = layout_sessions[0]["venue"]
            print(f"\n{venue['name']} {venue['configuration']}".rstrip())
            best_here: tuple[float, str] | None = None
            for session in sorted(layout_sessions, key=lambda s: s["start_time"]):
                # A lap with no lap_time is an untimed fragment (an out lap or
                # in lap), not a lap. A lap time of 0 is a timed lap, counted and
                # numbered like the others, but a timing glitch, so never "best":
                # the same rules as Podium's own lap summaries.
                timed = [
                    lap
                    for lap in paginate(client, "laps/", {"event_device": session["id"]})
                    if lap["lap_time"] is not None
                ]
                candidates = [lap for lap in timed if lap["lap_time"] > 0]
                number = session["competitor_number"]
                car = f"car {number}" if number else f"session {session['id'][:8]}"
                where = event_names.get(session["event"], "an event another account organised")
                label = f"{session['start_time'][:10]} {where}, {car}"
                if not candidates:
                    print(f"  {label}: no timed laps")
                    continue
                best = min(candidates, key=lambda lap: lap["lap_time"])
                print(
                    f"  {label}: best lap {best['lap_number']} in {format_lap_time(best['lap_time'])}"
                    f" of {len(timed)} timed laps"
                )
                if best_here is None or best["lap_time"] < best_here[0]:
                    best_here = (best["lap_time"], f"{where}, {session['start_time'][:10]}")
            if best_here:
                print(f"  Best here: {format_lap_time(best_here[0])} at {best_here[1]}")
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
