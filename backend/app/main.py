"""FastAPI app: serves the capture page and ingests audio over WebSocket.

Trimmed from live-meeting-assistant's main.py (MIT, Ben Linford): RAG,
speaker verification / voice enrollment, the settings API and the deep-dive
tier are gone. What remains is the MVP path:

  browser PCM -> faster-whisper -> rolling transcript -> Codex CLI -> advice
"""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import meeting
from .config import settings
from .copilot.engine import CopilotEngine
from .transcription.whisper_engine import StreamingTranscriber, get_model, transcribe_watched

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("meetcfg.main")

# repo_root/frontend
FRONTEND = Path(__file__).resolve().parents[2] / "frontend"

app = FastAPI(title="meetcfg")
app.mount("/static", StaticFiles(directory=str(FRONTEND / "static")), name="static")


@app.on_event("startup")
async def _warmup() -> None:
    # Load whisper at boot so the first utterance isn't slow.
    await asyncio.to_thread(get_model)


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(str(FRONTEND / "index.html"))


@app.get("/health")
async def health() -> dict:
    from .llm import provider_name
    return {
        "status": "ok",
        "model": settings.whisper_model,
        "device": settings.whisper_device,
        "compute_type": settings.whisper_compute_type,
        "language": settings.whisper_language,
        "model_loaded": get_model() is not None,
        "provider": provider_name(),
        "llm_cmd": settings.custom_llm_cmd,
    }


@app.get("/api/config")
async def api_config() -> dict:
    return {"user_name": settings.user_name}


def _session_for_id(meeting_id: str) -> meeting.MeetingSession:
    session = meeting.get_or_create(meeting_id)
    if session.engine is None:
        session.engine = CopilotEngine(session)
    return session


def _session_for(ws: WebSocket) -> meeting.MeetingSession:
    return _session_for_id(ws.query_params.get("meeting", "default"))


@app.websocket("/ws/audio")
async def ws_audio(ws: WebSocket) -> None:
    await ws.accept()
    source = ws.query_params.get("source", "mic")
    session = _session_for(ws)
    st = StreamingTranscriber(source=source)
    work: asyncio.Queue[object] = asyncio.Queue()
    log.info("ws connected source=%s", source)

    async def transcriber_worker() -> None:
        """Pull completed utterances and transcribe them off the receive loop."""
        while True:
            utt = await work.get()
            if utt is None:  # shutdown sentinel
                return
            try:
                text = await transcribe_watched(utt.audio)
            except Exception:  # noqa: BLE001 - never kill the socket on a transcription error
                log.exception("transcription failed")
                continue
            if not text:
                continue
            action, line_id, retract_id = session.ingest(source, text)
            if action == "suppress":
                continue
            payload = {
                "type": "transcript", "source": source, "text": text, "id": line_id,
                "t0": round(utt.t0, 2), "t1": round(utt.t1, 2),
            }
            # The UI reads transcripts off the copilot socket; this echo back on
            # the audio socket is what lets tools/feed_wav.py see them without a
            # browser. It MUST be guarded: on the final drain below the client is
            # already gone, and an unguarded send would raise out of the endpoint
            # and skip the broadcast, losing the last utterance.
            try:
                await ws.send_text(json.dumps(payload))
            except Exception:  # noqa: BLE001 - client already disconnected
                pass
            await session.broadcast(payload)
            if retract_id is not None:
                await session.broadcast({"type": "retract", "id": retract_id})

    worker = asyncio.create_task(transcriber_worker())
    await ws.send_text(json.dumps({"type": "status", "source": source, "msg": "connected"}))

    try:
        while True:
            data = await ws.receive_bytes()
            for utt in st.add_pcm(data):
                work.put_nowait(utt)
    except WebSocketDisconnect:
        log.info("ws disconnected source=%s", source)
    finally:
        final = st.flush_final()
        if final is not None:
            work.put_nowait(final)
        await work.put(None)   # let the worker drain, then stop it
        try:
            await asyncio.wait_for(worker, timeout=30)
        except asyncio.TimeoutError:
            worker.cancel()


@app.websocket("/ws/copilot")
async def ws_copilot(ws: WebSocket) -> None:
    await ws.accept()
    session = _session_for(ws)
    session.listeners.add(ws)
    log.info("copilot ws connected meeting=%s", session.meeting_id)
    try:
        # A refreshed page still sees the latest orientation.
        if session.last_advice:
            await ws.send_text(json.dumps({**session.last_advice, "replay": True}))
        await ws.send_text(json.dumps({"type": "copilot_status", "state": "idle"}))
        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if msg.get("type") == "advise_now":
                session.engine.poke(force=True)
    except WebSocketDisconnect:
        pass
    finally:
        session.listeners.discard(ws)
        log.info("copilot ws disconnected meeting=%s", session.meeting_id)


class InjectLine(BaseModel):
    meeting: str = "test"
    source: str = "system"   # "system" = the lead, "mic" = Tiago
    text: str


@app.post("/api/inject")
async def api_inject(line: InjectLine) -> dict:
    """Test hook: push a transcript line straight in, bypassing audio.

    Used by tools/inject_transcript.py to exercise transcript -> Codex -> UI
    without a real meeting. Harmless in normal use (nothing calls it).
    """
    session = _session_for_id(line.meeting)
    action, line_id, retract_id = session.ingest(line.source, line.text)
    if action != "suppress":
        await session.broadcast({
            "type": "transcript", "source": line.source, "text": line.text,
            "id": line_id, "t0": 0, "t1": 0,
        })
    return {"action": action, "id": line_id, "retract": retract_id}
