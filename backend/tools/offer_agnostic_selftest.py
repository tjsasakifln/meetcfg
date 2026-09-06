#!/usr/bin/env python3
"""Behavioral checks for an offer-agnostic meeting context and plan."""
from __future__ import annotations

import json
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.copilot.context import load_sales_context_v1, render_for_prompt  # noqa: E402
from app.copilot.engine import apply_session_channel  # noqa: E402
from app.copilot.handraiser import ReceiptStore, consume  # noqa: E402
from app.copilot.conversion import (  # noqa: E402
    apply_lines,
    advancement_readiness,
    live_commitments,
    meeting_plan_of,
    operational_output,
    parse_meeting_plan,
    questions_to_ask,
    render_plan_for_prompt,
)

REPO = Path(__file__).resolve().parents[2]
FIXTURES = REPO / "fixtures" / "offers"

NUCLEUS_IDS = (
    "expert_evidence_assistance",
    "property_valuation",
    "building_engineering_documentation",
    "occupational_safety",
    "public_works_b2g",
)


def load_doc(path: Path) -> dict:
    ctx, reason = load_sales_context_v1(path)
    assert reason == "", reason
    assert ctx is not None
    return ctx


def load_inline(doc: dict) -> dict:
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "context.json"
        path.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
        return load_doc(path)


def main() -> int:
    ctx = load_doc(FIXTURES / "synthetic_unknown.json")
    plan, reason = meeting_plan_of(ctx)
    assert reason == "", reason
    assert plan is not None
    assert plan["commercial_stage"] == "NEXT_STEP"
    assert plan["work_kind"] == "OPAQUE_FUTURE_WORK"
    assert plan["offer_id"] == "synthetic_flux_capacitor_audit"
    assert plan["offer_family"] == "unregistered_future_family"
    assert plan["acquisition_channel"] == "PARTNER"
    assert plan["conversation_channel"] == "phone"
    assert plan["next_state"] == "amostra anonimizada disponível para análise"

    canonical, canonical_reason = parse_meeting_plan(ctx["meeting_plan"])
    assert canonical_reason == ""
    rebound, rebound_reason = meeting_plan_of({**ctx, "meeting_plan": canonical})
    assert rebound_reason == ""
    assert rebound is not None
    assert rebound["offer_id"] == "synthetic_flux_capacitor_audit"
    assert rebound["acquisition_channel"] == "PARTNER"

    questions = questions_to_ask(plan)
    assert questions == ["A amostra pode ser compartilhada de forma anonimizada?"]
    ready, blockers = advancement_readiness(plan)
    assert ready is False
    assert blockers == ["lacuna aberta: sample_authorization"]

    state = apply_lines(plan, ctx["lines"])
    confirmed = live_commitments(state)
    assert len(confirmed) == 1
    assert confirmed[0]["action"] == "disponibilizar amostra anonimizada"
    assert questions_to_ask(plan, state["answered_questions"]) == []
    assert advancement_readiness(plan, state["answered_questions"])[0] is True
    live_plan_prompt = render_plan_for_prompt(
        plan, answered=state["answered_questions"]
    )
    assert "Pronto para avançar: sim" in live_plan_prompt
    assert "lacuna aberta: sample_authorization" not in live_plan_prompt
    out = operational_output(plan, state, explicit=True)
    assert out["advancement_ready"] is True
    assert out["advancement_blockers"] == []

    dangerous_plan = {**plan, "next_state": "contratação assinada"}
    unrelated = apply_lines(dangerous_plan, [
        {"source": "system", "text": "Vou ligar para meu sócio amanhã."},
        {"source": "mic", "text": "Combinado, você liga amanhã."},
    ])
    assert not any(
        item.get("action") == "contratação assinada"
        for item in unrelated.get("board") or []
    )
    assert live_commitments(unrelated) == []

    reviewer_repro = apply_lines(dangerous_plan, [
        {"source": "system", "text": "Eu reviso a contratação aprovada amanhã."},
        {"source": "mic", "text": "Combinado, você revisa amanhã."},
    ])
    assert not any(
        item.get("action") == "contratação assinada"
        or "assinada" in str(item.get("action") or "").lower()
        for item in reviewer_repro.get("board") or []
    )

    disagreement = apply_lines({**plan, "next_state": "escopo confirmado"}, [
        {
            "source": "pstn_remote",
            "role": "counterparty",
            "text": "Eu discordo do escopo amanhã.",
        },
        {
            "source": "mic",
            "role": "operator",
            "text": "Combinado, você discorda amanhã.",
        },
    ])
    assert not any(
        item.get("action") == "fechar escopo"
        for item in disagreement.get("board") or []
    )
    assert live_commitments(disagreement) == []

    unknown_answer = apply_lines(plan, [{
        "source": "system",
        "text": "Não sei se a amostra pode ser compartilhada de forma anonimizada.",
    }])
    assert questions_to_ask(plan, unknown_answer["answered_questions"]) == [
        "A amostra pode ser compartilhada de forma anonimizada?"
    ]
    assert advancement_readiness(plan, unknown_answer["answered_questions"])[0] is False

    prompt = render_for_prompt(ctx)
    assert "synthetic_flux_capacitor_audit" in prompt
    assert "unregistered_future_family" in prompt
    assert "phone" in prompt
    assert "registro da indicação" in prompt
    assert "Preço" not in prompt
    assert "Próximo passo alvo: receber uma amostra anonimizada para análise" not in prompt

    answered_plan, answered_reason = parse_meeting_plan({
        "schema": "MEETCFG_MEETING_PLAN/1.0",
        "objective": "confirmar papel",
        "advancement_criterion": "papel confirmado",
        "next_state": "papel confirmado",
        "context_status": "CURRENT",
        "unanswered_questions": ["Quem decide?"],
        "gaps": [{
            "id": "decision_role",
            "question": "Quem decide?",
            "status": "ANSWERED",
            "answer": "Ana",
            "blocking": True
        }]
    })
    assert answered_reason == ""
    assert questions_to_ask(answered_plan) == []
    assert advancement_readiness(answered_plan)[0] is True

    incomplete_answer, incomplete_reason = parse_meeting_plan({
        "schema": "MEETCFG_MEETING_PLAN/1.0",
        "objective": "confirmar papel",
        "advancement_criterion": "papel confirmado",
        "next_state": "papel confirmado",
        "context_status": "CURRENT",
        "gaps": [{
            "id": "decision_role",
            "question": "Quem decide?",
            "status": "ANSWERED",
            "blocking": True
        }]
    })
    assert incomplete_reason == ""
    assert questions_to_ask(incomplete_answer) == ["Quem decide?"]
    assert advancement_readiness(incomplete_answer)[0] is False

    compat_ctx = {**ctx, "conversation_channel": "google_meet"}
    compat = apply_session_channel(
        compat_ctx,
        SimpleNamespace(lines=[SimpleNamespace(conversation_channel="phone")]),
    )
    compat_plan, compat_reason = meeting_plan_of(compat)
    assert compat_reason == ""
    assert compat["conversation_channel"] == "phone"
    assert compat["acquisition_channel"] == "PARTNER"
    assert compat_plan is not None
    assert compat_plan["conversation_channel"] == "phone"

    corpus = json.loads((FIXTURES / "multivertical.json").read_text(encoding="utf-8"))
    assert len(corpus) == 5
    plans = []
    for raw in corpus:
        loaded = load_inline(raw)
        candidate, plan_reason = meeting_plan_of(loaded)
        assert plan_reason == "", plan_reason
        assert candidate is not None
        plans.append(candidate)
        rendered = render_for_prompt(loaded)
        assert candidate["objective"] in rendered
        assert candidate["advancement_criterion"] in rendered
        assert candidate["next_state"] in rendered
        assert candidate["offer_id"] in rendered
        if raw["case"] == "engenharia_documentacao":
            assert candidate["conflict_limits"] == [
                "duas versões documentais incompatíveis"
            ]
    assert {p["offer_id"] for p in plans} == set(NUCLEUS_IDS)
    assert {p["acquisition_channel"] for p in plans} == {
        "INBOUND_LIVE", "OUTBOUND_FIRST_TOUCH", "PARTNER"
    }
    assert {p["commercial_stage"] for p in plans} >= {
        "DISCOVERY", "SCOPE", "OBJECTION", "NEXT_STEP"
    }
    assert {p["conversation_channel"] for p in plans} == {"google_meet", "phone"}

    # The five known nuclei and the sixth synthetic id are corpus only: none
    # may control a branch in context/plan/orientation production code.
    production = "\n".join(
        (REPO / rel).read_text(encoding="utf-8")
        for rel in (
            "backend/app/copilot/context.py",
            "backend/app/copilot/conversion.py",
            "backend/app/copilot/engine.py",
            "backend/app/copilot/handraiser.py",
        )
    )
    for offer_id in (*NUCLEUS_IDS, "synthetic_flux_capacitor_audit"):
        assert offer_id not in production, offer_id

    accepted_unknown = json.loads(
        (REPO / "fixtures" / "handraiser" / "unknown_nucleus.json").read_text(
            encoding="utf-8"
        )
    )
    accepted_unknown["offer_candidate"] = "synthetic_flux_capacitor_audit"
    accepted_unknown.pop("private_asset", None)
    consumed = consume(
        accepted_unknown,
        store=ReceiptStore(),
        now=datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc),
        bind_session=False,
    )
    assert consumed.ok is True, consumed.reason
    assert consumed.dossier["offer_candidate"] == "synthetic_flux_capacitor_audit"
    assert consumed.conversation["offer_candidate"] == "synthetic_flux_capacitor_audit"
    assert consumed.conversation["nucleo"] == "not_a_confenge_nucleus"
    assert consumed.dossier["private_asset"] == "UNKNOWN"

    unsafe_handoff = json.loads(json.dumps(accepted_unknown))
    unsafe_handoff["context_status"] = "STALE"
    unsafe_handoff["item"]["context_status"] = "CONTRADICTORY"
    unsafe_consumed = consume(
        unsafe_handoff,
        store=ReceiptStore(),
        now=datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc),
        bind_session=False,
    )
    assert unsafe_consumed.ok is True, unsafe_consumed.reason
    assert unsafe_consumed.dossier["context_status"] == "CONTRADICTORY"
    assert "Contexto não confiável para afirmações" in render_for_prompt(
        unsafe_consumed.dossier
    )

    partial = load_doc(FIXTURES / "partial_unknown.json")
    partial_prompt = render_for_prompt(partial)
    assert "Canal de aquisição: UNKNOWN" in partial_prompt
    assert "Canal da conversa: UNKNOWN" in partial_prompt
    assert "Empresa: UNKNOWN" in partial_prompt
    partial_plan, partial_reason = meeting_plan_of(partial)
    assert partial_reason == ""
    ready, blockers = advancement_readiness(partial_plan)
    assert ready is False
    assert "contexto UNKNOWN" in blockers

    for name, status, forbidden_claim in (
        ("stale.json", "STALE", "um documento existia"),
        ("contradictory.json", "CONTRADICTORY", "o interlocutor é decisor"),
    ):
        unsafe = load_doc(FIXTURES / name)
        unsafe_prompt = render_for_prompt(unsafe)
        assert f"Estado do contexto: {status}" in unsafe_prompt
        assert forbidden_claim not in unsafe_prompt
        unsafe_plan, unsafe_reason = meeting_plan_of(unsafe)
        assert unsafe_reason == ""
        unsafe_ready, unsafe_blockers = advancement_readiness(unsafe_plan)
        assert unsafe_ready is False
        assert f"contexto {status}" in unsafe_blockers

    stale_with_current_plan = load_inline({
        **corpus[0],
        "context_status": "STALE",
        "meeting_plan": {**corpus[0]["meeting_plan"], "context_status": "CURRENT"},
    })
    stale_plan, stale_reason = meeting_plan_of(stale_with_current_plan)
    assert stale_reason == ""
    assert stale_plan is not None
    assert stale_plan["context_status"] == "STALE"
    assert advancement_readiness(stale_plan)[0] is False

    unrecognized = load_inline({
        **corpus[0],
        "context_status": "POSSIBLY_CURRENT",
    })
    unrecognized_prompt = render_for_prompt(unrecognized)
    assert "há fotos das fissuras" not in unrecognized_prompt
    unrecognized_plan, _ = meeting_plan_of(unrecognized)
    assert advancement_readiness(unrecognized_plan)[0] is False

    legacy_unsourced = load_inline({
        "schema": "CONFENGE_SALES_CONTEXT/1.0",
        "context_status": "CURRENT",
        "public_facts": ["produto, preço e prazo supostamente confirmados"],
    })
    assert "supostamente confirmados" not in render_for_prompt(legacy_unsourced)
    legacy_unknown = load_inline({
        "schema": "CONFENGE_SALES_CONTEXT/1.0",
        "provenance": "registro antigo",
        "public_facts": ["preço fechado em 2020"],
    })
    assert "preço fechado em 2020" not in render_for_prompt(legacy_unknown)

    bad, bad_reason = load_sales_context_v1(FIXTURES / "claim_without_source.json")
    assert bad is None
    assert "citable_facts[0].source" in bad_reason

    print("OFFER_UNKNOWN_PASS=PASS")
    print("NO_OFFER_BRANCHES=PASS")
    print("ACQUISITION_CHANNEL_SEPARATED=PASS")
    print("CONVERSATION_CHANNEL_COMPAT=PASS")
    print("UNKNOWN_SAFETY=PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
