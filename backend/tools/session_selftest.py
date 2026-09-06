"""Conversation-session/adapter contract regression test (fully local)."""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import meeting  # noqa: E402
from app.conversation import (  # noqa: E402
    AdapterBinding,
    AdapterContractError,
    CHANNEL_PHONE,
    ConversationInputAdapter,
    GOOGLE_MEET_ADAPTER,
    INVALID_PCM,
    INVALID_CHANNELS,
    INVALID_SAMPLE_RATE,
    INVALID_SAMPLE_FORMAT,
    PCM16kFrame,
    ROLE_COUNTERPARTY,
    ROLE_OPERATOR,
    RoleMappedAdapter,
    SOURCE_DISCONNECTED,
    SOURCE_HEALTH_DISCONNECTED,
    TranscriptEvent,
    UNKNOWN_SOURCE,
    validate_pcm16k,
)
from app.copilot.context import CHANNELS as ACQUISITION_CHANNELS  # noqa: E402
from app.copilot.conversion import apply_lines, live_commitments  # noqa: E402
from app.copilot.engine import CopilotEngine  # noqa: E402
from app.copilot import engine as engine_module  # noqa: E402
from app.config import settings  # noqa: E402

FAILS = 0


def check(name: str, got, want) -> None:
    global FAILS
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {name}: got {got!r}, want {want!r}")
    if not ok:
        FAILS += 1


class FakeEngine:
    def __init__(self) -> None:
        self.pokes = 0
        self.stops = 0

    def poke(self) -> None:
        self.pokes += 1

    def stop(self) -> None:
        self.stops += 1


PHONE_FAKE = RoleMappedAdapter(
    adapter_id="fake_phone",
    conversation_channel=CHANNEL_PHONE,
    source_roles={"agent_rx": ROLE_OPERATOR, "caller_rx": ROLE_COUNTERPARTY},
)


def event(adapter, session_id: str, source: str, text: str, event_id: str | None = None):
    return TranscriptEvent(adapter.bind(session_id, source), text, event_id)


def test_contract_and_meet_mapping() -> None:
    print("adapter seam: PCM 16 kHz, source health, Meet mapping, phone-ready fake")
    check("Google Meet satisfies adapter interface",
          isinstance(GOOGLE_MEET_ADAPTER, ConversationInputAdapter), True)
    check("fake phone satisfies adapter interface",
          isinstance(PHONE_FAKE, ConversationInputAdapter), True)
    mic = GOOGLE_MEET_ADAPTER.bind("wire-session", "mic")
    system = GOOGLE_MEET_ADAPTER.bind("wire-session", "system")
    check("Meet mic -> operator", mic.role, ROLE_OPERATOR)
    check("Meet system -> counterparty", system.role, ROLE_COUNTERPARTY)
    check("Meet channel normalized", mic.conversation_channel, "google_meet")
    check("wire meeting id preserved", mic.session_id, "wire-session")
    check("wire physical source preserved", system.physical_source, "system")
    check("valid mono PCM16 frame", PCM16kFrame(mic, b"\x00\x00" * 160).validate(), "")
    check("empty PCM frame refused", validate_pcm16k(mic, b""), INVALID_PCM)
    check("odd byte frame refused", validate_pcm16k(mic, b"\x00"), INVALID_PCM)

    bad_rate = AdapterBinding(**{**mic.__dict__, "sample_rate_hz": 8_000})
    check("non-16k binding refused", bad_rate.validate(), INVALID_SAMPLE_RATE)
    bad_format = AdapterBinding(**{**mic.__dict__, "sample_format": "f32le"})
    check("non-s16le binding refused", bad_format.validate(), INVALID_SAMPLE_FORMAT)
    stereo = AdapterBinding(**{**mic.__dict__, "channels": 2})
    check("non-mono binding refused", stereo.validate(), INVALID_CHANNELS)
    disconnected = AdapterBinding(
        **{**mic.__dict__, "source_health": SOURCE_HEALTH_DISCONNECTED}
    )
    check("disconnected source refused", disconnected.validate(), SOURCE_DISCONNECTED)
    forged_meet = AdapterBinding(
        **{**mic.__dict__, "role": ROLE_COUNTERPARTY}
    )
    check("forged Meet mic cannot impersonate counterparty",
          forged_meet.validate(), "UNKNOWN_ROLE")
    forged_phone = AdapterBinding(
        session_id="wire-session", conversation_channel="phone",
        role=ROLE_COUNTERPARTY, physical_source="intruder", adapter_id="fake_phone",
    )
    check("hand-built phone binding cannot bypass adapter mapping",
          forged_phone.validate(), UNKNOWN_SOURCE)
    reason = ""
    try:
        GOOGLE_MEET_ADAPTER.bind("wire-session", "third-speaker")
    except AdapterContractError as exc:
        reason = exc.reason
    check("unknown Meet source fail closed", reason, UNKNOWN_SOURCE)
    check("PHONE not commercial acquisition channel", "PHONE" in ACQUISITION_CHANNELS, False)
    check("GOOGLE_MEET not commercial acquisition channel",
          "GOOGLE_MEET" in ACQUISITION_CHANNELS, False)


def test_roles_trigger_and_echo() -> None:
    print("role normalization: operator-only is silent; echo is not counterparty proof")
    meeting.reset_sessions()
    s = meeting.get_or_create("roles")
    engine = FakeEngine()
    s.engine = engine
    got = s.ingest_event(event(PHONE_FAKE, "roles", "agent_rx", "Vou conduzir a pauta."))
    check("operator accepted", got.action, "accept")
    check("operator-only does not poke engine", engine.pokes, 0)
    check("operator-only counterparty chars", s.counterparty_chars(), 0)

    got = s.ingest_event(event(
        PHONE_FAKE, "roles", "caller_rx", "Precisamos do memorial até sexta-feira.",
    ))
    check("counterparty accepted", got.action, "accept")
    check("counterparty pokes the one engine", engine.pokes, 1)
    check("normalized phone role stored", s.lines[-1].role, ROLE_COUNTERPARTY)
    check("physical phone source stored", s.lines[-1].source, "caller_rx")

    meeting.reset_sessions()
    echo = meeting.get_or_create("echo-role")
    echo_engine = FakeEngine()
    echo.engine = echo_engine
    first = echo.ingest_event(event(
        PHONE_FAKE, "echo-role", "agent_rx",
        "Você envia o memorial de cálculo até sexta-feira.",
    ))
    second = echo.ingest_event(event(
        PHONE_FAKE, "echo-role", "caller_rx",
        "Você envia o memorial de cálculo até sexta-feira.",
    ))
    check("operator echo accepted first", first.action, "accept")
    check("counterparty copy retracts operator", second.action, "accept_retract")
    check("echo survivor marked derived", echo.lines[-1].echo_derived, True)
    check("echo-derived counterparty does not poke", echo_engine.pokes, 0)
    check("echo never mutually confirms", live_commitments(echo.conversion_state or {}), [])

    meeting.reset_sessions()
    late = meeting.get_or_create("late-echo")
    late.engine = FakeEngine()
    late.ingest_event(event(
        PHONE_FAKE, "late-echo", "agent_rx",
        "Vou mandar a planta técnica na sexta-feira.",
    ))
    late.ingest_event(event(
        PHONE_FAKE, "late-echo", "caller_rx",
        "Combinado, você envia o memorial de cálculo até sexta-feira.",
    ))
    check("echo grace blocks transient mutual confirmation",
          live_commitments(late.conversion_state or {}), [])
    real_engine = CopilotEngine(late)
    real_engine._consumed = late.counterparty_chars()
    late.engine = real_engine
    suppressed = late.ingest_event(event(
        PHONE_FAKE, "late-echo", "agent_rx",
        "Combinado, você envia o memorial de cálculo até sexta-feira.",
    ))
    check("later operator duplicate suppressed", suppressed.action, "suppress")
    check("earlier counterparty retroactively echo-derived",
          late.lines[-1].echo_derived, True)
    check("late-discovered echo removes mutual confirmation",
          live_commitments(late.conversion_state or {}), [])
    check("late-discovered echo no longer counts as trigger material",
          late.counterparty_chars(), 0)
    check("late echo rebases engine trigger watermark", real_engine._consumed, 0)

    async def inflight_echo() -> None:
        meeting.reset_sessions()
        session = meeting.get_or_create("inflight-echo")
        session.ingest_event(event(
            PHONE_FAKE, "inflight-echo", "agent_rx",
            "Vou mandar a planta técnica na sexta-feira.",
        ))
        session.ingest_event(event(
            PHONE_FAKE, "inflight-echo", "caller_rx",
            "Combinado, você envia o memorial de cálculo até sexta-feira.",
        ))
        copilot = CopilotEngine(session)
        session.engine = copilot
        started = asyncio.Event()
        release = asyncio.Event()

        async def fake_generate(_system: str, _prompt: str):
            started.set()
            await release.wait()
            return ("SINAL: eco\nFAÇA: nada\nDIGA: \"nada\"", {})

        original_generate = engine_module.generate
        engine_module.generate = fake_generate
        try:
            run = asyncio.create_task(copilot._run_once())
            await started.wait()
            session.ingest_event(event(
                PHONE_FAKE, "inflight-echo", "agent_rx",
                "Combinado, você envia o memorial de cálculo até sexta-feira.",
            ))
            release.set()
            check("in-flight echo-invalidated run completes", await run, True)
            check("in-flight echo advice is not published", session.last_advice, None)
        finally:
            engine_module.generate = original_generate

    asyncio.run(inflight_echo())

    async def settled_trigger() -> None:
        original = (
            settings.echo_window_s,
            settings.suggest_min_new_chars,
            settings.suggest_min_interval_s,
            settings.suggest_enabled,
            engine_module.generate,
        )
        calls = 0

        async def fake_generate(_system: str, _prompt: str):
            nonlocal calls
            calls += 1
            return ("SINAL: novo\nFAÇA: ouvir\nDIGA: \"conte mais\"", {})

        settings.echo_window_s = 0.08
        settings.suggest_min_new_chars = 1
        settings.suggest_min_interval_s = 0
        settings.suggest_enabled = True
        engine_module.generate = fake_generate
        echo_copilot = None
        legit_copilot = None
        try:
            meeting.reset_sessions()
            echo_session = meeting.get_or_create("settled-echo")
            echo_copilot = CopilotEngine(echo_session)
            echo_session.engine = echo_copilot
            echo_session.ingest_event(event(
                PHONE_FAKE, "settled-echo", "caller_rx",
                "A contraparte provisória ainda não pode disparar.",
            ))
            await asyncio.sleep(0.01)
            check("pending counterparty does not call LLM", calls, 0)
            echo_session.ingest_event(event(
                PHONE_FAKE, "settled-echo", "agent_rx",
                "A contraparte provisória ainda não pode disparar.",
            ))
            await asyncio.sleep(0.12)
            check("echo resolved inside grace never calls LLM", calls, 0)
            check("echo resolved inside grace leaves no advice",
                  echo_session.last_advice, None)

            legit_session = meeting.get_or_create("settled-legit")
            legit_copilot = CopilotEngine(legit_session)
            legit_session.engine = legit_copilot
            legit_session.ingest_event(event(
                PHONE_FAKE, "settled-legit", "caller_rx",
                "Fala legítima da contraparte após o grace.",
            ))
            await asyncio.sleep(0.12)
            check("settled counterparty calls LLM once", calls, 1)
            check("settled counterparty publishes advice",
                  isinstance(legit_session.last_advice, dict), True)
        finally:
            if echo_copilot is not None:
                echo_copilot.stop()
            if legit_copilot is not None:
                legit_copilot.stop()
            (
                settings.echo_window_s,
                settings.suggest_min_new_chars,
                settings.suggest_min_interval_s,
                settings.suggest_enabled,
                engine_module.generate,
            ) = original

    asyncio.run(settled_trigger())

    meeting.reset_sessions()
    mature = meeting.get_or_create("mature-grace")
    mature.ingest_event(event(
        PHONE_FAKE, "mature-grace", "agent_rx",
        "Vou mandar a planta técnica na sexta-feira.",
    ))
    mature.ingest_event(event(
        PHONE_FAKE, "mature-grace", "caller_rx",
        "Combinado, você envia o memorial de cálculo até sexta-feira.",
    ))
    check("live pair provisional inside grace",
          live_commitments(mature.conversion_state or {}), [])
    mature.lines[-1].echo_pending_until = time.time() - 1
    mature.refresh_conversion()
    check("non-echo pair confirms after grace",
          bool(live_commitments(mature.conversion_state or {})), True)


def test_two_sessions_two_adapters_and_replay_100() -> None:
    print("two sessions/two adapters: isolation and deterministic replay 100x")
    meeting.reset_sessions()
    meet = meeting.get_or_create("meet-A")
    phone = meeting.get_or_create("phone-B")
    meet.engine = FakeEngine()
    phone.engine = FakeEngine()
    meet.meeting_plan = {"marker": "meet-only"}
    phone.meeting_plan = {"marker": "phone-only"}

    meet_result = meet.ingest_event(event(
        GOOGLE_MEET_ADAPTER, "meet-A", "system", "Pergunta exclusiva do Meet.", "m-1",
    ))
    phone_result = phone.ingest_event(event(
        PHONE_FAKE, "phone-B", "caller_rx", "Pergunta exclusiva do telefone.", "p-1",
    ))
    check("Meet accepted", meet_result.action, "accept")
    check("phone accepted", phone_result.action, "accept")
    check("sessions are distinct", meet is phone, False)
    check("engines are distinct", meet.engine is phone.engine, False)
    check("Meet only has Meet line", [ln.conversation_channel for ln in meet.lines],
          ["google_meet"])
    check("phone only has phone line", [ln.conversation_channel for ln in phone.lines],
          ["phone"])
    check("plans do not leak", phone.meeting_plan.get("marker"), "phone-only")

    wrong_session = meet.ingest_event(event(
        PHONE_FAKE, "phone-B", "caller_rx", "Não pode vazar.", "wrong-1",
    ))
    check("cross-session event rejected", wrong_session.reason, "SESSION_MISMATCH")
    check("cross-session line not stored", len(meet.lines), 1)

    explicit_bad_role = TranscriptEvent(
        AdapterBinding(
            session_id="meet-A", conversation_channel="google_meet",
            role="intruder", physical_source="system", adapter_id="google_meet",
        ),
        "Também não pode vazar.",
    )
    bad_role = meet.ingest_event(explicit_bad_role)
    check("explicit unknown role is not rescued by legacy source",
          bad_role.reason, "UNKNOWN_ROLE")
    check("unknown role line not stored", len(meet.lines), 1)
    invalid_conversion = apply_lines(None, [{
        "role": "intruder", "source": "system", "text": "Combinado.",
    }])
    check("conversion ignores explicit unknown role despite legacy source",
          invalid_conversion.get("board"), [])

    replayed = 0
    for _ in range(100):
        replay = phone.ingest_event(event(
            PHONE_FAKE, "phone-B", "caller_rx", "Pergunta exclusiva do telefone.", "p-1",
        ))
        replayed += int(replay.action == "replay" and replay.reason == "REPLAY")
    check("100 phone replays recognized after first ingest", replayed, 100)
    meet_replayed = 0
    for _ in range(100):
        replay = meet.ingest_event(event(
            GOOGLE_MEET_ADAPTER, "meet-A", "system",
            "Pergunta exclusiva do Meet.", "m-1",
        ))
        meet_replayed += int(replay.action == "replay")
    check("100 Meet replays recognized after first ingest", meet_replayed, 100)
    check("101 phone deliveries produce one line", len(phone.lines), 1)
    check("101 Meet deliveries produce one line", len(meet.lines), 1)
    check("replay does not retrigger engine", phone.engine.pokes, 1)
    check("same event id isolated by session", len(meet.lines), 1)

    # Event ids are source-local: mic/system (or rx/tx) may both emit "1".
    other_source_same_id = phone.ingest_event(event(
        PHONE_FAKE, "phone-B", "agent_rx", "Source oposto com mesmo id.", "p-1",
    ))
    check("same event id on another physical source is distinct",
          other_source_same_id.action, "accept")


def test_channel_switch_and_prune() -> None:
    print("channel switch/prune: no cross-channel echo or retained session state")
    meeting.reset_sessions()
    switched = meeting.get_or_create("switch")
    switched.ingest_event(event(
        GOOGLE_MEET_ADAPTER, "switch", "system", "Mesmo texto substancial entre canais.",
    ))
    phone_copy = switched.ingest_event(event(
        PHONE_FAKE, "switch", "agent_rx", "Mesmo texto substancial entre canais.",
    ))
    check("channel switch does not cross-suppress", phone_copy.action, "accept")
    check("one core can observe two channels",
          {line.conversation_channel for line in switched.lines}, {"google_meet", "phone"})

    doomed = meeting.get_or_create("prune-me")
    keeper = meeting.get_or_create("keep-listener")
    doomed_engine = FakeEngine()
    doomed.engine = doomed_engine
    doomed.last_activity = time.time() - 10
    keeper.last_activity = time.time() - 10
    keeper.listeners.add(object())
    pruned = meeting.prune_sessions(now=time.time(), idle_s=1)
    check("idle unobserved session pruned", "prune-me" in pruned, True)
    check("prune stops its engine", doomed_engine.stops, 1)
    check("listener session retained", meeting.get("keep-listener") is keeper, True)

    streaming = meeting.get_or_create("keep-input")
    stream_binding = GOOGLE_MEET_ADAPTER.bind("keep-input", "system")
    check("live input attaches", streaming.attach_input(stream_binding), "")
    streaming.last_activity = time.time() - 10
    meeting.prune_sessions(now=time.time(), idle_s=1)
    check("live audio input prevents prune", meeting.get("keep-input") is streaming, True)
    streaming.detach_input(stream_binding)
    streaming.last_activity = time.time() - 10
    check("detached idle input can prune",
          "keep-input" in meeting.prune_sessions(now=time.time(), idle_s=1), True)
    fresh = meeting.get_or_create("prune-me")
    check("switch after prune gets fresh session", fresh is doomed, False)
    check("fresh session has no lines", fresh.lines, [])


def test_wire_compatibility() -> None:
    print("Meet wire compatibility: URLs, meeting id and source=mic|system")
    from fastapi.testclient import TestClient
    from app.main import app

    meeting.reset_sessions()
    client = TestClient(app)
    with client.websocket_connect("/ws/audio?meeting=legacy-wire&source=mic") as ws:
        status = ws.receive_json()
        check("legacy /ws/audio URL connects", status.get("msg"), "connected")
        check("legacy source remains mic", status.get("source"), "mic")
        check("additive role is operator", status.get("role"), ROLE_OPERATOR)
        check("additive channel is google_meet",
              status.get("conversation_channel"), "google_meet")
    check("legacy meeting id preserved", meeting.get("legacy-wire").meeting_id, "legacy-wire")
    check("closed websocket detaches active input",
          meeting.get("legacy-wire").active_inputs, 0)

    with client.websocket_connect("/ws/audio?meeting=invalid-pcm&source=system") as ws:
        ws.receive_json()  # connected
        ws.send_bytes(b"\x00")
        check("invalid PCM is refused on legacy websocket",
              ws.receive_json().get("reason"), INVALID_PCM)

    injected = client.post("/api/inject", json={
        "meeting": "legacy-wire", "source": "system", "text": "Texto do lead.",
    })
    check("legacy /api/inject accepts system", injected.status_code, 200)
    check("legacy system stored as physical source",
          meeting.get("legacy-wire").lines[-1].source, "system")
    check("legacy system normalized", meeting.get("legacy-wire").lines[-1].role,
          ROLE_COUNTERPARTY)


def main() -> int:
    test_contract_and_meet_mapping()
    print()
    test_roles_trigger_and_echo()
    print()
    test_two_sessions_two_adapters_and_replay_100()
    print()
    test_channel_switch_and_prune()
    print()
    test_wire_compatibility()
    if FAILS:
        print(f"\n{FAILS} FAILURES")
        return 1
    print("\nALL PASS")
    print("CONVERSATION_CORE=PASS")
    print("WIRE_COMPATIBILITY=PASS")
    print("ROLE_NORMALIZATION=PASS")
    print("REPLAY_100=PASS")
    print("PHONE_ADAPTER_CONTRACT=PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
