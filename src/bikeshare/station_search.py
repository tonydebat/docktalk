"""Case-insensitive substring search over station names.

Returns at most 10 results ranked by where the query appears in the name
(earlier match = higher rank). Ties are broken by station name length
so shorter, more specific names sort first.
"""

from typing import Any


def search_stations(query: str, stations: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """Return up to 10 stations whose name contains *query* (case-insensitive).

    Args:
        query: The search string. An empty string returns no results.
        stations: Dict of station_id → station info dict as returned by
                  ``fetch_all_stations()``. Each entry must have a ``"name"`` key.

    Returns:
        List of station info dicts (same shape as the input values, with
        ``station_id`` guaranteed present), sorted by earliest match position
        then shortest name. At most 10 results.
    """
    if not query:
        return []

    q = query.lower()
    matches: list[tuple[int, int, dict[str, Any]]] = []

    for station_id, info in stations.items():
        name = info.get("name", "")
        pos = name.lower().find(q)
        if pos >= 0:
            entry = {"station_id": station_id, **info}
            matches.append((pos, len(name), entry))

    matches.sort(key=lambda t: (t[0], t[1]))
    return [entry for _, _, entry in matches[:10]]
