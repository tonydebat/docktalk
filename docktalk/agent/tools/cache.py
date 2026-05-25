import json
import os
import tempfile
from typing import Any

import requests


def fetch_and_cache(url: str, prefix: str = "docktalk_") -> dict[str, Any]:
    """Download a GBFS JSON endpoint to a temporary file.

    Returns {"cache_path": "/tmp/..."} on success, or an error dict on failure.
    Call delete_cache_file(cache_path) when the cached file is no longer needed.
    """
    try:
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        data = response.json()
    except requests.exceptions.RequestException as exc:
        return {"is_error": True, "content": f"RequestException: {exc}"}
    except ValueError as exc:
        return {"is_error": True, "content": f"JSONDecodeError: {exc}"}

    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            prefix=prefix,
            suffix=".json",
            delete=False,
        ) as fh:
            json.dump(data, fh)
            return {"cache_path": fh.name}
    except OSError as exc:
        return {"is_error": True, "content": f"OSError: {exc}"}


def delete_cache_file(cache_path: str) -> dict[str, Any]:
    """Delete a temporary cache file created by fetch_and_cache.

    Call this once the agent session is done to clean up disk space.
    Returns {"deleted": cache_path} on success, or an error dict on failure.
    """
    try:
        os.remove(cache_path)
        return {"deleted": cache_path}
    except FileNotFoundError:
        return {
            "is_error": True,
            "content": f"FileNotFoundError: cache file '{cache_path}' not found",
        }
    except OSError as exc:
        return {"is_error": True, "content": f"OSError: {exc}"}
