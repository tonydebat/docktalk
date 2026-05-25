import json
from unittest.mock import MagicMock, patch

import pytest

from docktalk.agent.tools.station_information import (
    STATION_INFORMATION_URL,
    fetch_station_information,
    get_station_information,
)

SAMPLE_PAYLOAD = {
    "data": {
        "stations": [
            {
                "station_id": "7000",
                "name": "Bay St / Queens Quay W",
                "lat": 43.6404,
                "lon": -79.3831,
                "capacity": 11,
                "address": "Bay St / Queens Quay W",
            },
            {
                "station_id": "7001",
                "name": "Simcoe St / Front St W",
                "lat": 43.6451,
                "lon": -79.3854,
                "capacity": 15,
                "address": "Simcoe St / Front St W",
            },
        ]
    }
}


class TestFetchStationInformation:
    def test_success_returns_cache_path(self, tmp_path):
        mock_response = MagicMock()
        mock_response.json.return_value = SAMPLE_PAYLOAD

        with patch("docktalk.agent.tools.cache.requests.get", return_value=mock_response), \
             patch("docktalk.agent.tools.cache.tempfile.NamedTemporaryFile") as mock_tmp:
            mock_file = MagicMock()
            mock_file.__enter__ = lambda s: s
            mock_file.__exit__ = MagicMock(return_value=False)
            mock_file.name = str(tmp_path / "docktalk_station_info_abc.json")
            mock_tmp.return_value = mock_file

            result = fetch_station_information()

        mock_get_call = patch("docktalk.agent.tools.cache.requests.get")
        assert "cache_path" in result
        assert result["cache_path"] == mock_file.name

    def test_uses_correct_url(self):
        import requests as req

        with patch("docktalk.agent.tools.cache.requests.get", side_effect=req.exceptions.ConnectionError()) as mock_get:
            fetch_station_information()

        mock_get.assert_called_once_with(STATION_INFORMATION_URL, timeout=10)

    def test_network_error_returns_error_dict(self):
        import requests as req

        with patch("docktalk.agent.tools.cache.requests.get", side_effect=req.exceptions.ConnectionError("refused")):
            result = fetch_station_information()

        assert result.get("is_error") is True
        assert "RequestException" in result["content"]


class TestGetStationInformation:
    def test_returns_station_when_found(self, tmp_path):
        cache_file = tmp_path / "info.json"
        cache_file.write_text(json.dumps(SAMPLE_PAYLOAD))

        result = get_station_information("7000", str(cache_file))

        assert result["station_id"] == "7000"
        assert result["name"] == "Bay St / Queens Quay W"
        assert result["capacity"] == 11

    def test_returns_second_station(self, tmp_path):
        cache_file = tmp_path / "info.json"
        cache_file.write_text(json.dumps(SAMPLE_PAYLOAD))

        result = get_station_information("7001", str(cache_file))

        assert result["station_id"] == "7001"
        assert result["name"] == "Simcoe St / Front St W"

    def test_returns_error_when_station_not_found(self, tmp_path):
        cache_file = tmp_path / "info.json"
        cache_file.write_text(json.dumps(SAMPLE_PAYLOAD))

        result = get_station_information("9999", str(cache_file))

        assert result.get("is_error") is True
        assert "StationNotFound" in result["content"]
        assert "9999" in result["content"]

    def test_returns_error_when_cache_file_missing(self, tmp_path):
        result = get_station_information("7000", str(tmp_path / "nonexistent.json"))

        assert result.get("is_error") is True
        assert "FileNotFoundError" in result["content"]

    def test_returns_error_on_malformed_json(self, tmp_path):
        cache_file = tmp_path / "info.json"
        cache_file.write_text("{{broken")

        result = get_station_information("7000", str(cache_file))

        assert result.get("is_error") is True

    def test_handles_empty_stations_list(self, tmp_path):
        cache_file = tmp_path / "info.json"
        cache_file.write_text(json.dumps({"data": {"stations": []}}))

        result = get_station_information("7000", str(cache_file))

        assert result.get("is_error") is True
        assert "StationNotFound" in result["content"]
