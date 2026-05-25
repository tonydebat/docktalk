import json
from unittest.mock import MagicMock, patch

import pytest

from docktalk.agent.tools.station_status import (
    STATION_STATUS_URL,
    fetch_station_status,
    get_station_status,
)

SAMPLE_PAYLOAD = {
    "data": {
        "stations": [
            {
                "station_id": "7000",
                "num_bikes_available": 3,
                "num_docks_available": 8,
                "is_installed": 1,
                "is_renting": 1,
                "is_returning": 1,
            },
            {
                "station_id": "7001",
                "num_bikes_available": 0,
                "num_docks_available": 0,
                "is_installed": 0,
                "is_renting": 0,
                "is_returning": 0,
            },
        ]
    }
}


class TestFetchStationStatus:
    def test_success_returns_cache_path(self, tmp_path):
        mock_response = MagicMock()
        mock_response.json.return_value = SAMPLE_PAYLOAD

        with patch("docktalk.agent.tools.cache.requests.get", return_value=mock_response) as mock_get, \
             patch("docktalk.agent.tools.cache.tempfile.NamedTemporaryFile") as mock_tmp:
            mock_file = MagicMock()
            mock_file.__enter__ = lambda s: s
            mock_file.__exit__ = MagicMock(return_value=False)
            mock_file.name = str(tmp_path / "docktalk_station_status_abc.json")
            mock_tmp.return_value = mock_file

            result = fetch_station_status()

        mock_get.assert_called_once_with(STATION_STATUS_URL, timeout=10)
        assert "cache_path" in result
        assert result["cache_path"] == mock_file.name

    def test_network_error_returns_error_dict(self):
        import requests as req

        with patch("docktalk.agent.tools.cache.requests.get", side_effect=req.exceptions.ConnectionError("timeout")):
            result = fetch_station_status()

        assert result.get("is_error") is True
        assert "RequestException" in result["content"]

    def test_http_error_returns_error_dict(self):
        import requests as req

        mock_response = MagicMock()
        mock_response.raise_for_status.side_effect = req.exceptions.HTTPError("503")

        with patch("docktalk.agent.tools.cache.requests.get", return_value=mock_response):
            result = fetch_station_status()

        assert result.get("is_error") is True

    def test_writes_json_to_temp_file(self, tmp_path):
        mock_response = MagicMock()
        mock_response.json.return_value = SAMPLE_PAYLOAD

        with patch("docktalk.agent.tools.cache.requests.get", return_value=mock_response):
            result = fetch_station_status()

        assert "cache_path" in result
        cache_path = result["cache_path"]
        try:
            written = json.loads(open(cache_path).read())
            assert written == SAMPLE_PAYLOAD
        finally:
            import os
            os.remove(cache_path)


class TestGetStationStatus:
    def test_returns_station_when_found(self, tmp_path):
        cache_file = tmp_path / "status.json"
        cache_file.write_text(json.dumps(SAMPLE_PAYLOAD))

        result = get_station_status("7000", str(cache_file))

        assert result["station_id"] == "7000"
        assert result["num_docks_available"] == 8

    def test_returns_error_when_station_not_found(self, tmp_path):
        cache_file = tmp_path / "status.json"
        cache_file.write_text(json.dumps(SAMPLE_PAYLOAD))

        result = get_station_status("9999", str(cache_file))

        assert result.get("is_error") is True
        assert "StationNotFound" in result["content"]
        assert "9999" in result["content"]

    def test_returns_error_when_cache_file_missing(self, tmp_path):
        result = get_station_status("7000", str(tmp_path / "nonexistent.json"))

        assert result.get("is_error") is True
        assert "FileNotFoundError" in result["content"]

    def test_returns_error_on_malformed_json(self, tmp_path):
        cache_file = tmp_path / "status.json"
        cache_file.write_text("not valid json{{{")

        result = get_station_status("7000", str(cache_file))

        assert result.get("is_error") is True

    def test_handles_empty_stations_list(self, tmp_path):
        cache_file = tmp_path / "status.json"
        cache_file.write_text(json.dumps({"data": {"stations": []}}))

        result = get_station_status("7000", str(cache_file))

        assert result.get("is_error") is True
        assert "StationNotFound" in result["content"]
