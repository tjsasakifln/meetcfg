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
from typing import Literal

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import meeting
from .config import settings
from .copilot.engine import CopilotEngine
from .copilot import handraiser
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
    if settings.whisper_warmup:
        await asyncio.to_thread(get_model)
    # Opt-in producer pull once. Failure keeps manual mode; never blocks boot.
    if settings.handraiser_consumer_enabled and handraiser.producer_configured():
        try:
            await asyncio.to_thread(handraiser.refresh_conversations)
        except Exception:  # noqa: BLE001 - producer must not take the app down
            log.info("handraiser startup fetch failed; manual mode preserved")


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
    # Token, URL userinfo, and producer secrets never leave the backend.
    return {
        "user_name": settings.user_name,
        "handraiser_consumer_enabled": bool(settings.handraiser_consumer_enabled),
        "conversion_enabled": bool(settings.conversion_enabled),
        "warmbly_configured": handraiser.producer_configured(),
    }


def _session_for_id(meeting_id: str) -> meeting.MeetingSession:
    session = meeting.get_or_create(meeting_id)
    if session.engine is None:
        session.engine = CopilotEngine(session)
    if not session.handraiser_id and meeting_id.startswith(handraiser.SESSION_PREFIX):
        rec = handraiser.get_store().get(meeting_id[len(handraiser.SESSION_PREFIX):])
        if rec is not None:
            session.handraiser_id = rec.handraiser_id
            session.handraiser_context = rec.dossier
            session.handraiser_version = rec.version
            plan = rec.dossier.get("meeting_plan") if isinstance(rec.dossier, dict) else None
            session.meeting_plan = plan if isinstance(plan, dict) else None
    return session


def _session_for(ws: WebSocket) -> meeting.MeetingSession:
    return _session_for_id(ws.query_params.get("meeting", "default"))


@app.websocket("/ws/audio")
async def ws_audio(ws: WebSocket) -> None:
    await ws.accept()
    source = ws.query_params.get("source", meeting.SOURCE_MIC)
    if not meeting.is_valid_source(source):
        # An unrecognised channel must not open a stream: downstream it would
        # look like a third speaker and could confirm a next step by itself.
        log.warning("ws refused: unknown source=%r", source)
        await ws.send_text(json.dumps({
            "type": "error", "reason": "UNKNOWN_SOURCE",
            "allowed": list(meeting.VALID_SOURCES),
        }))
        await ws.close(code=1008)
        return
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
            if action in ("suppress", "reject"):
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
        ctx = _conversation_payload(session)
        if ctx is not None:
            await ws.send_text(json.dumps(ctx))
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


def _conversation_payload(session: meeting.MeetingSession) -> dict | None:
    dossier = handraiser.context_for_session(
        session,
        refresh=False,
        enabled=bool(settings.handraiser_consumer_enabled),
    )
    if dossier is None:
        return None
    conv = handraiser.render_conversation_layer(dossier)
    return {
        "type": "handraiser_context",
        "handraiser_id": getattr(session, "handraiser_id", None) or conv.get("handraiser_id"),
        "version": getattr(session, "handraiser_version", 0),
        "conversation": conv,
        "empresa": conv.get("empresa"),
        "por_que_chegou_agora": conv.get("por_que_chegou_agora"),
        "intencao": conv.get("intencao"),
        "fatos_verificaveis": conv.get("fatos_verificaveis"),
        "o_que_nao_sabemos": conv.get("o_que_nao_sabemos"),
        "oportunidade_contrato": conv.get("oportunidade_contrato"),
        "ultimo_touch_outcome": conv.get("ultimo_touch_outcome"),
        "proximo_estado_comercial": conv.get("proximo_estado_comercial"),
        "inbound_only": conv.get("inbound_only"),
        "canal": conv.get("canal"),
        "freshness": conv.get("freshness"),
        "status": conv.get("status"),
        "identity_ref": conv.get("identity_ref"),
        "resumo": conv.get("resumo"),
        "nucleo": conv.get("nucleo"),
        "nucleo_id": conv.get("nucleo_id"),
        "nucleo_problema": conv.get("nucleo_problema"),
        "proximo_estado": conv.get("proximo_estado"),
        "source": conv.get("source"),
        "schema": conv.get("schema"),
        "meeting_plan": (
            session.meeting_plan
            if isinstance(getattr(session, "meeting_plan", None), dict)
            else (dossier.get("meeting_plan") if isinstance(dossier, dict) else None)
        ),
        "meeting_plan_reason": (
            dossier.get("meeting_plan_reason") if isinstance(dossier, dict) else None
        ),
    }


def _consume_kwargs() -> dict:
    return {
        "enabled": bool(settings.handraiser_consumer_enabled),
        "max_bytes": int(settings.handraiser_max_payload_bytes),
        "freshness_max_age_s": float(settings.handraiser_freshness_max_age_s),
        "bind_session": True,
    }


@app.get("/api/handraiser/list")
async def api_handraiser_list() -> dict:
    """Short accepted-conversation list. Empty is valid (manual mode)."""
    return {
        "ok": True,
        "reason": "",
        "fetch": handraiser.get_fetch_state(),
        "conversations": handraiser.list_conversations(),
        "warmbly_configured": handraiser.producer_configured(),
        "enabled": bool(settings.handraiser_consumer_enabled),
    }


@app.post("/api/handraiser/refresh")
async def api_handraiser_refresh():
    """Explicit pull from Warmbly. Not called per copilot orientation."""
    result = await asyncio.to_thread(
        handraiser.refresh_conversations,
        enabled=bool(settings.handraiser_consumer_enabled),
        bind_session=False,
    )
    body = result.as_http()
    if not result.ok:
        code = 409 if result.reason == handraiser.CONSUMER_DISABLED else 400
        if result.reason == handraiser.PRODUCER_NOT_CONFIGURED:
            code = 200  # app stays up in manual mode
            body["ok"] = True
        return JSONResponse(body, status_code=code)
    return body


@app.post("/api/handraiser/ingest")
async def api_handraiser_ingest(request: Request):
    """Accept one hand-raiser payload. Collection/rejected/UNKNOWN fail closed."""
    raw = await request.body()
    max_bytes = int(settings.handraiser_max_payload_bytes)
    if len(raw) > max_bytes:
        log.info("handraiser ingest refused reason=%s bytes=%s", handraiser.OVERSIZED, len(raw))
        return JSONResponse(
            {"ok": False, "reason": handraiser.OVERSIZED, "session_id": None},
            status_code=413,
        )
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        log.info("handraiser ingest refused reason=%s", handraiser.MALFORMED)
        return JSONResponse(
            {"ok": False, "reason": handraiser.MALFORMED, "session_id": None},
            status_code=400,
        )
    result = handraiser.consume(payload, raw_size=len(raw), **_consume_kwargs())
    body = result.as_http()
    if not result.ok:
        return JSONResponse(body, status_code=409 if result.reason == handraiser.CONSUMER_DISABLED else 400)
    return body


@app.get("/api/handraiser/{handraiser_id}")
async def api_handraiser_get(handraiser_id: str):
    """Already-accepted context stays readable after the consumer is disabled."""
    rec = handraiser.get_store().get(handraiser_id)
    if rec is None:
        return JSONResponse({"ok": False, "reason": handraiser.NOT_FOUND, "session_id": None}, status_code=404)
    conv = rec.conversation
    return {
        "ok": True,
        "reason": "",
        "handraiser_id": rec.handraiser_id,
        "session_id": rec.session_id,
        "receipt": rec.receipt,
        "version": rec.version,
        "inbound_only": rec.inbound_only,
        "conversation": conv,
        "empresa": conv.get("empresa"),
        "intencao": conv.get("intencao"),
        "proximo_estado_comercial": conv.get("proximo_estado_comercial"),
        "por_que_chegou_agora": conv.get("por_que_chegou_agora"),
        "canal": conv.get("canal"),
        "freshness": conv.get("freshness"),
        "status": conv.get("status"),
        "inbound_only": rec.inbound_only,
        "resumo": conv.get("resumo"),
        "nucleo": conv.get("nucleo"),
        "nucleo_id": conv.get("nucleo_id"),
        "proximo_estado": conv.get("proximo_estado"),
        "source": conv.get("source"),
        "schema": conv.get("schema"),
    }


class SelectHandraiser(BaseModel):
    handraiser_id: str


@app.post("/api/handraiser/select")
async def api_handraiser_select(body: SelectHandraiser) -> dict:
    rec = handraiser.get_store().get(body.handraiser_id)
    if rec is None:
        return JSONResponse({"ok": False, "reason": handraiser.NOT_FOUND, "session_id": None}, status_code=404)
    session = _session_for_id(rec.session_id)
    session.handraiser_id = rec.handraiser_id
    session.handraiser_context = rec.dossier
    session.handraiser_version = rec.version
    plan = rec.dossier.get("meeting_plan") if isinstance(rec.dossier, dict) else None
    session.meeting_plan = plan if isinstance(plan, dict) else None
    conv = rec.conversation
    log.info("handraiser selected handraiser_id=%s session_id=%s", rec.handraiser_id, rec.session_id)
    return {
        "ok": True,
        "reason": "",
        "session_id": rec.session_id,
        "handraiser_id": rec.handraiser_id,
        "version": rec.version,
        "conversation": conv,
        "empresa": conv.get("empresa"),
        "intencao": conv.get("intencao"),
        "proximo_estado_comercial": conv.get("proximo_estado_comercial"),
        "canal": conv.get("canal"),
        "freshness": conv.get("freshness"),
        "status": conv.get("status"),
        "inbound_only": rec.inbound_only,
        "resumo": conv.get("resumo"),
        "nucleo": conv.get("nucleo"),
        "nucleo_id": conv.get("nucleo_id"),
        "proximo_estado": conv.get("proximo_estado"),
        "source": conv.get("source"),
        "schema": conv.get("schema"),
    }


@app.get("/api/session/context")
async def api_session_context(meeting: str = "default") -> dict:
    session = meeting_mod_get(meeting)
    if session is None:
        # Selecting a known hr:* meeting materializes the bound context.
        rec = None
        if meeting.startswith(handraiser.SESSION_PREFIX):
            rec = handraiser.get_store().get(meeting[len(handraiser.SESSION_PREFIX):])
        if rec is None:
            return JSONResponse({"ok": False, "reason": handraiser.NOT_FOUND, "session_id": None}, status_code=404)
        session = _session_for_id(rec.session_id)
        session.handraiser_id = rec.handraiser_id
        session.handraiser_context = rec.dossier
        session.handraiser_version = rec.version
        plan = rec.dossier.get("meeting_plan") if isinstance(rec.dossier, dict) else None
        session.meeting_plan = plan if isinstance(plan, dict) else None
    payload = _conversation_payload(session)
    if payload is None:
        return JSONResponse({"ok": False, "reason": handraiser.NOT_FOUND, "session_id": session.meeting_id},
                            status_code=404)
    return {"ok": True, "reason": "", "session_id": session.meeting_id, **payload}


def meeting_mod_get(meeting_id: str):
    return meeting.get(meeting_id)


class InjectLine(BaseModel):
    meeting: str = "test"
    # "system" = the lead, "mic" = Tiago. Closed enum: an unknown channel is
    # not a third speaker and must never take part in mutual confirmation.
    source: Literal["mic", "system"] = "system"
    text: str


@app.post("/api/inject")
async def api_inject(line: InjectLine) -> dict:
    """Test hook: push a transcript line straight in, bypassing audio.

    Used by tools/inject_transcript.py to exercise transcript -> Codex -> UI
    without a real meeting. Harmless in normal use (nothing calls it).
    """
    session = _session_for_id(line.meeting)
    action, line_id, retract_id = session.ingest(line.source, line.text)
    if action == "reject":
        return JSONResponse(
            {"ok": False, "reason": "UNKNOWN_SOURCE", "action": action,
             "allowed": list(meeting.VALID_SOURCES)},
            status_code=422,
        )
    if action != "suppress":
        await session.broadcast({
            "type": "transcript", "source": line.source, "text": line.text,
            "id": line_id, "t0": 0, "t1": 0,
        })
        conv = getattr(session, "conversion_state", None)
        if isinstance(conv, dict):
            await session.broadcast({
                "type": "conversion_update",
                "board": conv.get("board") or [],
                "pending_questions": conv.get("pending_questions") or [],
            })
    return {"action": action, "id": line_id, "retract": retract_id}


class PostCallRequest(BaseModel):
    meeting: str = "default"


@app.post("/api/postcall")
async def api_postcall(req: PostCallRequest) -> dict:
    """Relatório do pós-chamada — só sob ação explícita, nunca automático.

    Precisa rodar aqui porque a transcrição só existe na memória desta sessão.
    Nada é gravado em disco nem enviado a lugar nenhum: o relatório volta nesta
    resposta e quem quiser guardar, guarda.
    """
    from .copilot.postcall import generate_post_call_report
    session = _session_for_id(req.meeting)
    return await generate_post_call_report(session)


class MeetingEndRequest(BaseModel):
    meeting: str = "default"


@app.post("/api/meeting/end")
async def api_meeting_end(req: MeetingEndRequest) -> dict:
    """Operational snapshot for Tiago — only on explicit action, never automatic.

    No calendar, no SMTP, no final proposal, no stage write, no raw transcript.
    """
    from .copilot.conversion import crm_side_effect_keys, meeting_plan_of, operational_output
    session = meeting_mod_get(req.meeting)
    if session is None and req.meeting.startswith(handraiser.SESSION_PREFIX):
        session = _session_for_id(req.meeting)
    if session is None:
        return JSONResponse(
            {"ok": False, "reason": handraiser.NOT_FOUND, "session_id": None},
            status_code=404,
        )
    plan = getattr(session, "meeting_plan", None)
    if not isinstance(plan, dict):
        plan, _reason = meeting_plan_of(getattr(session, "handraiser_context", None) or {})
    state = getattr(session, "conversion_state", None)
    if state is None:
        session.refresh_conversion()
        state = session.conversion_state
    body = operational_output(plan, state, explicit=True)
    body["session_id"] = session.meeting_id
    body["handraiser_id"] = getattr(session, "handraiser_id", None)
    if crm_side_effect_keys(body):
        log.info("conversion output refused reason=CRM_SIDE_EFFECT session_id=%s",
                 session.meeting_id)
        return JSONResponse(
            {"ok": False, "reason": "CRM_SIDE_EFFECT", "session_id": session.meeting_id},
            status_code=400,
        )
    log.info(
        "conversion end reason=EXPLICIT session_id=%s decisao=%s live=%s",
        session.meeting_id, body.get("decisao"),
        "yes" if isinstance(body.get("proximo_passo_confirmado"), dict) else "no",
    )
    return body


@app.get("/api/session/conversion")
async def api_session_conversion(meeting: str = "default") -> dict:
    """In-memory meeting plan + next-step board. Readable after conversion rollback."""
    from .copilot.conversion import meeting_plan_of, questions_to_ask
    session = meeting_mod_get(meeting)
    if session is None and meeting.startswith(handraiser.SESSION_PREFIX):
        session = _session_for_id(meeting)
    if session is None:
        return JSONResponse(
            {"ok": False, "reason": handraiser.NOT_FOUND, "session_id": None},
            status_code=404,
        )
    plan = getattr(session, "meeting_plan", None)
    plan_reason = ""
    if not isinstance(plan, dict):
        plan, plan_reason = meeting_plan_of(getattr(session, "handraiser_context", None) or {})
        if isinstance(getattr(session, "handraiser_context", None), dict):
            plan_reason = plan_reason or session.handraiser_context.get("meeting_plan_reason") or ""
    state = getattr(session, "conversion_state", None) or {}
    answered = list(state.get("answered_questions") or []) if isinstance(state, dict) else []
    return {
        "ok": True,
        "reason": plan_reason or (state.get("reason") if isinstance(state, dict) else "") or "",
        "session_id": session.meeting_id,
        "enabled": bool(settings.conversion_enabled),
        "meeting_plan": plan,
        "board": (state.get("board") if isinstance(state, dict) else []) or [],
        "pending_questions": (state.get("pending_questions") if isinstance(state, dict) else []) or [],
        "answered_questions": answered,
        "questions_to_ask": questions_to_ask(plan, answered),
        "commercial_stage": (plan or {}).get("commercial_stage") if isinstance(plan, dict) else None,
    }
