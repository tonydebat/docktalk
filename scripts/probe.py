#!/usr/bin/env python
"""One-shot diagnostic probe for a single Bike Share Toronto station.

Runs a Gemini agent that calls the DockTalk tools to fetch information and
live status for one station, then prints a plain-English summary.

Usage:
    python scripts/probe.py <station_id>

Example:
    python scripts/probe.py 7000

Requires a .env file at the project root containing:
    GEMINI_API_KEY=your_key_here
"""

import argparse
import os
import sys
from pathlib import Path

# Ensure the project root is on sys.path so `docktalk` is importable whether
# the script is run as `python scripts/probe.py` or `./scripts/probe.py`.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv
from google import genai
from google.genai import types

from docktalk.agent.tools import ALL_TOOLS

load_dotenv(Path(__file__).parent.parent / ".env")

MODEL = "gemini-2.0-flash"

_TOOLS = ALL_TOOLS
_TOOL_MAP = {fn.__name__: fn for fn in _TOOLS}


def _dispatch(name: str, args: dict) -> dict:
    fn = _TOOL_MAP.get(name)
    if fn is None:
        return {"is_error": True, "content": f"UnknownTool: {name}"}
    return fn(**args)


def run_probe(station_id: str) -> None:
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        print("Error: GEMINI_API_KEY not set. Add it to a .env file.", file=sys.stderr)
        sys.exit(1)

    client = genai.Client(api_key=api_key)

    prompt = (
        f"You are a DockTalk diagnostic probe. You must fetch live data using "
        f"the provided tools — do not use prior knowledge. "
        f"Look up information and live status for Bike Share Toronto station_id "
        f"'{station_id}'. Fetch the station information and status feeds, look up "
        f"the station in each, then clean up the cache files. Finally, report the "
        f"station name, location, number of available docks, and whether the "
        f"station is active and accepting returns."
    )

    print(f"Probing station {station_id!r} ...\n")

    contents = [types.Content(role="user", parts=[types.Part(text=prompt)])]

    # Track which feed-fetch functions have fired. Both must be called before
    # the agent is allowed to answer from its own reasoning (AUTO mode).
    _REQUIRED_FETCHES = {"fetch_station_information", "fetch_station_status"}
    fetched: set[str] = set()

    while True:
        remaining_fetches = _REQUIRED_FETCHES - fetched
        if remaining_fetches:
            # Force Gemini to call one of the outstanding fetch functions.
            tool_config = types.ToolConfig(
                function_calling_config=types.FunctionCallingConfig(
                    mode="ANY",
                    allowed_function_names=list(remaining_fetches),
                )
            )
        else:
            # Both feeds fetched — let Gemini finish with a text summary.
            tool_config = types.ToolConfig(
                function_calling_config=types.FunctionCallingConfig(mode="AUTO")
            )

        response = client.models.generate_content(
            model=MODEL,
            contents=contents,
            config=types.GenerateContentConfig(
                tools=_TOOLS,
                tool_config=tool_config,
            ),
        )

        candidate = response.candidates[0]

        if candidate.content is None or not candidate.content.parts:
            print(f"Warning: empty response (finish_reason={candidate.finish_reason})", file=sys.stderr)
            break

        contents.append(candidate.content)

        function_calls = [
            p.function_call
            for p in candidate.content.parts
            if p.function_call is not None
        ]

        if not function_calls:
            for part in candidate.content.parts:
                if part.text:
                    print(part.text)
            break

        tool_response_parts = []
        for fc in function_calls:
            print(f"  → {fc.name}({dict(fc.args)})")
            result = _dispatch(fc.name, dict(fc.args))
            if fc.name in _REQUIRED_FETCHES:
                fetched.add(fc.name)
            result = _dispatch(fc.name, dict(fc.args))
            tool_response_parts.append(
                types.Part(
                    function_response=types.FunctionResponse(
                        name=fc.name,
                        response=result,
                    )
                )
            )

        contents.append(types.Content(role="user", parts=tool_response_parts))


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Probe information and status for a single Bike Share Toronto station."
    )
    parser.add_argument("station_id", help="GBFS station_id to look up (e.g. 7000)")
    args = parser.parse_args()
    run_probe(args.station_id)


if __name__ == "__main__":
    main()
