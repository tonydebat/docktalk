"""Agent tools for DockTalk.

Exports ALL_TOOLS — a list of all callable tool functions the Gemini agent
may invoke. Each function returns a plain dict: either the result data or
{"is_error": True, "content": "<ErrorType>: <message>"}.

Typical session lifecycle
--------------------------
1. fetch_station_information() → {"cache_path": "/tmp/docktalk_station_info_xxx.json"}
2. fetch_station_status()      → {"cache_path": "/tmp/docktalk_station_status_xxx.json"}
3. get_station_information(station_id, info_cache_path)
4. get_station_status(station_id, status_cache_path)
5. delete_cache_file(info_cache_path)
6. delete_cache_file(status_cache_path)
"""

from .cache import delete_cache_file
from .station_information import fetch_station_information, get_station_information
from .station_status import fetch_station_status, get_station_status

ALL_TOOLS = [
    fetch_station_status,
    get_station_status,
    fetch_station_information,
    get_station_information,
    delete_cache_file,
]

__all__ = [
    "ALL_TOOLS",
    "fetch_station_status",
    "get_station_status",
    "fetch_station_information",
    "get_station_information",
    "delete_cache_file",
]
