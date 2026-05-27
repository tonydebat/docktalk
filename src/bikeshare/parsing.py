"""Gemini-backed structured-output parsers for voice input.

Three functions, all of the same shape: free-text in, structured dataclass out.
Each function fully encapsulates Gemini — callers never touch the SDK directly.

The Gemini client is constructed inside each function (using ``GEMINI_API_KEY``
from the environment), but every function accepts an injected ``client``
parameter for testing. Tests supply a fake client whose ``models.generate_content``
returns canned structured output.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Literal

GEMINI_MODEL = "gemini-2.5-flash"
GEMINI_TIMEOUT_MS = 10_000

ClarificationKind = Literal[
    "intersection", "major_street", "spelling", "describe_nearby"
]
_CLARIFICATION_KINDS: tuple[ClarificationKind, ...] = (
    "intersection",
    "major_street",
    "spelling",
    "describe_nearby",
)

SelectionIntentName = Literal["select", "new_destination", "unclear"]


@dataclass(frozen=True)
class ClarificationPrompt:
    """Returned by :func:`clarify_destination`."""

    kind: ClarificationKind
    spoken_question: str


@dataclass(frozen=True)
class SelectionIntent:
    """Returned by :func:`classify_selection_intent`.

    Exactly one of ``index`` / ``transcript`` is populated depending on
    ``intent``:

    - ``select``           → ``index`` is the chosen candidate (0-based).
    - ``new_destination``  → ``transcript`` is the rider's new destination.
    - ``unclear``          → both fields are ``None``.
    """

    intent: SelectionIntentName
    index: int | None = None
    transcript: str | None = None


_INTENT_PROMPT = (
    "You are a destination parser for a Toronto bike-share app. "
    "The rider has spoken a phrase describing where they want to return "
    "the bike. Extract the drop-off destination and produce a SHORT, "
    "RANKED list of search terms suitable for matching against Bike Share "
    "Toronto station names. Prefer the full landmark or station name first, "
    "then intersection forms, then partial street fragments. Return at most "
    "5 terms, ordered from most to least specific. Output strictly the JSON "
    "object {\"terms\": [\"...\", \"...\"]} and nothing else."
)

_CLARIFY_PROMPT = (
    "You are helping a Toronto bike-share rider pinpoint a drop-off "
    "destination. The rider has already tried to describe it and the system "
    "failed to match any nearby station. Choose ONE clarification "
    "type to ask next. Available kinds:\n"
    "- intersection: ask for the nearest intersection\n"
    "- major_street: ask for the nearest major street\n"
    "- spelling: ask the rider to spell the destination name\n"
    "- describe_nearby: ask the rider to describe what is around the destination\n"
    "Do not repeat a kind that has obviously already been tried in the "
    "history. Output strictly the JSON object "
    "{\"kind\": \"...\", \"spoken_question\": \"...\"} and nothing else. "
    "The spoken question must be one short, natural-sounding sentence."
)

_SELECTION_PROMPT = (
    "A Toronto bike-share rider has been shown a small numbered list of "
    "candidate stations and is responding by voice. Decide whether the "
    "rider is selecting one of the displayed candidates, asking for a new "
    "destination entirely, or unclear. Candidates are 0-indexed. Output "
    "strictly one of:\n"
    "{\"intent\": \"select\", \"index\": N}\n"
    "{\"intent\": \"new_destination\", \"transcript\": \"...\"}\n"
    "{\"intent\": \"unclear\"}\n"
    "Use \"select\" only when the rider's words clearly map to one specific "
    "candidate by ordinal (\"first one\", \"number two\") or by name."
)


def _build_client(client: Any | None) -> Any:
    if client is not None:
        return client
    from google import genai
    from google.genai import types

    return genai.Client(
        api_key=os.getenv("GEMINI_API_KEY"),
        http_options=types.HttpOptions(timeout=GEMINI_TIMEOUT_MS),
    )


def _generate_json(
    client: Any,
    *,
    system_instruction: str,
    user_text: str,
) -> dict[str, Any]:
    """Run a single Gemini call expecting a JSON object reply.

    Returns the parsed dict, or ``{}`` on any failure. The caller is responsible
    for treating an empty dict as a degradation and applying its own fallback.
    """
    from google.genai import types

    config = types.GenerateContentConfig(
        temperature=0.0,
        system_instruction=system_instruction,
        response_mime_type="application/json",
    )
    try:
        response = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=[types.Content(role="user", parts=[types.Part(text=user_text)])],
            config=config,
        )
    except Exception:
        return {}

    text = getattr(response, "text", None)
    if not text:
        return {}
    try:
        parsed = json.loads(text)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def parse_destination_intent(
    transcript: str,
    *,
    client: Any | None = None,
) -> list[str]:
    """Turn a raw transcript into a ranked list of search terms.

    Returns ``[]`` for empty input or on any Gemini failure — callers
    fall through to the next cascade step (geocoding) when this returns
    empty.
    """
    if not transcript or not transcript.strip():
        return []

    client = _build_client(client)
    payload = _generate_json(
        client,
        system_instruction=_INTENT_PROMPT,
        user_text=f"Rider said: {transcript.strip()!r}",
    )
    raw_terms = payload.get("terms", [])
    if not isinstance(raw_terms, list):
        return []

    terms: list[str] = []
    for term in raw_terms:
        if isinstance(term, str):
            cleaned = term.strip()
            if cleaned and cleaned not in terms:
                terms.append(cleaned)
    return terms[:5]


def clarify_destination(
    history: list[str],
    *,
    client: Any | None = None,
) -> ClarificationPrompt:
    """Ask Gemini for the next clarification question.

    On any failure, falls back to a hardcoded "describe a nearby intersection"
    prompt so the loop never blows up.
    """
    fallback = ClarificationPrompt(
        kind="intersection",
        spoken_question="Can you describe a nearby intersection?",
    )

    safe_history = [h for h in (history or []) if isinstance(h, str) and h.strip()]
    user_text = "Failed transcripts so far (most recent last):\n" + "\n".join(
        f"- {h.strip()}" for h in safe_history
    )

    try:
        client = _build_client(client)
    except Exception:
        return fallback

    payload = _generate_json(
        client,
        system_instruction=_CLARIFY_PROMPT,
        user_text=user_text,
    )
    kind = payload.get("kind")
    spoken = payload.get("spoken_question")
    if kind not in _CLARIFICATION_KINDS or not isinstance(spoken, str) or not spoken.strip():
        return fallback
    return ClarificationPrompt(kind=kind, spoken_question=spoken.strip())


def classify_selection_intent(
    transcript: str,
    candidates: list[dict[str, Any]],
    *,
    client: Any | None = None,
) -> SelectionIntent:
    """Classify a confirmation-phase voice response.

    Returns ``SelectionIntent(intent="unclear")`` on empty input, on any
    Gemini failure, or when Gemini's output is malformed (e.g. an out-of-range
    index or an unknown intent string).
    """
    unclear = SelectionIntent(intent="unclear")
    if not transcript or not transcript.strip():
        return unclear

    candidate_lines = [
        f"[{i}] {c.get('name', '(unnamed)')}" for i, c in enumerate(candidates or [])
    ]
    user_text = (
        "Candidates shown to the rider:\n"
        + "\n".join(candidate_lines)
        + f"\nRider said: {transcript.strip()!r}"
    )

    try:
        client = _build_client(client)
    except Exception:
        return unclear

    payload = _generate_json(
        client,
        system_instruction=_SELECTION_PROMPT,
        user_text=user_text,
    )

    intent = payload.get("intent")
    if intent == "select":
        index = payload.get("index")
        if isinstance(index, int) and 0 <= index < len(candidates or []):
            return SelectionIntent(intent="select", index=index)
        return unclear
    if intent == "new_destination":
        new_transcript = payload.get("transcript")
        if isinstance(new_transcript, str) and new_transcript.strip():
            return SelectionIntent(
                intent="new_destination",
                transcript=new_transcript.strip(),
            )
        # Fall back to the rider's original words if Gemini didn't echo them.
        return SelectionIntent(intent="new_destination", transcript=transcript.strip())
    return unclear
