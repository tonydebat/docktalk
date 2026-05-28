"""DockTalk FastAPI server.

Endpoints:
  GET  /               → serve index.html
  GET  /stations       → live list of all stations (for debugging / UI)
  WS   /ws/{session_id}→ Gemini Live bridge for one rider session
  GET  /session/{id}   → session snapshot (trip state + status)
  POST /session/{id}/stop → stop monitoring for a session

Run with:
  uvicorn app.server:app --reload
"""

from __future__ import annotations

import logging
import os
import uuid
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.websockets import WebSocket, WebSocketDisconnect

from app.live_bridge import run_bridge
from app.session_store import SessionRecord

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

_STATIC_DIR = Path(__file__).parent / "static"

app = FastAPI(title="DockTalk", version="0.1.0")

# In-memory session store: session_id → SessionRecord
sessions: dict[str, SessionRecord] = {}

app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")


# ── Routes ────────────────────────────────────────────────────────────────────


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(_STATIC_DIR / "index.html")


@app.get("/stations")
async def list_stations() -> JSONResponse:
    """Return merged station info+status for the UI map or debugging."""
    from src.bikeshare.station_data import fetch_all_stations, fetch_live_status
    from src.bikeshare.destination_resolver import merge_info_and_status
    import asyncio

    loop = asyncio.get_running_loop()
    info = await loop.run_in_executor(None, fetch_all_stations)
    status = await loop.run_in_executor(None, fetch_live_status)
    merged = merge_info_and_status(info, status)
    return JSONResponse(content=list(merged.values()))


@app.get("/session/{session_id}")
async def get_session(session_id: str) -> JSONResponse:
    record = sessions.get(session_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Session not found")
    return JSONResponse(
        content={
            "session_id": session_id,
            "status": record.status,
            "trip_state": record.trip_state,
        }
    )


@app.post("/session/{session_id}/stop")
async def stop_session(session_id: str) -> JSONResponse:
    record = sessions.get(session_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Session not found")
    record.status = "STOPPED"
    if record.trip_state:
        record.trip_state["status"] = "finished"
    if record.monitor_task_handle and not record.monitor_task_handle.done():
        record.monitor_task_handle.cancel()
    return JSONResponse(content={"stopped": True, "session_id": session_id})


@app.websocket("/ws/{session_id}")
async def websocket_endpoint(websocket: WebSocket, session_id: str) -> None:
    await websocket.accept()
    logger.info("WS connect: %s", session_id)
    try:
        await run_bridge(websocket, session_id, sessions)
    except WebSocketDisconnect:
        logger.info("WS disconnect: %s", session_id)
    except Exception as exc:
        logger.exception("WS error [%s]: %s", session_id, exc)
