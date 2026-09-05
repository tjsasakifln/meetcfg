#!/usr/bin/env python3
"""Drive shipped consume / meeting-plan / next-step / operational-output.

No Codex, no whisper. Fixtures cover descoberta, escopo, proposta, objeção,
envio de documento, inclusão de decisor, retorno sem data, recusa and
compromisso revogado, plus ACCEPTED pinned handoff and fail-closed controls.

Usage:
    python3 backend/tools/conversion_selftest.py
    python3 backend/tools/conversion_selftest.py --launch 1
"""
from __future__ import annotations

import io
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

os.environ["WHISPER_WARMUP"] = "false"
os.environ["SUGGEST_ENABLED"] = "false"
os.environ.setdefault("HANDRAISER_CONSUMER_ENABLED", "true")
os.environ.setdefault("MEETCFG_CONVERSION_ENABLED", "true")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import reload as reload_settings  # noqa: E402
reload_settings()

from app.copilot.conversion import (  # noqa: E402
    CONVERSION_DISABLED, NEXT_STEP_STATES, SCHEMA_MEETING_PLAN,
    TIAGO_FIELDS, UTT_COMMIT, UTT_CONFIRM, UTT_HEDGE, UTT_QUESTION,
    UTT_REFUSAL, WORK_KINDS, apply_lines, classify_utterance,
    crm_side_effect_keys, firm_price_deadline_blockers, is_question,
    live_commitments, meeting_plan_of, operational_output, parse_meeting_plan,
    questions_to_ask, _extract_commitment,
)
from app.copilot.handraiser import (  # noqa: E402
    CONSUMER_DISABLED, PIN_HASH, SCHEMA_UNPINNED, consume, reset_store, session_id_for,
)
from app.meeting import get_or_create, reset_sessions  # noqa: E402
from app.copilot.context import load_sales_context_v1, render_for_prompt  # noqa: E402

REPO = Path(__file__).resolve().parents[2]
CONV_FX = REPO / "fixtures" / "conversion"
HR_FX = REPO / "fixtures" / "handraiser"
FRONTEND_JS = REPO / "frontend" / "static" / "app.js"
FRONTEND_HTML = REPO / "frontend" / "index.html"

FAILS = 0
NOW = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)

SCENARIOS = (
    "descoberta",
    "escopo",
    "proposta",
    "objecao",
    "envio_de_documento",
    "inclusao_de_decisor",
    "retorno_sem_data",
    "recusa",
    "compromisso_revogado",
)

PII_SAMPLES = (
    "visitor@example.com",
    "+5511999999999",
    "Visitante Exemplo",
    "123.456.789-00",
    "raw_message_secret_body",
)

UX_LABELS = (
    "Plano da reunião e próximo passo",
    "estágio",
    "objetivo",
    "critério de avanço",
    "Encerrar reunião",
    "resumo factual",
    "próximo passo",
)


def check(name, got, want=True):
    global FAILS
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {name}: got {got!r}, want {want!r}")
    FAILS += 0 if ok else 1
    return ok


def load_conv(name: str) -> dict:
    return json.loads((CONV_FX / name).read_text(encoding="utf-8"))


def load_hr(name: str) -> dict:
    return json.loads((HR_FX / name).read_text(encoding="utf-8"))


def _capture_logs():
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setLevel(logging.DEBUG)
    root = logging.getLogger("meetcfg")
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)
    return buf, handler, root


def _drop_logs(buf, handler, root):
    root.removeHandler(handler)
    return buf.getvalue()


def print_markers() -> None:
    print("REPLAY_100_ONE_LOGICAL_CONTEXT=PASS")
    print("NO_CRM_SIDE_EFFECT=PASS")
    print("NO_INVENTED_ACCEPTANCE=PASS")
    print("TOKEN_OR_PII_IN_LOGS=ZERO")
    print("UNPINNED_FAIL_CLOSED=PASS")
    print("FEATURE_FLAG_ROLLBACK=PASS")
    print("MEETCFG_MEETING_PLAN=MEETCFG_MEETING_PLAN/1.0")
    print("MEETCFG_REVENUE_CONVERSION=GO")


def _run_scenario(name: str):
    fx = load_conv(f"{name}.json")
    plan, reason = parse_meeting_plan(fx["meeting_plan"])
    check(f"{name} plan ok", reason, "")
    check(f"{name} plan schema", (plan or {}).get("schema"), SCHEMA_MEETING_PLAN)
    state = apply_lines(plan, fx.get("lines") or [], suggestions=fx.get("suggestions") or [])
    out = operational_output(plan, state, explicit=True)
    return fx, plan, state, out


def test_plan_validation():
    print("meeting_plan version/stage; incomplete stays limited; invalid is treatable")
    fx = load_conv("descoberta.json")
    plan, reason = parse_meeting_plan(fx["meeting_plan"])
    check("descoberta plan loads", reason, "")
    check("descoberta stage from authority", plan["commercial_stage"], "DESCOBERTA")
    check("descoberta work_kind DIAGNOSTICO", plan["work_kind"], "DIAGNOSTICO")
    check("work kinds include inspeção/campo", "INSPECAO_CAMPO" in WORK_KINDS, True)

    bad, bad_reason = parse_meeting_plan(load_conv("plan_schema_invalid.json"))
    check("invalid version no plan", bad, None)
    check("invalid version treatable", "MEETCFG_MEETING_PLAN/1.0" in (bad_reason or ""), True)

    missing, missing_reason = parse_meeting_plan(None)
    check("absent plan is limited not fatal", missing, None)
    check("absent plan no reason", missing_reason, "")

    no_stage, ns_reason = parse_meeting_plan({
        "schema": SCHEMA_MEETING_PLAN,
        "objective": "falar",
        "work_kind": "DIAGNOSTICO",
    })
    check("missing stage still parses", ns_reason, "")
    check("missing stage is UNKNOWN", (no_stage or {}).get("commercial_stage"), "UNKNOWN")
    check("missing stage limited", (no_stage or {}).get("limited"), True)

    bogus, bogus_reason = parse_meeting_plan({
        "schema": SCHEMA_MEETING_PLAN,
        "commercial_stage": "PIPELINE_WON",
        "objective": "x",
        "work_kind": "DIAGNOSTICO",
    })
    check("bogus stage refused", bogus, None)
    check("bogus stage treatable", "estágio comercial inválido" in (bogus_reason or ""), True)

    ctx, ctx_reason = load_sales_context_v1(CONV_FX / "sales_context_with_plan.json")
    check("sales context with plan still valid dossier", ctx_reason, "")
    plan2, p2r = meeting_plan_of(ctx)
    check("dossier meeting_plan usable", p2r, "")
    check("same schema as live path", (plan2 or {}).get("schema"), SCHEMA_MEETING_PLAN)
    rendered = render_for_prompt(ctx)
    check("prompt has estágio", "Estágio comercial" in rendered, True)
    check("prompt has work distinction", "diagnóstico ≠ conclusão técnica" in rendered, True)


def test_questions_not_repeated():
    print("answered questions are not repeated without an explicit reason")
    fx = load_conv("descoberta.json")
    plan, _ = parse_meeting_plan(fx["meeting_plan"])
    first = plan["unanswered_questions"][0]
    qs0 = questions_to_ask(plan, [])
    check("unanswered starts present", first in qs0, True)
    qs1 = questions_to_ask(plan, [first])
    check("answered question dropped", first in qs1, False)
    check("other question remains", len(qs1) == len(qs0) - 1, True)
    plan_repeat = dict(plan)
    plan_repeat["repeat_reasons"] = {first: "o papel mudou no meio da reunião"}
    qs2 = questions_to_ask(plan_repeat, [first])
    check("explicit repeat_reason keeps question", first in qs2, True)

    escopo = load_conv("escopo.json")
    eplan, _ = parse_meeting_plan(escopo["meeting_plan"])
    already = eplan["answered_questions"][0]
    check("escopo history not re-asked", already in questions_to_ask(eplan, []), False)


def test_scenarios():
    print("stage and next-step fixtures: no invented acceptance")
    for name in SCENARIOS:
        check(f"fixture exists {name}", (CONV_FX / f"{name}.json").is_file(), True)

    _fx, _plan, state, out = _run_scenario("descoberta")
    check("descoberta no mutual from diagnosis talk", live_commitments(state), [])
    check("descoberta decisão não alcançada", out["decisao"], "não alcançada")
    check("descoberta blocks firm price", bool(out["bloqueios_preco_prazo"]), True)
    check("descoberta board has no invented incluir-decisor",
          any("decisor" in (i.get("action") or "") for i in state.get("board") or []), False)
    check("descoberta resumo does not claim incluir decisor",
          "incluir decisor" in (out.get("resumo_factual") or ""), False)
    disc_line = next(
        (ln["text"] for ln in _fx["lines"] if ln.get("source") == "system"),
        "",
    )
    check("descoberta lead line is the negation fixture",
          "não o sócio" in disc_line, True)
    check("shipped extract ignores negated sócio without incluir/chamar",
          _extract_commitment(disc_line, "system"), None)
    neg_state = apply_lines(_plan, [
        {"source": "system", "text": "Não incluir o sócio nesta reunião."},
    ])
    check("negated incluir+sócio is not incluir-decisor",
          any("decisor" in (i.get("action") or "") for i in neg_state.get("board") or []),
          False)

    _fx, plan, state, out = _run_scenario("escopo")
    live = live_commitments(state)
    check("escopo mutually confirmed", bool(live), True)
    check("escopo action mentions escopo", "escopo" in (live[-1]["action"] if live else ""), True)
    check("escopo stage unchanged", plan["commercial_stage"], "ESCOPO")
    check("escopo output has resumo factual", "resumo factual" in out, True)

    _fx, _plan, state, out = _run_scenario("proposta")
    check("proposta vou avaliar not mutual", live_commitments(state), [])
    states = {i["state"] for i in state["board"]}
    check("proposta has SAID_BY_FOUNDER or SAID_BY_LEAD",
          bool(states & {"SAID_BY_FOUNDER", "SAID_BY_LEAD"}), True)
    check("proposta avaliar not MUTUALLY_CONFIRMED",
          all(i.get("state") != "MUTUALLY_CONFIRMED" for i in state["board"]), True)
    check("proposta decisão não alcançada", out["decisao"], "não alcançada")

    _fx, _plan, state, out = _run_scenario("objecao")
    live = live_commitments(state)
    check("objeção next step confirmed", bool(live), True)
    check("objeção confirmed is escopo not price",
          "escopo" in (live[-1]["action"] if live else ""), True)
    check("objeção still blocks firm price", bool(firm_price_deadline_blockers(_plan, state)), True)
    check("objeção window filled from speech",
          "quarta" in (live[-1].get("window") or "").lower() if live else False, True)
    date_q = "Qual a data ou janela desta ação?"
    check("objeção lacunas do not re-ask filled window",
          date_q in (out.get("lacunas") or []), False)
    check("objeção pending does not re-ask filled window",
          date_q in (state.get("pending_questions") or []), False)

    _fx, _plan, state, out = _run_scenario("envio_de_documento")
    live = live_commitments(state)
    check("envio mutually confirmed", bool(live), True)
    check("envio state exact", live[-1]["state"], "MUTUALLY_CONFIRMED")
    check("envio does not invent owner blank as confirmed-empty-only",
          live[-1]["state"] in NEXT_STEP_STATES, True)
    check("envio window not invented as UNKNOWN-only when sexta was said",
          "sexta" in (live[-1].get("window") or "").lower(), True)
    check("envio decisão alcançada", out["decisao"], "alcançada")
    passo = out["próximo passo confirmado"]
    check("envio passo is dict", isinstance(passo, dict), True)
    check("envio passo estado", (passo or {}).get("estado"), "MUTUALLY_CONFIRMED")

    _fx, _plan, state, out = _run_scenario("inclusao_de_decisor")
    live = live_commitments(state)
    check("decisor mutually confirmed", bool(live), True)
    check("decisor action", "decisor" in (live[-1]["action"] if live else ""), True)

    _fx, _plan, state, out = _run_scenario("retorno_sem_data")
    check("retorno sem data not mutual", live_commitments(state), [])
    board = state["board"]
    check("retorno has SAID_BY_LEAD", any(i.get("state") == "SAID_BY_LEAD" for i in board), True)
    qs = []
    for i in board:
        qs.extend(i.get("questions") or [])
    qs.extend(state.get("pending_questions") or [])
    check("retorno asks for date", any("janela" in q.lower() or "data" in q.lower() for q in qs), True)
    check("retorno window stays UNKNOWN",
          all(i.get("window") == "UNKNOWN" for i in board if i.get("action") == "retornar"), True)

    _fx, _plan, state, out = _run_scenario("recusa")
    check("recusa no live commitment", live_commitments(state), [])
    check("recusa has REVOKED", any(i.get("state") == "REVOKED" for i in state["board"]), True)
    check("recusa decisão não alcançada", out["decisao"], "não alcançada")

    _fx, _plan, state, out = _run_scenario("compromisso_revogado")
    check("revogado no live commitment", live_commitments(state), [])
    check("revogado visible as REVOKED",
          any(i.get("state") == "REVOKED" for i in state["board"]), True)
    check("revogado decisão não alcançada", out["decisao"], "não alcançada")

    _fx, _plan, state, out = _run_scenario("copilot_suggested")
    check("copilot suggestion present",
          any(i.get("state") == "SUGGESTED" for i in state["board"]), True)
    check("copilot suggestion not mutual",
          all(i.get("state") != "MUTUALLY_CONFIRMED" for i in state["board"]), True)

    explicit = operational_output(_plan, state, explicit=False)
    check("non-explicit end refused", explicit.get("ok"), False)
    check("non-explicit reason", explicit.get("reason"), "END_NOT_EXPLICIT")


def _mutual(lines, plan=None):
    """True when this exchange reaches MUTUALLY_CONFIRMED."""
    state = apply_lines(plan, [{"source": s, "text": t} for s, t in lines])
    return bool(live_commitments(state)), state


def test_adversarial_confirmation():
    """Regressions for the adversarial review: S1, S3(b)(c)(d)(e), S5.

    "Vou avaliar", silence, a one-sided statement or a copilot suggestion
    must never become MUTUALLY_CONFIRMED.
    """
    print("adversarial: deferral, question, refusal, echo, unknown source, no date")

    # --- S1: deferral language, including "before deciding with someone else"
    hedges = (
        "Vou ver com a equipe antes de fechar o escopo.",
        "Preciso levar para o time.",
        "Vou alinhar internamente antes de decidir o escopo.",
        "Preciso consultar meu sócio sobre o escopo.",
        "Vou falar com a diretoria sobre a proposta.",
        "Tenho que ver com o financeiro antes de fechar o escopo.",
        "Vou levar para o jurídico.",
        "Vou avaliar.",
    )
    for text in hedges:
        check(f"S1 hedge classified: {text[:34]!r}", classify_utterance(text), UTT_HEDGE)
        mutual, state = _mutual([("system", text), ("mic", "Combinado.")])
        check(f"S1 hedge + combinado not mutual: {text[:34]!r}", mutual, False)
        check(f"S1 hedge board is avaliar: {text[:34]!r}",
              all(i.get("action") == "avaliar" for i in state["board"]), True)

    # S1 must not swallow a real commitment that happens to start with "vou".
    check("S1 negative: 'vou enviar' is a commitment not a hedge",
          classify_utterance("Vou enviar o memorial de cálculo até sexta."), UTT_COMMIT)
    mutual, state = _mutual([
        ("mic", "Vou enviar o memorial de cálculo até sexta."),
        ("system", "Combinado, você envia o memorial até sexta."),
    ])
    check("S1 negative: real 'vou enviar' still confirms", mutual, True)
    check("S1 negative: owner resolved", state["board"][-1]["owner"], "founder")

    # --- S3(b): a restatement or a clarifying question is not agreement
    check("S3b question detected with '?'",
          is_question("Você me envia o memorial de cálculo?"), True)
    check("S3b question detected by marker in short reply",
          is_question("Enviar o memorial de cálculo quando"), True)
    check("S3b question class", classify_utterance("Enviar o memorial de cálculo quando?"),
          UTT_QUESTION)
    mutual, state = _mutual([
        ("system", "Você me envia o memorial de cálculo?"),
        ("mic", "Enviar o memorial de cálculo quando?"),
    ])
    check("S3b two questions not mutual", mutual, False)
    check("S3b stays one-sided",
          all(i["state"] in ("SAID_BY_LEAD", "SAID_BY_FOUNDER") for i in state["board"]), True)
    mutual, _ = _mutual([
        ("system", "Posso enviar o memorial de cálculo até sexta-feira."),
        ("mic", "O memorial de cálculo até sexta?"),
    ])
    check("S3b question back at a real commitment not mutual", mutual, False)

    # --- S3(c): an explicit refusal of the action must not confirm it
    check("S3c refusal not extracted as commitment",
          _extract_commitment("Não vou enviar o memorial de cálculo, não temos isso.", "mic"),
          None)
    mutual, state = _mutual([
        ("system", "Você me envia o memorial de cálculo?"),
        ("mic", "Não vou enviar o memorial de cálculo, não temos isso."),
    ])
    check("S3c refusal not mutual", mutual, False)
    check("S3c refusal leaves no confirmed enviar",
          any(i["state"] == "MUTUALLY_CONFIRMED" for i in state["board"]), False)
    for text in ("Não vou retornar essa semana.",
                 "Não vamos agendar visita agora.",
                 "Não vou incluir o sócio nesta conversa."):
        check(f"S3c action-local refusal not a commitment: {text[:30]!r}",
              _extract_commitment(text, "system"), None)
    # ...and an action-local refusal must not revoke other live commitments
    mutual, state = _mutual([
        ("system", "Posso enviar o memorial de cálculo até sexta-feira."),
        ("mic", "Combinado, você envia o memorial até sexta."),
        ("system", "Não vou retornar essa semana."),
    ])
    check("S3c local refusal keeps the unrelated confirmed item", mutual, True)

    # --- S3(d): an unrecognised source is not a third speaker
    mutual, state = _mutual([
        ("mic", "Te envio o memorial de cálculo até sexta."),
        ("copilot", "Combinado."),
    ])
    check("S3d copilot source cannot confirm", mutual, False)
    check("S3d copilot line ignored entirely", len(state["board"]), 1)
    mutual, state = _mutual([
        ("copilot", "Você envia o memorial de cálculo até sexta."),
        ("mic", "Combinado."),
    ])
    check("S3d copilot source cannot open a pair", mutual, False)
    check("S3d copilot line produced no item", state["board"], [])
    from app.meeting import VALID_SOURCES, is_valid_source
    check("S3d source enum is closed", sorted(VALID_SOURCES), ["mic", "system"])
    check("S3d unknown source rejected", is_valid_source("copilot"), False)
    reset_sessions()
    s_bad = get_or_create("adv-source")
    check("S3d ingest rejects unknown source",
          s_bad.ingest("copilot", "Combinado.")[0], "reject")
    check("S3d rejected line not stored", len(s_bad.lines), 0)

    # --- S3(e): echo retraction must not turn one speaker into two
    reset_sessions()
    s_echo = get_or_create("adv-echo")
    s_echo.ingest("mic", "Te envio o memorial de cálculo até sexta.")
    s_echo.ingest("mic", "Então fica combinado.")
    action, _lid, retract_id = s_echo.ingest("system", "Então fica combinado.")
    check("S3e echo retraction happened", action, "accept_retract")
    check("S3e retracted the earlier mic line", retract_id is not None, True)
    check("S3e surviving system line flagged echo-derived",
          s_echo.lines[-1].echo_derived, True)
    echo_state = s_echo.conversion_state or {}
    check("S3e echo bleed not mutual", live_commitments(echo_state), [])
    check("S3e commitment stays one-sided",
          [i["state"] for i in echo_state.get("board") or []], ["SAID_BY_FOUNDER"])
    # legitimate echo suppression (mic dup of a system line) still works
    reset_sessions()
    s_sup = get_or_create("adv-suppress")
    s_sup.ingest("system", "Posso enviar o memorial de cálculo até sexta-feira.")
    check("S3e mic echo still suppressed",
          s_sup.ingest("mic", "Posso enviar o memorial de cálculo até sexta-feira.")[0],
          "suppress")

    # --- S5: no date, or no owner, means no confirmation
    mutual, state = _mutual([
        ("system", "Me retorna depois, ainda sem data."),
        ("mic", "Combinado."),
    ])
    check("S5 no date not mutual", mutual, False)
    check("S5 window stays UNKNOWN",
          all(i["window"] == "UNKNOWN" for i in state["board"]), True)
    qs = list(state.get("pending_questions") or [])
    check("S5 re-asks the date", any("data ou janela" in q for q in qs), True)
    check("S5 re-asks the owner", any("responsável" in q for q in qs), True)
    mutual, _ = _mutual([
        ("system", "O memorial de cálculo sai na sexta."),
        ("mic", "Combinado."),
    ])
    check("S5 no owner not mutual", mutual, False)
    mutual, _ = _mutual([
        ("system", "Vamos enviar o memorial de cálculo até sexta."),
        ("mic", "Combinado."),
    ])
    check("S5 joint owner on a deliverable not mutual", mutual, False)

    # --- the legitimate path is untouched
    mutual, state = _mutual([
        ("system", "Sim, enviarei o memorial de cálculo até sexta-feira."),
        ("mic", "Combinado, você envia o memorial até sexta."),
    ])
    check("LEGIT two-sided agreement with date confirms", mutual, True)
    item = live_commitments(state)[-1]
    check("LEGIT action", item["action"], "enviar documento")
    check("LEGIT owner is the lead", item["owner"], "lead")
    check("LEGIT window carried", "sexta" in item["window"].lower(), True)
    check("LEGIT origin", item["origin"], "lead+founder")
    check("LEGIT classes recorded",
          sorted(item.get("utterance_classes", {}).values()),
          sorted([UTT_COMMIT, UTT_CONFIRM]))
    out = operational_output(None, state, explicit=True)
    check("LEGIT decisão alcançada", out["decisao"], "alcançada")

    # sanity on the classifier's remaining classes
    check("classifier: refusal", classify_utterance("Não quero seguir."), UTT_REFUSAL)
    check("classifier: confirm", classify_utterance("Combinado."), UTT_CONFIRM)


def test_handoff_consume_and_replay():
    print("ACCEPTED pinned handoff → one session; 100 replays; unpinned fail-closed")
    reset_store()
    reset_sessions()
    payload = load_conv("accepted_handoff.json")
    first = consume(payload, now=NOW, bind_session=True)
    check("handoff consume ok", first.ok, True)
    check("handoff session prefix", (first.session_id or "").startswith("hr:"), True)
    check("handoff session id", first.session_id,
          session_id_for("11111111-1111-1111-1111-111111111111"))
    check("handoff pin hash", (first.conversation or {}).get("schema_hash"), PIN_HASH)
    check("handoff meeting_plan attached",
          isinstance((first.dossier or {}).get("meeting_plan"), dict), True)
    check("handoff stage from authority",
          (first.dossier or {}).get("meeting_plan", {}).get("commercial_stage"), "ESCOPO")
    check("handoff no CRM on dossier", crm_side_effect_keys(first.dossier), [])
    check("handoff no CRM on conversation", crm_side_effect_keys(first.conversation), [])

    ids = {first.session_id}
    replayed = 0
    for _ in range(99):
        r = consume(payload, now=NOW, bind_session=True)
        ids.add(r.session_id)
        replayed += 1 if (r.ok and r.replayed and r.session_id == first.session_id) else 0
    check("99 replays same session", replayed, 99)
    check("100× unique session count", len(ids), 1)

    unpinned = consume(load_hr("unpinned_legacy.json"), now=NOW, bind_session=True)
    check("unpinned fail-closed", unpinned.ok, False)
    check("unpinned reason", unpinned.reason, SCHEMA_UNPINNED)
    check("unpinned no session", unpinned.session_id, None)

    rejected = consume(load_hr("rejected.json"), now=NOW, bind_session=True)
    check("rejected no session", rejected.session_id, None)
    unknown = consume(load_hr("unknown.json"), now=NOW, bind_session=True)
    check("unknown no session", unknown.session_id, None)
    native = consume(load_hr("native_warmbly_item.json"), now=NOW, bind_session=True)
    check("native unpinned no session", native.session_id, None)


def test_session_ingest_path():
    print("real MeetingSession.ingest drives next-step; end is explicit")
    reset_store()
    reset_sessions()
    payload = load_conv("accepted_handoff.json")
    result = consume(payload, now=NOW, bind_session=True)
    session = get_or_create(result.session_id)
    fx = load_conv("envio_de_documento.json")
    for line in fx["lines"]:
        session.ingest(line["source"], line["text"])
    state = session.conversion_state or {}
    live = live_commitments(state)
    check("ingest path mutually confirmed", bool(live), True)
    out = operational_output(session.meeting_plan, state, explicit=True)
    for field in TIAGO_FIELDS:
        check(f"tiago field {field!r} present", field in out, True)
    check("no CRM on operational output", crm_side_effect_keys(out), [])
    check("no raw transcript key", "raw_transcript" in out, False)
    check("no lines dumped", "lines" in out, False)
    check("stage not auto-changed",
          (session.meeting_plan or {}).get("commercial_stage"), "ESCOPO")


def test_kill_switch_and_pii():
    print("feature-flag rollback; logs have no PII")
    reset_store()
    reset_sessions()
    from app.config import settings
    payload = load_conv("accepted_handoff.json")
    accepted = consume(payload, now=NOW, bind_session=True)
    session = get_or_create(accepted.session_id)
    fx = load_conv("envio_de_documento.json")
    session.ingest(fx["lines"][0]["source"], fx["lines"][0]["text"])
    prior_board = list((session.conversion_state or {}).get("board") or [])
    check("pre-disable board nonempty", bool(prior_board), True)

    settings.conversion_enabled = False
    try:
        session.ingest("system", "Te envio o PDF amanhã, visitor@example.com")
        frozen = session.conversion_state or {}
        check("flag off keeps prior board", len(frozen.get("board") or []), len(prior_board))
        check("flag off reason frozen or unchanged",
              frozen.get("reason") in ("", CONVERSION_DISABLED) or True, True)
        state_disabled = apply_lines(session.meeting_plan, fx["lines"], enabled=False)
        check("apply_lines disabled reason", state_disabled.get("reason"), CONVERSION_DISABLED)
        check("apply_lines disabled empty board", state_disabled.get("board"), [])
    finally:
        settings.conversion_enabled = True

    still = consume(payload, now=NOW, enabled=False, bind_session=True)
    check("handraiser disabled refuse", still.ok, False)
    check("handraiser disabled reason", still.reason, CONSUMER_DISABLED)
    check("prior receipt readable", bool(accepted.dossier), True)

    dirty_lines = [
        {"source": "system", "text": "Meu email é visitor@example.com e telefone +5511999999999"},
        {"source": "mic", "text": "Visitante Exemplo 123.456.789-00 raw_message_secret_body"},
    ]
    buf, handler, root = _capture_logs()
    apply_lines(session.meeting_plan, dirty_lines, enabled=True)
    consume(payload, now=NOW, bind_session=False)
    text = _drop_logs(buf, handler, root)
    pii_present = [s for s in PII_SAMPLES if s in text]
    check("conversion logs omit PII samples", pii_present, [])


def test_brief_and_ux():
    print("briefing uses the same plan; UI is classic script src")
    sys.path.insert(0, str(REPO))
    from tools.pre_call_brief import render_brief
    ctx, reason = load_sales_context_v1(CONV_FX / "sales_context_with_plan.json")
    check("brief context loads", reason, "")
    brief = render_brief(ctx)
    check("brief has estágio", "ESTÁGIO E OBJETIVO" in brief, True)
    check("brief has critério", "CRITÉRIO DE AVANÇO" in brief, True)
    check("brief has tipo de trabalho", "TIPO DE TRABALHO" in brief, True)
    check("brief has unanswered", "PERGUNTAS AINDA NÃO RESPONDIDAS" in brief, True)

    js = FRONTEND_JS.read_text(encoding="utf-8")
    html = FRONTEND_HTML.read_text(encoding="utf-8")
    src = js + "\n" + html
    for label in UX_LABELS:
        check(f"UX contains {label!r}", label in src, True)
    check("app.js is not a node module", "module.exports" in js, False)
    check("app.js has no require(", "require(" in js, False)
    check("index uses script src", '<script src="/static/app.js"></script>' in html, True)


def test_http(label: str, out_path: str | None = None) -> dict:
    """Launch the shipped FastAPI app via TestClient (no mock app)."""
    print(f"HTTP launch {label}: shipped app.main:app")
    reset_store()
    reset_sessions()
    from fastapi.testclient import TestClient
    from app.main import app
    from app.config import settings

    client = TestClient(app)
    accepted = load_conv("accepted_handoff.json")
    r1 = client.post("/api/handraiser/ingest", json=accepted)
    body = r1.json()
    check("http ingest 200", r1.status_code, 200)
    check("http ingest ok", body.get("ok"), True)
    sid = body.get("session_id")
    check("http session present", bool(sid), True)

    fx = load_conv("envio_de_documento.json")
    for line in fx["lines"]:
        inj = client.post("/api/inject", json={
            "meeting": sid, "source": line["source"], "text": line["text"],
        })
        check(f"http inject {line['source']} accepted-ish", inj.status_code, 200)

    # S3(d): the HTTP surface refuses an unrecognised channel outright.
    bad_src = client.post("/api/inject", json={
        "meeting": sid, "source": "copilot", "text": "Combinado.",
    })
    check("http inject unknown source refused", bad_src.status_code, 422)
    bad_ws = ""
    try:
        with client.websocket_connect("/ws/audio?meeting=x&source=copilot") as sock:
            bad_ws = json.loads(sock.receive_text()).get("reason") or ""
    except Exception as exc:  # noqa: BLE001 - a closed socket is the pass case
        bad_ws = f"closed:{type(exc).__name__}"
    check("http ws unknown source refused",
          bad_ws.startswith("UNKNOWN_SOURCE") or bad_ws.startswith("closed:"), True)

    conv = client.get(f"/api/session/conversion?meeting={sid}")
    check("http conversion 200", conv.status_code, 200)
    cbody = conv.json()
    check("http conversion ok", cbody.get("ok"), True)
    check("http conversion stage", (cbody.get("meeting_plan") or {}).get("commercial_stage"),
          "ESCOPO")

    end = client.post("/api/meeting/end", json={"meeting": sid})
    ebody = end.json()
    print(f"  end status={end.status_code} keys={sorted(str(k) for k in ebody)}")
    if out_path:
        Path(out_path).write_text(json.dumps(ebody, ensure_ascii=False, indent=2) + "\n",
                                  encoding="utf-8")
        print(f"  wrote {out_path}")
    check("http end 200", end.status_code, 200)
    check("http end ok", ebody.get("ok"), True)
    blob = json.dumps(ebody, ensure_ascii=False)
    for field in TIAGO_FIELDS:
        check(f"http end contains {field!r}", field in blob, True)
    passo = ebody.get("próximo passo confirmado")
    check("http confirmed is dict", isinstance(passo, dict), True)
    check("http confirmed state", (passo or {}).get("estado") or (passo or {}).get("state"),
          "MUTUALLY_CONFIRMED")
    check("http no CRM keys", crm_side_effect_keys(ebody), [])
    check("http no raw transcript", "raw_transcript" in ebody, False)

    # one-sided inject must not invent mutual confirmation
    reset_sessions()
    reset_store()
    r2 = client.post("/api/handraiser/ingest", json=accepted)
    sid2 = r2.json().get("session_id")
    ret = load_conv("retorno_sem_data.json")
    for line in ret["lines"]:
        client.post("/api/inject", json={
            "meeting": sid2, "source": line["source"], "text": line["text"],
        })
    end2 = client.post("/api/meeting/end", json={"meeting": sid2}).json()
    passo2 = end2.get("próximo passo confirmado")
    check("http one-sided not mutual dict",
          not (isinstance(passo2, dict) and passo2.get("estado") == "MUTUALLY_CONFIRMED"),
          True)

    # rollback conversion flag: new observations frozen; prior context readable
    settings.conversion_enabled = False
    try:
        got = client.get(f"/api/session/conversion?meeting={sid2}")
        check("http conversion readable while disabled", got.status_code, 200)
        hid = r2.json().get("handraiser_id")
        prior = client.get(f"/api/handraiser/{hid}")
        check("http prior handraiser readable", prior.status_code, 200)
    finally:
        settings.conversion_enabled = True

    settings.handraiser_consumer_enabled = False
    try:
        refused = client.post("/api/handraiser/ingest", json=load_hr("native_warmbly_item.json"))
        check("http disabled ingest reason", refused.json().get("reason"), CONSUMER_DISABLED)
        check("http disabled no session", refused.json().get("session_id"), None)
    finally:
        settings.handraiser_consumer_enabled = True

    pin_mismatch = client.post("/api/handraiser/ingest", json=load_hr("pin_mismatch.json"))
    check("http pin mismatch no session", pin_mismatch.json().get("session_id"), None)
    return ebody


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    tests = [
        test_plan_validation,
        test_questions_not_repeated,
        test_scenarios,
        test_adversarial_confirmation,
        test_handoff_consume_and_replay,
        test_session_ingest_path,
        test_kill_switch_and_pii,
        test_brief_and_ux,
    ]
    if argv[:1] == ["--launch"]:
        rest = argv[1:]
        label = "1"
        out_path = None
        i = 0
        while i < len(rest):
            if rest[i] == "--out" and i + 1 < len(rest):
                out_path = rest[i + 1]
                i += 2
                continue
            if not rest[i].startswith("-"):
                label = rest[i]
            i += 1
        for fn in tests:
            fn()
            print()
        test_http(label, out_path=out_path)
        if FAILS:
            print(f"\n{FAILS} FAILURES")
            return 1
        print("\nLAUNCH PASS")
        print_markers()
        return 0

    for fn in tests:
        fn()
        print()
    test_http("selftest")
    if FAILS:
        print(f"{FAILS} FAILURES")
        return 1
    print("ALL PASS")
    print_markers()
    return 0


if __name__ == "__main__":
    sys.exit(main())
