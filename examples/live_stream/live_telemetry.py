"""Print a session's live telemetry to the console, one sample per line, and keep
printing through network drops for as long as it runs:

    Utc=1791936000123;Speed=112.4;RPM=6120;...

    python examples/live_stream/live_telemetry.py <event-device-id>
    python examples/live_stream/live_telemetry.py --device <device-id>   # follow a car across sessions
    python examples/live_stream/live_telemetry.py --latest               # the account's newest session
    python examples/live_stream/live_telemetry.py --latest --backfill    # recent samples first

How it works:

1. Find the session to watch, then ask the API for a live stream link
   (POST /v1/event-devices/{id}/live-stream-url/). The API key needs the
   live_stream_read scope, and event_device_read to find the session.
2. Connect to the broadcaster at the link's URL, over Socket.IO on a WebSocket.
   The URL carries the token as `?token=`. Treat the URL as a credential until
   it expires.
3. Print each `{edid}/telemetry` message as one line. The broadcaster sends the
   session's `{edid}/status` first, and again whenever it changes, so a stopped
   device is reported rather than guessed from silence.

Staying connected (an endurance race, a pit wall on flaky internet):

- A lost connection is reconnected automatically, with a growing delay (1 s up
  to 30 s) so a long outage does not hammer the API, and with no time limit:
  it keeps trying until it is stopped (Ctrl-C).
- Every reconnect asks the API for a *new* link. The Socket.IO client's own
  reconnection is off on purpose: it would reuse the old URL, whose token may
  have expired, and the host a session streams from can change.
- A dead network is noticed by Socket.IO's heartbeat, within about 45 seconds,
  even when no error arrives.
- After a reconnect, the broadcaster's recent samples (it keeps the last 60
  seconds) fill the gap. Only samples newer than the last one printed are
  printed, so nothing appears twice. A gap longer than that is not recovered
  here; the data is still in Podium.
- A device that is merely stopped is not a lost connection: the socket stays
  open, and the status says when it starts again.
- With --device, a new session for the same car (say it moved to another event)
  is picked up: when its session ends, the newest session of that device is
  watched instead.

Only telemetry goes to standard output, so it can be piped or redirected.
Status, laps, alerts and reconnection notes go to standard error.
"""

import argparse
import json
import logging
import os
import random
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Any

import httpx
import socketio
from dotenv import load_dotenv

load_dotenv()

API_BASE = (os.environ.get("PODIUM_API_BASE") or "https://api.podium.live").rstrip("/")


class _QuietDrop(logging.Filter):
    """Engine.IO logs its own view of a dropped connection ("packet queue is
    empty, aborting"); this program reports drops itself, in plainer words.
    Only that message is dropped: an error in one of the handlers below still shows.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        return "packet queue is empty" not in record.getMessage()


logging.getLogger("engineio.client").addFilter(_QuietDrop())

QUIET_WARNING_SECONDS = 15
# Seconds to wait before each reconnection attempt; the last repeats forever.
RETRY_DELAYS = (1, 2, 5, 10, 20, 30)
# A connection that lasted this long resets the delay to the first step.
STABLE_SECONDS = 30
# A link refused this many times in a row is not a network problem: stop.
MAX_REFUSALS = 5
# The longest Retry-After honoured; a longer one falls back to RETRY_DELAYS' last step.
MAX_RETRY_AFTER_SECONDS = 300


class Fatal(Exception):
    """Something retrying will not fix: a bad key, a missing scope, an unknown session."""


class Transient(Exception):
    """Something worth retrying: the network, a server error, a rate limit."""

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


def note(message: str) -> None:
    """Status for the person watching. Standard error keeps standard output pure data."""
    print(f"# {message}", file=sys.stderr, flush=True)


def api(api_key: str) -> httpx.Client:
    return httpx.Client(
        base_url=f"{API_BASE}/v1/", headers={"Authorization": f"Api-Key {api_key}"}, timeout=30
    )


def retry_after_seconds(value: str | None) -> float | None:
    """Retry-After in seconds. It may also be an HTTP date (a load balancer's
    503, say); that falls back to this program's own delays rather than crashing."""
    try:
        seconds = float(value) if value else None
    except ValueError:
        return None
    if seconds is None or seconds > MAX_RETRY_AFTER_SECONDS:
        return None
    return seconds


def request(client: httpx.Client, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
    """One API call, sorted into success, Transient (retry) or Fatal (stop)."""
    try:
        response = client.request(method, path, **kwargs)
    except httpx.TransportError as error:  # no network, DNS, timeout
        raise Transient(f"cannot reach Podium ({type(error).__name__})") from error
    if response.is_success:
        return dict(response.json())
    try:
        # Never echo a response body wholesale: a successful link carries its token.
        detail = response.json().get("detail", "")
    except ValueError:
        detail = ""
    if response.status_code == 429 or response.status_code >= 500:
        raise Transient(
            f"Podium answered {response.status_code} {detail}".strip(),
            retry_after_seconds(response.headers.get("Retry-After")),
        )
    raise Fatal(f"Podium refused {method} {path}: {response.status_code} {detail}".strip())


@dataclass
class Target:
    """What to watch: one session, a device's newest session, or the account's newest."""

    event_device_id: str | None = None
    device_id: str | None = None
    latest: bool = False

    def resolve(self, client: httpx.Client) -> str:
        """The session to watch now. Asked again before every reconnection."""
        if self.event_device_id:
            return self.event_device_id
        params: dict[str, Any] = {"page_size": 1}  # newest first
        if self.device_id:
            params["device"] = self.device_id
        results = request(client, "GET", "event-devices/", params=params)["results"]
        if not results:
            # Nothing to watch yet; a device may start a session later.
            raise Transient(
                "no session yet for " + (f"device {self.device_id}" if self.device_id else "this account")
            )
        return str(results[0]["id"])

    @property
    def follows(self) -> bool:
        """Whether a session that ends should be replaced by the next one."""
        return self.event_device_id is None


# How long to hold live samples for a backfill reply before printing them anyway.
BACKFILL_WAIT_SECONDS = 5


def utc_of(sample: dict[str, Any]) -> float | None:
    utc = sample.get("Utc")
    return float(utc) if isinstance(utc, (int, float)) else None


@dataclass
class Printed:
    """What has been printed, so a reconnection's backfill fills the gap exactly.

    After a reconnection, live samples start arriving before the backfill reply
    does. Printing them straight away would put the gap after newer samples, and
    the newest-printed mark would move past the gap. So while a reply is
    pending, live samples are held; when it arrives, the gap is printed first
    (samples newer than the last one printed before the drop, and not among
    those held), then the held samples. If no reply comes, the held samples are
    printed after BACKFILL_WAIT_SECONDS.
    """

    edid: str | None = None
    last_utc: float | None = None
    holding: bool = False
    held: list[dict[str, Any]] = field(default_factory=list)
    gap_start: float | None = None
    generation: int = 0  # which hold a timer belongs to
    closed: bool = False  # standard output has gone (a pipe whose reader exited)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def switch_to(self, edid: str) -> None:
        with self.lock:
            if edid != self.edid:
                self.edid, self.last_utc = edid, None

    def expect_backfill(self) -> None:
        """Called before connecting: hold live samples until the backfill reply."""
        with self.lock:
            self.holding, self.held, self.gap_start = True, [], self.last_utc
            self.generation += 1

    def requested_backfill(self) -> None:
        """Called once the request is sent, which on a slow link can be seconds
        after expect_backfill: give the reply BACKFILL_WAIT_SECONDS from here."""
        with self.lock:
            generation = self.generation
        timer = threading.Timer(BACKFILL_WAIT_SECONDS, self._give_up_waiting, args=(generation,))
        timer.daemon = True  # never keeps the program alive
        timer.start()

    def _give_up_waiting(self, generation: int) -> None:
        with self.lock:
            waiting = self.holding and generation == self.generation
        if waiting:
            note(f"no backfill reply in {BACKFILL_WAIT_SECONDS} s; the gap is not filled")
            self.backfill([])

    def live(self, sample: dict[str, Any]) -> None:
        with self.lock:
            if self.holding:
                self.held.append(sample)
            else:
                self._print(sample)

    def backfill(self, samples: list[dict[str, Any]]) -> None:
        """The backfill reply (or, from the timer, none): print the gap, then what was held."""
        with self.lock:
            if not self.holding:
                return  # already flushed: the timer fired, or a reply came twice
            held_utcs = {utc_of(sample) for sample in self.held}
            gap = [
                sample
                for sample in samples
                if utc_of(sample) is not None
                and (self.gap_start is None or utc_of(sample) > self.gap_start)  # type: ignore[operator]
                and utc_of(sample) not in held_utcs
            ]
            for sample in sorted(gap, key=lambda sample: utc_of(sample) or 0):
                self._print(sample)
            for sample in self.held:
                self._print(sample)
            self.holding, self.held = False, []

    def _print(self, sample: dict[str, Any]) -> None:
        if self.closed:
            return
        utc = utc_of(sample)
        if utc is not None:
            self.last_utc = utc if self.last_utc is None else max(self.last_utc, utc)
        try:
            print(format_sample(sample), flush=True)
        except BrokenPipeError:
            # This runs on Socket.IO's thread, which would swallow the error;
            # watch() and run() check the flag and stop.
            self.closed = True


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


@dataclass
class Outcome:
    """How one connection ended."""

    lasted: float
    connected: bool = True
    refused: bool = False  # the broadcaster would not accept the link
    session_over: str | None = None  # "session_ended" or "eventdevice_deleted"


def watch(edid: str, link: dict[str, Any], printed: Printed, backfill: bool) -> Outcome:
    """One connection, from connect to disconnect. Ctrl-C propagates."""
    received = threading.Event()
    state: dict[str, Any] = {"state": None, "over": None, "server_closed": False, "refused_as": None}
    sio = socketio.Client(reconnection=False)
    started = connected_at = time.monotonic()

    @sio.event
    def connect() -> None:
        nonlocal connected_at
        connected_at = time.monotonic()
        note(f"connected to {link['url'].split('?')[0]} for session {edid}")
        if backfill:
            sio.emit("backfill", json.dumps({"edid": edid}))
            printed.requested_backfill()

    @sio.event
    def connect_error(data: Any) -> None:
        # The broadcaster refuses a bad link at the handshake, with a reason
        # code such as "invalid_stream_token" or "api_unavailable".
        state["refused_as"] = data.get("message") if isinstance(data, dict) else str(data)

    @sio.event
    def disconnect(*reason: Any) -> None:
        state["server_closed"] = bool(reason) and str(reason[0]) == sio.reason.SERVER_DISCONNECT

    @sio.on(f"{edid}/telemetry")
    def telemetry(payload: Any) -> None:
        sample = decode(payload)
        if isinstance(sample, dict):
            received.set()
            printed.live(sample)

    @sio.on(f"{edid}/telemetry_backfill")
    def telemetry_backfill(payload: Any) -> None:
        samples = decode(payload)
        samples = [s for s in samples if isinstance(s, dict)] if isinstance(samples, list) else []
        note(f"backfill: {len(samples)} recent sample(s)")
        printed.backfill(samples)

    @sio.on(f"{edid}/status")
    def status(payload: Any) -> None:
        current = decode(payload)
        state["state"] = current.get("state") if isinstance(current, dict) else None
        note(describe_status(current))
        reason = current.get("reason") if isinstance(current, dict) else None
        if state["state"] == "offline" and reason in ("session_ended", "eventdevice_deleted"):
            # This session will not stream again; the loop below leaves so the
            # caller can decide. Disconnecting here instead would race a connect
            # still in progress, when the status is the first thing to arrive.
            state["over"] = reason

    @sio.on("*")
    def anything_else(event: str, payload: Any) -> None:
        # Laps and alert messages for this session; not telemetry, so not on standard output.
        note(f"{event.removeprefix(edid + '/')}: {json.dumps(decode(payload))}")

    def warn_if_quiet() -> None:
        # The status already says when the device is off. Silence from a device
        # that says it is streaming is the case worth a warning.
        if not received.wait(QUIET_WARNING_SECONDS) and sio.connected and state["state"] == "streaming":
            note(f"the device says it is streaming, but no telemetry in {QUIET_WARNING_SECONDS} s")

    if backfill:
        printed.expect_backfill()
    try:
        # The link's URL as the API returns it, token included. Python's WebSocket
        # client sends an Origin header, and the broadcaster only admits one
        # without the web app's origin when the URL carries the token.
        sio.connect(link["url"], transports=["websocket"], wait_timeout=10)
    except socketio.exceptions.ConnectionError as error:
        printed.backfill([])  # nothing is coming; stop holding
        lasted = time.monotonic() - started
        reason = state["refused_as"]
        if reason and reason != "api_unavailable":
            # Not the network: a link just created was turned away.
            note(f"the broadcaster refused the link ({reason})")
            return Outcome(lasted=lasted, connected=False, refused=True)
        note(f"could not connect to the broadcaster: {reason or error}")
        return Outcome(lasted=lasted, connected=False)
    threading.Thread(target=warn_if_quiet, daemon=True).start()
    try:
        # A short sleep loop rather than sio.wait(): that blocks in a thread
        # join that Ctrl-C cannot interrupt, so the program would not stop.
        while sio.connected and not printed.closed and state["over"] is None:
            time.sleep(0.25)
    finally:
        if sio.connected:
            # Bounded, so a stalled socket cannot keep the program alive.
            closer = threading.Thread(target=sio.disconnect, daemon=True)
            closer.start()
            closer.join(timeout=3)
    lasted = time.monotonic() - started
    # An older broadcaster accepted a bad link and closed it at once. Timed from
    # the connect, since the handshake alone can take seconds on a slow network.
    refused = state["server_closed"] and time.monotonic() - connected_at < 3 and state["over"] is None
    return Outcome(lasted=lasted, connected=True, refused=refused, session_over=state["over"])


def run(client: httpx.Client, target: Target, expires_in: int, backfill_first: bool, reconnect: bool) -> int:
    printed = Printed()
    failures = 0  # consecutive attempts that did not hold a connection
    refusals = 0
    first = True
    ended = None  # a session that ended; finding it again means no newer one is listed yet
    while True:
        try:
            edid = target.resolve(client)
            if edid == ended:
                raise Transient(f"session {edid} has ended and no newer one is listed yet")
            printed.switch_to(edid)
            link = request(
                client, "POST", f"event-devices/{edid}/live-stream-url/", json={"expires_in": expires_in}
            )
        except Transient as error:
            delay = error.retry_after or RETRY_DELAYS[min(failures, len(RETRY_DELAYS) - 1)]
            failures += 1
            note(f"{error}; retrying in {delay:.0f} s")
            time.sleep(delay + random.uniform(0, 0.5))
            continue

        # Backfill on the first connection only when asked; on every reconnection,
        # to fill the gap (only newer samples are printed).
        outcome = watch(edid, link, printed, backfill=backfill_first or not first)
        first = False
        if printed.closed:
            note("standard output closed; stopping")
            # Python flushes standard output at exit; with the reader gone that
            # would raise a BrokenPipeError of its own.
            os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
            return 0

        if outcome.session_over == "eventdevice_deleted":
            note("the session was deleted; stopping")
            return 1
        if outcome.session_over == "session_ended":
            if not target.follows:
                note("this session has ended (the device moved on); use --device to follow the car")
                return 0
            note("the session ended; looking for the device's new session")
            ended, failures = edid, 0
            continue
        if not reconnect:
            note("disconnected; not reconnecting (--no-reconnect)")
            return 1

        refusals = refusals + 1 if outcome.refused else 0
        if refusals >= MAX_REFUSALS:
            note("the broadcaster keeps refusing new links; check the key's access to this session")
            return 1
        if outcome.lasted >= STABLE_SECONDS:
            failures = 0  # it held: start the delays again from the shortest
        delay = RETRY_DELAYS[min(failures, len(RETRY_DELAYS) - 1)]
        failures += 1
        if outcome.connected:
            note(f"connection lost after {outcome.lasted:.0f} s; reconnecting in {delay} s with a new link")
        else:
            note(f"retrying in {delay} s with a new link")
        time.sleep(delay + random.uniform(0, 0.5))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("event_device_id", nargs="?", help="The session (event device) to watch")
    target.add_argument("--device", help="Watch this device's newest session, and follow it to the next")
    target.add_argument("--latest", action="store_true", help="Watch the account's newest session")
    parser.add_argument(
        "--backfill", action="store_true", help="Print the broadcaster's recent samples first"
    )
    parser.add_argument("--no-reconnect", action="store_true", help="Exit when the connection drops")
    parser.add_argument(
        "--expires-in",
        type=int,
        default=300,
        help="Link lifetime in seconds (60 to 86400). It only has to last until the connect; "
        "every reconnection gets a new link.",
    )
    args = parser.parse_args()

    api_key = os.environ.get("PODIUM_API_KEY")
    if not api_key:
        sys.exit("Set PODIUM_API_KEY in .env first (see .env.example).")

    chosen = Target(event_device_id=args.event_device_id, device_id=args.device, latest=args.latest)
    try:
        with api(api_key) as client:
            return run(client, chosen, args.expires_in, args.backfill, reconnect=not args.no_reconnect)
    except Fatal as error:
        note(str(error))
        return 1
    except KeyboardInterrupt:
        note("stopping")
        return 0


if __name__ == "__main__":
    sys.exit(main())
