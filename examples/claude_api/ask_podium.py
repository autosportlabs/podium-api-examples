"""Ask Claude a question about your Podium data, with Podium's MCP server
attached through the Claude API's MCP connector.

    python examples/claude_api/ask_podium.py "How many laps did each car run at my last event?"
    python examples/claude_api/ask_podium.py "What was my best lap at Thunderhill this year?"

Anthropic's servers connect to Podium's MCP server and call its tools while
Claude answers. Two things follow from that:

- PODIUM_API_BASE must be a public Podium (staging or production). Anthropic
  cannot reach a server on your own machine.
- Claude acts with an access token you supply. This example reuses the one
  examples/oauth_cli saves, so run `podium_oauth.py login` first. Your
  application would use the token from its own OAuth sign-in.

Needs ANTHROPIC_API_KEY in .env, or an `ant auth login` profile.
"""

import json
import os
import sys
from pathlib import Path
from urllib.parse import urlsplit

import anthropic
from dotenv import load_dotenv

load_dotenv()

# No default: say which Podium you mean. Staging and production are separate
# systems with separate accounts, and a wrong guess is a confusing failure.
API_BASE = os.environ.get("PODIUM_API_BASE", "").rstrip("/")
if not API_BASE:
    sys.exit("Set PODIUM_API_BASE in .env: https://api.staging.podium.live or https://api.podium.live")
# The token examples/oauth_cli saved for this Podium.
TOKEN_FILE = Path(__file__).resolve().parents[2] / f".podium-tokens.{urlsplit(API_BASE).hostname}.json"


def podium_access_token() -> str:
    if not TOKEN_FILE.exists():
        sys.exit("No Podium token. Run `python examples/oauth_cli/podium_oauth.py login` first.")
    return json.loads(TOKEN_FILE.read_text())["access_token"]


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    question = " ".join(sys.argv[1:])

    client = anthropic.Anthropic()
    with client.beta.messages.stream(
        model="claude-opus-5-5",
        max_tokens=16000,
        betas=["mcp-client-2025-11-20", "server-side-fallback-2026-07-01"],
        # If a safety classifier declines the request, the API reruns it on a
        # suitable fallback model inside the same call.
        fallbacks="default",
        mcp_servers=[
            {
                "type": "url",
                "url": f"{API_BASE}/mcp",
                "name": "podium",
                "authorization_token": podium_access_token(),
            }
        ],
        # Every server above must be referenced by exactly one toolset.
        tools=[{"type": "mcp_toolset", "mcp_server_name": "podium"}],
        system=(
            "You answer questions about the user's motorsport data in Podium using the podium tools. "
            "Lap times are decimal minutes: show them as m:ss.sss. A lap with no lap_time is an "
            "untimed out lap or in lap, not a lap. For lap counts and best laps use get_lap_summary "
            "and get_best_laps rather than paging through laps. Layouts of one venue are different "
            "tracks: compare lap times only within one venue id, and say which layout you mean."
        ),
        messages=[{"role": "user", "content": question}],
    ) as stream:
        message = stream.get_final_message()

    if message.stop_reason == "refusal":
        print("Claude declined this request.", file=sys.stderr)
        return 1
    for block in message.content:
        if block.type == "mcp_tool_use":
            print(f"[called {block.name} {json.dumps(block.input)}]")
        elif block.type == "text":
            print(block.text)
    if message.stop_reason == "max_tokens":
        print("\n[The answer was cut off at max_tokens.]", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
