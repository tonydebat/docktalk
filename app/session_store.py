"""Shared session record definition.

Kept in its own module to avoid circular imports between
server.py, tools.py, live_bridge.py, and monitor_task.py.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any


@dataclass
class SessionRecord:
    """Per-WebSocket session state.

    Owned by server.py; shared by reference with live_bridge and monitor_task.
    """

    session_id: str
    trip_state: dict[str, Any] | None = None
    alert_queue: asyncio.Queue = field(default_factory=asyncio.Queue)
    monitor_task_handle: asyncio.Task | None = None
    # Populated by the live_bridge after connecting a WebSocket
    ws_ref: Any | None = None
    # Session-level status string mirroring trip_state["status"] for fast reads
    status: str = "NOT_STARTED"
    # Set by handle_confirm_station; cleared by live_bridge after spawning
    spawn_monitor: bool = False
