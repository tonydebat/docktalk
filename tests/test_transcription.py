"""Unit tests for ``infer_audio_mime_from_headers``.

``transcribe_audio`` itself is a thin wrapper over the OpenAI SDK and is
not unit-tested per the PRD's "out of test scope" list.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.bikeshare.transcription import infer_audio_mime_from_headers

_IPHONE = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_2 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Mobile/15E148 Safari/604.1"
)
_IPAD = (
    "Mozilla/5.0 (iPad; CPU OS 16_5 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/16.5 Mobile/15E148 Safari/604.1"
)
_IPADOS_AS_MAC = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/16.5 Safari/605.1.15"
)
_DESKTOP_CHROME = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
_DESKTOP_FIREFOX = (
    "Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:120.0) Gecko/20100101 Firefox/120.0"
)


def test_iphone_user_agent_returns_m4a():
    assert infer_audio_mime_from_headers({"User-Agent": _IPHONE}) == "audio/mp4"


def test_ipad_user_agent_returns_m4a():
    assert infer_audio_mime_from_headers({"User-Agent": _IPAD}) == "audio/mp4"


def test_ipados_identifying_as_mac_returns_webm():
    # iPadOS 13+ Safari pretends to be desktop Mac. We deliberately accept
    # this and serve WebM, because Whisper still handles the real-Mac case.
    assert infer_audio_mime_from_headers({"User-Agent": _IPADOS_AS_MAC}) == "audio/webm"


def test_desktop_chrome_returns_webm():
    assert infer_audio_mime_from_headers({"User-Agent": _DESKTOP_CHROME}) == "audio/webm"


def test_desktop_firefox_returns_webm():
    assert infer_audio_mime_from_headers({"User-Agent": _DESKTOP_FIREFOX}) == "audio/webm"


def test_missing_user_agent_defaults_to_webm():
    assert infer_audio_mime_from_headers({}) == "audio/webm"


def test_empty_user_agent_defaults_to_webm():
    assert infer_audio_mime_from_headers({"User-Agent": ""}) == "audio/webm"


def test_none_headers_defaults_to_webm():
    assert infer_audio_mime_from_headers(None) == "audio/webm"


def test_header_lookup_is_case_insensitive():
    # Different servers normalise header casing differently.
    assert infer_audio_mime_from_headers({"user-agent": _IPHONE}) == "audio/mp4"
    assert infer_audio_mime_from_headers({"USER-AGENT": _IPHONE}) == "audio/mp4"
