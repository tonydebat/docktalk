"""Unit tests for ``src.bikeshare.parsing``.

A fake Gemini client is injected — its ``models.generate_content`` returns
a canned JSON string per test case. We assert only on the public return
shapes (``list[str]``, ``ClarificationPrompt``, ``SelectionIntent``).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.bikeshare.parsing import (
    ClarificationPrompt,
    SelectionIntent,
    classify_selection_intent,
    clarify_destination,
    parse_destination_intent,
)


class _FakeResponse:
    def __init__(self, text: str) -> None:
        self.text = text


class _FakeModels:
    def __init__(self, text: str) -> None:
        self._text = text
        self.calls: list[dict] = []

    def generate_content(self, *, model, contents, config):
        self.calls.append({"model": model, "contents": contents})
        return _FakeResponse(self._text)


class _FakeClient:
    def __init__(self, text: str) -> None:
        self.models = _FakeModels(text)


def _client(text: str) -> _FakeClient:
    return _FakeClient(text)


# ── parse_destination_intent ─────────────────────────────────────────


def test_parse_destination_intent_returns_ranked_terms():
    fake = _client('{"terms": ["Union Station", "Front and Bay", "Front St"]}')
    terms = parse_destination_intent("near Union Station", client=fake)
    assert terms == ["Union Station", "Front and Bay", "Front St"]


def test_parse_destination_intent_strips_and_dedupes():
    fake = _client('{"terms": ["  Union  ", "Union", "Bay"]}')
    terms = parse_destination_intent("anywhere", client=fake)
    assert terms == ["Union", "Bay"]


def test_parse_destination_intent_caps_at_five():
    fake = _client('{"terms": ["a", "b", "c", "d", "e", "f", "g"]}')
    terms = parse_destination_intent("anywhere", client=fake)
    assert len(terms) == 5


def test_parse_destination_intent_rejects_empty_input():
    fake = _client('{"terms": ["should not be reached"]}')
    assert parse_destination_intent("", client=fake) == []
    assert parse_destination_intent("   ", client=fake) == []


def test_parse_destination_intent_handles_malformed_json():
    fake = _client("not json at all")
    assert parse_destination_intent("near somewhere", client=fake) == []


def test_parse_destination_intent_ignores_non_string_terms():
    fake = _client('{"terms": ["Union", 42, null, "Bay"]}')
    terms = parse_destination_intent("anywhere", client=fake)
    assert terms == ["Union", "Bay"]


# ── clarify_destination ──────────────────────────────────────────────


def test_clarify_destination_returns_structured_prompt():
    fake = _client('{"kind": "intersection", "spoken_question": "Which streets meet there?"}')
    prompt = clarify_destination(["near somewhere"], client=fake)
    assert isinstance(prompt, ClarificationPrompt)
    assert prompt.kind == "intersection"
    assert prompt.spoken_question == "Which streets meet there?"


def test_clarify_destination_falls_back_on_invalid_kind():
    fake = _client('{"kind": "made_up", "spoken_question": "?"}')
    prompt = clarify_destination(["x"], client=fake)
    assert prompt.kind == "intersection"
    assert prompt.spoken_question == "Can you describe a nearby intersection?"


def test_clarify_destination_falls_back_on_empty_question():
    fake = _client('{"kind": "spelling", "spoken_question": ""}')
    prompt = clarify_destination(["x"], client=fake)
    assert prompt.kind == "intersection"
    assert "intersection" in prompt.spoken_question.lower()


def test_clarify_destination_falls_back_on_garbage_response():
    fake = _client("definitely not json")
    prompt = clarify_destination(["x"], client=fake)
    assert prompt.kind == "intersection"


def test_clarify_destination_accepts_empty_history():
    fake = _client('{"kind": "major_street", "spoken_question": "Major street nearby?"}')
    prompt = clarify_destination([], client=fake)
    assert prompt.kind == "major_street"


# ── classify_selection_intent ────────────────────────────────────────

_CANDIDATES = [
    {"station_id": "1", "name": "Union Station"},
    {"station_id": "2", "name": "Bay and Wellington"},
    {"station_id": "3", "name": "Front and York"},
]


def test_classify_selection_select_returns_valid_index():
    fake = _client('{"intent": "select", "index": 1}')
    intent = classify_selection_intent("the second one", _CANDIDATES, client=fake)
    assert intent == SelectionIntent(intent="select", index=1)


def test_classify_selection_out_of_range_index_is_unclear():
    fake = _client('{"intent": "select", "index": 9}')
    intent = classify_selection_intent("number ten", _CANDIDATES, client=fake)
    assert intent.intent == "unclear"
    assert intent.index is None


def test_classify_selection_negative_index_is_unclear():
    fake = _client('{"intent": "select", "index": -1}')
    intent = classify_selection_intent("???", _CANDIDATES, client=fake)
    assert intent.intent == "unclear"


def test_classify_selection_new_destination_uses_provided_transcript():
    fake = _client('{"intent": "new_destination", "transcript": "Spadina and King"}')
    intent = classify_selection_intent(
        "actually I meant Spadina and King", _CANDIDATES, client=fake
    )
    assert intent.intent == "new_destination"
    assert intent.transcript == "Spadina and King"


def test_classify_selection_new_destination_falls_back_to_rider_words():
    fake = _client('{"intent": "new_destination"}')
    intent = classify_selection_intent("Spadina please", _CANDIDATES, client=fake)
    assert intent.intent == "new_destination"
    assert intent.transcript == "Spadina please"


def test_classify_selection_unknown_intent_is_unclear():
    fake = _client('{"intent": "frobnicate"}')
    intent = classify_selection_intent("um", _CANDIDATES, client=fake)
    assert intent.intent == "unclear"


def test_classify_selection_empty_transcript_is_unclear():
    fake = _client('{"intent": "select", "index": 0}')
    intent = classify_selection_intent("", _CANDIDATES, client=fake)
    assert intent.intent == "unclear"


def test_classify_selection_malformed_json_is_unclear():
    fake = _client("nope")
    intent = classify_selection_intent("first one", _CANDIDATES, client=fake)
    assert intent.intent == "unclear"
