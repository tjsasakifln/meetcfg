"""Meeting plan, next-step board, and explicit operational output.

Pure functions over dicts and transcript lines. No Codex, no whisper, no
producer fetch, no CRM write, no calendar, no SMTP, no stage mutation.

Commercial stage is read from the authority and never auto-changed.
"""
from __future__ import annotations

import copy
import logging
import re
from typing import Any

log = logging.getLogger("meetcfg.conversion")

SCHEMA_MEETING_PLAN = "MEETCFG_MEETING_PLAN/1.0"
UNKNOWN = "UNKNOWN"

NEXT_STEP_STATES = (
    "SUGGESTED",
    "SAID_BY_LEAD",
    "SAID_BY_FOUNDER",
    "MUTUALLY_CONFIRMED",
    "REVOKED",
    "UNKNOWN",
)

COMMERCIAL_STAGES = ("DESCOBERTA", "ESCOPO", "PROPOSTA", "OBJECAO")
WORK_KINDS = (
    "DIAGNOSTICO",
    "ANALISE_DOCUMENTAL",
    "INSPECAO_CAMPO",
    "ESCOPO",
    "PROPOSTA",
)

WORK_KIND_LABELS = {
    "DIAGNOSTICO": "diagnóstico — hipótese, não conclusão técnica",
    "ANALISE_DOCUMENTAL": "análise documental — não substitui inspeção/campo",
    "INSPECAO_CAMPO": "inspeção/campo — evidência in loco, não só papel",
    "ESCOPO": "escopo — fronteira do trabalho, não é proposta dimensionada",
    "PROPOSTA": "proposta dimensionada — exige escopo, insumos, capacidade e atribuição",
}

STAGE_CRITERIA = {
    "DESCOBERTA": (
        "sair com o problema, o papel do interlocutor e o próximo passo "
        "de diagnóstico ou análise documental"
    ),
    "ESCOPO": "sair com fronteira de escopo, insumos e critério para proposta",
    "PROPOSTA": "sair com aceite, recusa ou condição explícita — 'vou avaliar' não basta",
    "OBJECAO": (
        "sair com a objeção nomeada e o próximo passo que a desbloqueia, "
        "ou recusa visível"
    ),
}

CONVERSION_DISABLED = "CONVERSION_DISABLED"
PLAN_SCHEMA_INVALID = "MEETING_PLAN_SCHEMA_INVALID"
PLAN_STAGE_INVALID = "MEETING_PLAN_STAGE_INVALID"
END_NOT_EXPLICIT = "END_NOT_EXPLICIT"

CRM_SIDE_EFFECT_KEYS = (
    "lead", "lead_id", "pipeline", "queue", "cadence",
    "offer_truth", "opportunity", "account",
    "calendar_event", "smtp", "message_sent", "proposal_final",
    "stage_write", "raw_transcript",
)

TIAGO_FIELDS = (
    "resumo factual",
    "lacunas",
    "decisão alcançada/não alcançada",
    "próximo passo confirmado",
    "insumos para proposta",
    "itens que impedem preço/prazo firme",
)

_STAGE_ALIASES = {
    "descoberta": "DESCOBERTA",
    "escopo": "ESCOPO",
    "proposta": "PROPOSTA",
    "objecao": "OBJECAO",
    "objeção": "OBJECAO",
    "objeção de preço": "OBJECAO",
    "objecao de preco": "OBJECAO",
}

_KIND_ALIASES = {
    "diagnostico": "DIAGNOSTICO",
    "diagnóstico": "DIAGNOSTICO",
    "analise documental": "ANALISE_DOCUMENTAL",
    "análise documental": "ANALISE_DOCUMENTAL",
    "inspecao/campo": "INSPECAO_CAMPO",
    "inspeção/campo": "INSPECAO_CAMPO",
    "inspecao campo": "INSPECAO_CAMPO",
    "inspeção campo": "INSPECAO_CAMPO",
    "campo": "INSPECAO_CAMPO",
    "escopo": "ESCOPO",
    "proposta": "PROPOSTA",
}

_EVAL_RE = re.compile(
    r"\b(vou avaliar|vou pensar|vamos nos falando|preciso avaliar|"
    r"vou ver internamente|deixa eu pensar|deixe[- ]me pensar|"
    r"vou analisar)\b",
    re.I,
)
_REFUSAL_RE = re.compile(
    r"\b(n[aã]o quero|n[aã]o vamos seguir|recuso|sem interesse|"
    r"n[aã]o tenho interesse|n[aã]o vamos fechar|n[aã]o vamos continuar|"
    r"n[aã]o quero seguir)\b",
    re.I,
)
_REVOKE_RE = re.compile(
    r"\b(cancela( o que combinamos)?|na verdade n[aã]o posso|"
    r"revogo|desisto do combinado|n[aã]o vale mais|cancela isso)\b",
    re.I,
)
_CONFIRM_RE = re.compile(
    r"\b(combinado|combinamos|confirmado|fica assim|ent[aã]o fica|"
    r"pode mandar|pode enviar|fechado assim|ent[aã]o fica combinado)\b",
    re.I,
)
_WINDOW_RE = re.compile(
    r"\b(hoje|amanh[aã]|segunda|ter[cç]a|quarta|quinta|sexta|s[aá]bado|"
    r"domingo|pr[oó]xima semana|at[eé]\s+[^.,;]{2,40}|em\s+\d+\s+dias|"
    r"dia\s+\d{1,2})\b",
    re.I,
)
_FOLD_ACCENTS = str.maketrans(
    "áàãâäéèêëíìîïóòõôöúùûüçÁÀÃÂÄÉÈÊËÍÌÎÏÓÒÕÔÖÚÙÛÜÇ",
    "aaaaaeeeeiiiiooooouuuucAAAAAEEEEIIIIOOOOOUUUUC",
)


def _str(v) -> str:
    return v.strip() if isinstance(v, str) and v.strip() else ""


def _fold(s: str) -> str:
    return _str(s).translate(_FOLD_ACCENTS).lower()


def _copy_state(state: dict | None) -> dict:
    return copy.deepcopy(state) if isinstance(state, dict) else empty_state()


def empty_state() -> dict:
    return {
        "board": [],
        "pending_questions": [],
        "answered_questions": [],
        "commercial_stage": None,
        "reason": "",
        "_next_id": 1,
    }


def crm_side_effect_keys(obj: dict | None) -> list[str]:
    if not isinstance(obj, dict):
        return []
    found = [k for k in CRM_SIDE_EFFECT_KEYS if k in obj]
    for v in obj.values():
        if isinstance(v, dict):
            found.extend(crm_side_effect_keys(v))
    return found


def _norm_stage(raw) -> str:
    s = _str(raw)
    if not s:
        return ""
    if s.upper() in COMMERCIAL_STAGES:
        return s.upper()
    return _STAGE_ALIASES.get(_fold(s), "")


def _norm_kind(raw) -> str:
    s = _str(raw)
    if not s:
        return ""
    if s.upper() in WORK_KINDS:
        return s.upper()
    return _KIND_ALIASES.get(_fold(s), "")


def parse_meeting_plan(raw) -> tuple[dict | None, str]:
    """Validate a meeting_plan object. Absent → (None, '') limited/manual.

    Invalid schema/stage → (None, treatable reason). Never invents stage.
    """
    if raw is None:
        return None, ""
    if not isinstance(raw, dict):
        return None, "meeting_plan deve ser um objeto"
    schema = _str(raw.get("schema"))
    if schema and schema != SCHEMA_MEETING_PLAN:
        return None, (
            f"meeting_plan schema deve ser exatamente {SCHEMA_MEETING_PLAN!r} "
            f"(veio {schema!r})"
        )
    if not schema:
        return None, (
            f"meeting_plan schema deve ser exatamente {SCHEMA_MEETING_PLAN!r} "
            f"(veio {raw.get('schema')!r})"
        )

    stage_raw = raw.get("commercial_stage") or raw.get("estagio") or raw.get("estágio")
    stage = _norm_stage(stage_raw)
    if _str(stage_raw) and not stage:
        return None, (
            f"estágio comercial inválido ({stage_raw!r}); "
            f"use um de: {', '.join(COMMERCIAL_STAGES)}"
        )
    if not stage:
        stage = UNKNOWN

    kind_raw = raw.get("work_kind") or raw.get("tipo_trabalho")
    kind = _norm_kind(kind_raw) or UNKNOWN

    roles = raw.get("participant_roles") or raw.get("papeis") or []
    if roles is None:
        roles = []
    if not isinstance(roles, list):
        return None, "participant_roles deve ser uma lista"
    clean_roles = []
    for r in roles:
        if isinstance(r, str) and r.strip():
            clean_roles.append({"name": UNKNOWN, "role": r.strip()})
        elif isinstance(r, dict):
            clean_roles.append({
                "name": _str(r.get("name")) or UNKNOWN,
                "role": _str(r.get("role") or r.get("papel")) or UNKNOWN,
            })
    if not clean_roles:
        clean_roles = [{"name": UNKNOWN, "role": UNKNOWN}]

    def _str_list(key, *alts):
        for k in (key, *alts):
            v = raw.get(k)
            if isinstance(v, list) and all(isinstance(x, str) for x in v):
                return [x.strip() for x in v if x.strip()]
            if v is None:
                continue
            if not isinstance(v, list):
                return None
        return []

    unanswered = _str_list("unanswered_questions", "perguntas")
    answered = _str_list("answered_questions", "perguntas_respondidas")
    evidence = _str_list("evidence_to_confirm", "evidencias", "evidências")
    scope_limits = _str_list("scope_limits", "limites_escopo")
    conflict_limits = _str_list("conflict_limits", "limites_conflito")
    for label, got in (
        ("unanswered_questions", unanswered),
        ("answered_questions", answered),
        ("evidence_to_confirm", evidence),
        ("scope_limits", scope_limits),
        ("conflict_limits", conflict_limits),
    ):
        if got is None:
            return None, f"{label} deve ser uma lista de strings"

    objective = _str(raw.get("objective") or raw.get("objetivo")) or UNKNOWN
    criterion = _str(
        raw.get("advancement_criterion") or raw.get("criterio_avanco")
        or raw.get("critério_de_avanço")
    )
    criterion_origin = "authority" if criterion else "stage_default"
    if not criterion:
        criterion = STAGE_CRITERIA.get(stage, UNKNOWN)

    limited = stage == UNKNOWN or objective == UNKNOWN
    plan = {
        "schema": SCHEMA_MEETING_PLAN,
        "commercial_stage": stage,
        "objective": objective,
        "participant_roles": clean_roles,
        "unanswered_questions": unanswered,
        "answered_questions": answered,
        "evidence_to_confirm": evidence,
        "scope_limits": scope_limits,
        "conflict_limits": conflict_limits,
        "advancement_criterion": criterion,
        "advancement_criterion_origin": criterion_origin,
        "work_kind": kind,
        "work_kind_label": WORK_KIND_LABELS.get(kind, UNKNOWN),
        "limited": limited,
    }
    return plan, ""


def meeting_plan_of(ctx: dict | None) -> tuple[dict | None, str]:
    if not isinstance(ctx, dict):
        return None, ""
    raw = ctx.get("meeting_plan")
    if raw is None:
        return None, ""
    return parse_meeting_plan(raw)


def questions_to_ask(plan: dict | None, answered: list[str] | None = None) -> list[str]:
    """Unanswered questions minus already-answered, unless repeat_reason is set."""
    if not isinstance(plan, dict):
        return []
    answered_l = [_fold(x) for x in (answered or []) + list(plan.get("answered_questions") or [])]
    repeat_reasons = plan.get("repeat_reasons") if isinstance(plan.get("repeat_reasons"), dict) else {}
    out: list[str] = []
    seen: set[str] = set()
    for q in plan.get("unanswered_questions") or []:
        if not isinstance(q, str) or not q.strip():
            continue
        key = _fold(q)
        if key in seen:
            continue
        already = any(key == a or key in a or a in key for a in answered_l if a)
        if already and not _str(repeat_reasons.get(q)):
            continue
        seen.add(key)
        out.append(q.strip())
    return out


def _role_known(plan: dict | None) -> bool:
    if not isinstance(plan, dict):
        return False
    for r in plan.get("participant_roles") or []:
        if isinstance(r, dict) and _str(r.get("role")) not in ("", UNKNOWN):
            return True
    return False


def firm_price_deadline_blockers(plan: dict | None, state: dict | None) -> list[str]:
    """Without confirmed scope, inputs, capacity and attribution, no firm promise."""
    blockers: list[str] = []
    state = state or empty_state()
    live = [i for i in state.get("board") or [] if i.get("state") == "MUTUALLY_CONFIRMED"]
    stage = (plan or {}).get("commercial_stage") if isinstance(plan, dict) else UNKNOWN
    kind = (plan or {}).get("work_kind") if isinstance(plan, dict) else UNKNOWN

    scope_ok = False
    inputs_ok = False
    capacity_ok = False
    attrib_ok = False
    for item in live:
        action = _fold(item.get("action") or "")
        if "escopo" in action or item.get("scope_confirmed") is True:
            scope_ok = True
        if _str(item.get("input")) not in ("", UNKNOWN):
            inputs_ok = True
        if _str(item.get("owner")) not in ("", UNKNOWN):
            attrib_ok = True
        if item.get("capacity_confirmed") is True:
            capacity_ok = True
    if isinstance(plan, dict) and plan.get("scope_confirmed") is True:
        scope_ok = True
    if isinstance(plan, dict) and _str_list_nonempty(plan.get("evidence_to_confirm")) is False:
        # evidence still to confirm means inputs not done
        pass
    if kind in ("DIAGNOSTICO", "ANALISE_DOCUMENTAL", "INSPECAO_CAMPO") or stage == "DESCOBERTA":
        if not scope_ok:
            blockers.append("escopo não confirmado")
        if not inputs_ok:
            blockers.append("insumos não confirmados")
        if not capacity_ok:
            blockers.append("capacidade não confirmada")
        if not attrib_ok:
            blockers.append("atribuição não confirmada")
        return blockers
    if not scope_ok:
        blockers.append("escopo não confirmado")
    if not inputs_ok:
        blockers.append("insumos não confirmados")
    if not capacity_ok:
        blockers.append("capacidade não confirmada")
    if not attrib_ok:
        blockers.append("atribuição não confirmada")
    return blockers


def _str_list_nonempty(v) -> bool:
    return isinstance(v, list) and any(isinstance(x, str) and x.strip() for x in v)


def may_suggest_firm_commitment(plan: dict | None, state: dict | None) -> bool:
    return not firm_price_deadline_blockers(plan, state)


def render_plan_for_prompt(plan: dict | None, *, answered: list[str] | None = None,
                           reason: str = "") -> str:
    if reason:
        return (
            f"Plano de reunião indisponível ({reason}). "
            "Modo limitado/manual: não invente estágio, preço, prazo nem aceite."
        )
    if not isinstance(plan, dict):
        return (
            "Plano de reunião ausente. Modo limitado/manual: pergunte o objetivo "
            "e o critério de avanço; não invente estágio comercial."
        )
    parts = [
        f"Estágio comercial (autoridade, não alterar): {plan.get('commercial_stage') or UNKNOWN}",
        f"Objetivo único desta reunião: {plan.get('objective') or UNKNOWN}",
        f"Tipo de trabalho: {plan.get('work_kind_label') or plan.get('work_kind') or UNKNOWN}",
        f"Critério de avanço: {plan.get('advancement_criterion') or UNKNOWN}",
    ]
    roles = []
    for r in plan.get("participant_roles") or []:
        if isinstance(r, dict):
            roles.append(f"{r.get('name') or UNKNOWN} ({r.get('role') or UNKNOWN})")
    if roles:
        parts.append("Papel dos participantes: " + "; ".join(roles))
    qs = questions_to_ask(plan, answered)
    if qs:
        parts.append("Perguntas ainda não respondidas (não repetir as já respondidas):")
        parts.extend(f"- {q}" for q in qs)
    ev = [x for x in (plan.get("evidence_to_confirm") or []) if isinstance(x, str) and x.strip()]
    if ev:
        parts.append("Fatos/evidências a confirmar:")
        parts.extend(f"- {x}" for x in ev)
    limits = [x for x in (plan.get("scope_limits") or []) + (plan.get("conflict_limits") or [])
              if isinstance(x, str) and x.strip()]
    if limits:
        parts.append("Limites de escopo e conflito:")
        parts.extend(f"- {x}" for x in limits)
    parts.append(
        "Distinção obrigatória: diagnóstico ≠ conclusão técnica; "
        "análise documental ≠ inspeção/campo; escopo ≠ proposta dimensionada."
    )
    if plan.get("limited"):
        parts.append("Contexto incompleto: modo limitado/manual.")
    parts.append(
        "Sem escopo, insumos, capacidade e atribuição confirmados, "
        "não sugira preço, prazo ou entrega assinada firme."
    )
    return "\n".join(parts)


def missing_field_questions(item: dict) -> list[str]:
    qs: list[str] = []
    if _str(item.get("owner")) in ("", UNKNOWN):
        qs.append("Quem é o responsável por esta ação?")
    if _str(item.get("window")) in ("", UNKNOWN):
        qs.append("Qual a data ou janela desta ação?")
    if _str(item.get("input")) in ("", UNKNOWN):
        qs.append("Qual insumo é necessário para esta ação?")
    return qs


def _new_item(board_state: dict, **fields) -> dict:
    nid = int(board_state.get("_next_id") or 1)
    board_state["_next_id"] = nid + 1
    item = {
        "id": f"ns:{nid}",
        "action": UNKNOWN,
        "owner": UNKNOWN,
        "window": UNKNOWN,
        "input": UNKNOWN,
        "condition": UNKNOWN,
        "origin": UNKNOWN,
        "state": "UNKNOWN",
        "span": "",
        "questions": [],
    }
    item.update(fields)
    for key in ("action", "owner", "window", "input", "condition", "origin", "state"):
        if not _str(item.get(key)):
            item[key] = UNKNOWN
    if item["state"] not in NEXT_STEP_STATES:
        item["state"] = "UNKNOWN"
    if item["state"] != "MUTUALLY_CONFIRMED":
        item["questions"] = missing_field_questions(item)
    else:
        item["questions"] = missing_field_questions(item)
    return item


def _public_item(item: dict | None) -> dict | None:
    if not isinstance(item, dict):
        return None
    return {
        "id": item.get("id"),
        "ação": item.get("action") or UNKNOWN,
        "action": item.get("action") or UNKNOWN,
        "responsável": item.get("owner") or UNKNOWN,
        "owner": item.get("owner") or UNKNOWN,
        "prazo/janela": item.get("window") or UNKNOWN,
        "window": item.get("window") or UNKNOWN,
        "insumo": item.get("input") or UNKNOWN,
        "input": item.get("input") or UNKNOWN,
        "condição": item.get("condition") or UNKNOWN,
        "condition": item.get("condition") or UNKNOWN,
        "origem da afirmação": item.get("origin") or UNKNOWN,
        "origin": item.get("origin") or UNKNOWN,
        "estado": item.get("state") or UNKNOWN,
        "state": item.get("state") or UNKNOWN,
        "questions": list(item.get("questions") or []),
    }


def live_commitments(state: dict | None) -> list[dict]:
    return [
        i for i in (state or {}).get("board") or []
        if isinstance(i, dict) and i.get("state") == "MUTUALLY_CONFIRMED"
    ]


def _revoke_live(state: dict, *, origin: str, span: str) -> None:
    revoked_any = False
    for item in state.get("board") or []:
        if item.get("state") in ("MUTUALLY_CONFIRMED", "SAID_BY_LEAD", "SAID_BY_FOUNDER"):
            item["state"] = "REVOKED"
            item["origin"] = origin
            item["span"] = span[:120]
            item["condition"] = "compromisso revogado"
            revoked_any = True
            log.info("conversion observe reason=REVOKED item_id=%s", item.get("id"))
    if not revoked_any:
        item = _new_item(
            state,
            action="recusa",
            owner=origin,
            state="REVOKED",
            origin=origin,
            span=span[:120],
            condition="recusa — não pressionar",
        )
        state["board"].append(item)
        log.info("conversion observe reason=REFUSAL item_id=%s", item.get("id"))


def _extract_window(text: str) -> str:
    m = _WINDOW_RE.search(text)
    return m.group(0).strip() if m else ""


def _extract_doc_noun(low: str) -> str:
    for noun in ("memorial de calculo", "memorial", "pdf", "planta", "laudo",
                 "art", "projeto", "documento"):
        if noun in low:
            return noun
    return UNKNOWN


def _extract_role(low: str) -> str:
    for role in ("socio", "sócio", "diretor", "decisor", "engenheiro"):
        if _fold(role) in low:
            return role
    return UNKNOWN


def _negated(low: str, word: str) -> bool:
    return bool(re.search(rf"\bnao\b.{{0,24}}\b{word}\b", low))


def _extract_commitment(text: str, source: str) -> dict | None:
    low = _fold(text)
    action = ""
    input_ = UNKNOWN
    if re.search(r"\b(envi\w+|mando|mandar|te mando|te envio)\b", low):
        if re.search(r"\b(documento|pdf|memorial|planta|laudo|art|projeto)\b", low):
            action = "enviar documento"
            input_ = _extract_doc_noun(low)
        elif "proposta" in low:
            action = "enviar proposta"
            input_ = "proposta"
        else:
            action = "enviar"
    elif re.search(r"\b(inclu\w+|chamar)\b", low) or re.search(
            r"\b(socio|diretor|decisor|engenheiro respons)\b", low):
        action = "incluir decisor"
        input_ = _extract_role(low)
    elif re.search(r"\b(retorn\w+|me liga|depois a gente)\b", low):
        action = "retornar"
    elif re.search(r"\b(visita|inspecao|inspeccao|campo)\b", low) and not _negated(low, "visita"):
        action = "agendar inspeção/campo"
    elif "escopo" in low and not _negated(low, "escopo"):
        action = "fechar escopo"
        input_ = "escopo"
    elif "proposta" in low and not _negated(low, "proposta"):
        action = "tratar proposta"
        input_ = "proposta"
    if not action:
        return None
    window = _extract_window(text) or UNKNOWN
    owner = UNKNOWN
    if re.search(r"\b(eu|vou|posso|te envio|te mando)\b", low):
        owner = "lead" if source == "system" else "founder"
    elif re.search(r"\b(voce|vc|voces)\b", low):
        owner = "founder" if source == "system" else "lead"
    return {
        "action": action,
        "owner": owner,
        "window": window,
        "input": input_,
        "condition": UNKNOWN,
    }


def _same_action(a: str, b: str) -> bool:
    fa, fb = _fold(a), _fold(b)
    if not fa or not fb or fa == UNKNOWN or fb == UNKNOWN:
        return False
    return fa == fb or fa in fb or fb in fa


def _merge_fields(item: dict, extracted: dict) -> None:
    for key in ("action", "owner", "window", "input", "condition"):
        incoming = _str(extracted.get(key))
        if incoming and incoming != UNKNOWN and _str(item.get(key)) in ("", UNKNOWN):
            item[key] = incoming
    item["questions"] = missing_field_questions(item)


def _promote_mutual(item: dict) -> bool:
    if item.get("state") == "SUGGESTED":
        return False
    if _fold(item.get("action") or "") == "avaliar":
        return False
    if item.get("state") == "REVOKED":
        return False
    item["state"] = "MUTUALLY_CONFIRMED"
    item["origin"] = "lead+founder"
    item["questions"] = missing_field_questions(item)
    log.info("conversion observe reason=MUTUALLY_CONFIRMED item_id=%s", item.get("id"))
    return True


def _open_from_other(state: dict, speaker_state: str) -> dict | None:
    for item in reversed(state.get("board") or []):
        st = item.get("state")
        if st in ("REVOKED", "MUTUALLY_CONFIRMED", "SUGGESTED"):
            continue
        if st in ("SAID_BY_LEAD", "SAID_BY_FOUNDER") and st != speaker_state:
            return item
    return None


def _line_parts(line) -> tuple[str, str]:
    if isinstance(line, dict):
        return _str(line.get("source")), _str(line.get("text"))
    return _str(getattr(line, "source", "")), _str(getattr(line, "text", ""))


def _absorb_answers(state: dict, plan: dict | None, text: str, source: str) -> None:
    if source != "system" or not isinstance(plan, dict):
        return
    low = _fold(text)
    if len(low) < 8:
        return
    for q in plan.get("unanswered_questions") or []:
        qf = _fold(q)
        # A lead reply after a question was asked counts when it is substantive
        # and not itself a question. Matching uses overlapping keywords.
        words = [w for w in re.findall(r"[a-z0-9]{4,}", qf) if w not in
                 ("qual", "quem", "como", "quando", "onde", "quais", "dessa", "deste",
                  "nesta", "neste", "para", "essa", "esse", "isso")]
        if words and sum(1 for w in words if w in low) >= min(2, len(words)):
            if q not in state["answered_questions"]:
                state["answered_questions"].append(q)


def observe_line(state: dict | None, line, plan: dict | None = None,
                 *, enabled: bool = True) -> dict:
    """Update the in-memory board from one transcript line.

    Copilot suggestions never become MUTUALLY_CONFIRMED. "Vou avaliar" is not
    acceptance. Missing date/owner/input become short questions, never fill.
    """
    state = _copy_state(state)
    if not enabled:
        state["reason"] = CONVERSION_DISABLED
        return state
    if isinstance(plan, dict) and state.get("commercial_stage") is None:
        # Record authority stage once; never overwrite from speech.
        state["commercial_stage"] = plan.get("commercial_stage") or UNKNOWN
    source, text = _line_parts(line)
    if not text:
        return state
    low = _fold(text)
    speaker_state = (
        "SAID_BY_LEAD" if source == "system"
        else "SAID_BY_FOUNDER" if source == "mic"
        else "UNKNOWN"
    )
    origin = "lead" if speaker_state == "SAID_BY_LEAD" else (
        "founder" if speaker_state == "SAID_BY_FOUNDER" else UNKNOWN
    )

    if _REVOKE_RE.search(low) or _REFUSAL_RE.search(low):
        _revoke_live(state, origin=origin, span=text)
        _absorb_answers(state, plan, text, source)
        return state

    if _EVAL_RE.search(low):
        item = _new_item(
            state,
            action="avaliar",
            owner=origin,
            state=speaker_state,
            origin=origin,
            span=text[:120],
            condition="avaliação não é aceite",
        )
        state["board"].append(item)
        for q in item["questions"]:
            if q not in state["pending_questions"]:
                state["pending_questions"].append(q)
        log.info("conversion observe reason=EVALUATION_NOT_ACCEPTANCE item_id=%s", item["id"])
        _absorb_answers(state, plan, text, source)
        return state

    extracted = _extract_commitment(text, source)
    confirmed_now = bool(_CONFIRM_RE.search(low))

    if extracted:
        pending = _open_from_other(state, speaker_state)
        if pending and _same_action(pending.get("action") or "", extracted["action"]):
            _merge_fields(pending, extracted)
            if confirmed_now or speaker_state != pending.get("state"):
                # Other side restated the same action: that is mutual if they
                # also confirm, or if both actually stated the action.
                if confirmed_now or (
                    speaker_state in ("SAID_BY_LEAD", "SAID_BY_FOUNDER")
                    and pending.get("state") in ("SAID_BY_LEAD", "SAID_BY_FOUNDER")
                    and speaker_state != pending.get("state")
                ):
                    _promote_mutual(pending)
            _absorb_answers(state, plan, text, source)
            return state
        item = _new_item(
            state,
            action=extracted["action"],
            owner=extracted["owner"],
            window=extracted["window"],
            input=extracted["input"],
            condition=extracted["condition"],
            state=speaker_state,
            origin=origin,
            span=text[:120],
        )
        if confirmed_now:
            # Confirmation words on a first mention do not make it mutual.
            pass
        state["board"].append(item)
        for q in item["questions"]:
            if q not in state["pending_questions"]:
                state["pending_questions"].append(q)
        log.info("conversion observe reason=%s item_id=%s", speaker_state, item["id"])
        _absorb_answers(state, plan, text, source)
        return state

    if confirmed_now:
        pending = _open_from_other(state, speaker_state)
        if pending:
            _promote_mutual(pending)
            extra = _extract_commitment(text, source) or {}
            if extra:
                _merge_fields(pending, extra)
            # Confirmation can also carry window/owner ("combinado, você envia até sexta")
            win = _extract_window(text)
            if win and _str(pending.get("window")) in ("", UNKNOWN):
                pending["window"] = win
            if re.search(r"\b(voce|vc)\b", low) and _str(pending.get("owner")) in ("", UNKNOWN):
                pending["owner"] = "lead" if source == "mic" else "founder"
            pending["questions"] = missing_field_questions(pending)
        _absorb_answers(state, plan, text, source)
        return state

    _absorb_answers(state, plan, text, source)
    return state


def observe_suggestion(state: dict | None, advice: dict | None,
                       *, enabled: bool = True) -> dict:
    """Copilot output is SUGGESTED only. Never a commitment."""
    state = _copy_state(state)
    if not enabled:
        state["reason"] = CONVERSION_DISABLED
        return state
    if not isinstance(advice, dict):
        return state
    action = _str(advice.get("faca") or advice.get("diga") or advice.get("action"))
    if not action:
        return state
    item = _new_item(
        state,
        action=action[:80],
        state="SUGGESTED",
        origin="copilot",
        span="",
        condition="sugestão do copiloto não é compromisso",
    )
    state["board"].append(item)
    log.info("conversion observe reason=SUGGESTED item_id=%s", item["id"])
    return state


def apply_lines(plan: dict | None, lines, *, enabled: bool = True,
                suggestions: list | None = None) -> dict:
    """Shipped entry: empty board + every transcript line, in order."""
    state = empty_state()
    if isinstance(plan, dict):
        state["commercial_stage"] = plan.get("commercial_stage") or UNKNOWN
        state["answered_questions"] = list(plan.get("answered_questions") or [])
    if not enabled:
        state["reason"] = CONVERSION_DISABLED
        return state
    for line in lines or []:
        state = observe_line(state, line, plan=plan, enabled=True)
    for sug in suggestions or []:
        state = observe_suggestion(state, sug, enabled=True)
    return state


def rebuild_from_session(session, *, enabled: bool = True) -> dict:
    """Rebuild board from current in-memory lines. Retracted echo is gone."""
    plan, _reason = meeting_plan_of(getattr(session, "handraiser_context", None) or {})
    if plan is None:
        plan = getattr(session, "meeting_plan", None)
    lines = getattr(session, "lines", None) or []
    suggestions = []
    last = getattr(session, "last_advice", None)
    if isinstance(last, dict):
        suggestions.append(last)
    return apply_lines(plan, lines, enabled=enabled, suggestions=suggestions)


def operational_output(plan: dict | None, state: dict | None, *,
                       explicit: bool = True) -> dict:
    """Six-field snapshot for Tiago. Never automatic. No transcript stored."""
    if not explicit:
        return {"ok": False, "reason": END_NOT_EXPLICIT}
    state = state if isinstance(state, dict) else empty_state()
    live = live_commitments(state)
    revoked = [i for i in state.get("board") or [] if i.get("state") == "REVOKED"]
    answered = list(state.get("answered_questions") or [])
    gaps = questions_to_ask(plan, answered)
    for item in state.get("board") or []:
        if item.get("state") == "MUTUALLY_CONFIRMED":
            continue
        for q in item.get("questions") or []:
            if q not in gaps:
                gaps.append(q)
    for q in state.get("pending_questions") or []:
        if q not in gaps:
            gaps.append(q)

    if live:
        decisao = "alcançada"
        confirmed = live[-1]
    elif revoked:
        decisao = "não alcançada"
        confirmed = None
    else:
        decisao = "não alcançada"
        confirmed = None

    blockers = firm_price_deadline_blockers(plan, state)
    stage = (plan or {}).get("commercial_stage") if isinstance(plan, dict) else UNKNOWN
    objective = (plan or {}).get("objective") if isinstance(plan, dict) else UNKNOWN
    kind = (plan or {}).get("work_kind_label") if isinstance(plan, dict) else UNKNOWN

    facts: list[str] = []
    if stage and stage != UNKNOWN:
        facts.append(f"estágio {stage}")
    if objective and objective != UNKNOWN:
        facts.append(f"objetivo: {objective}")
    if kind and kind != UNKNOWN:
        facts.append(str(kind))
    for item in state.get("board") or []:
        st = item.get("state")
        act = item.get("action") or UNKNOWN
        if st == "MUTUALLY_CONFIRMED":
            facts.append(f"combinado: {act}")
        elif st == "REVOKED":
            facts.append(f"revogado: {act}")
        elif st == "SAID_BY_LEAD":
            facts.append(f"lead disse: {act}")
        elif st == "SAID_BY_FOUNDER":
            facts.append(f"founder disse: {act}")
        elif st == "SUGGESTED":
            facts.append(f"sugestão (não compromisso): {act}")
    resumo = "; ".join(facts) if facts else "nenhum fato comercial confirmado nesta sessão"

    insumos: list[str] = []
    if confirmed and _str(confirmed.get("input")) not in ("", UNKNOWN):
        insumos.append(confirmed["input"])
    if isinstance(plan, dict):
        for x in plan.get("evidence_to_confirm") or []:
            if isinstance(x, str) and x.strip() and x.strip() not in insumos:
                insumos.append(x.strip())

    public_confirmed = _public_item(confirmed) if confirmed else None
    passo_txt = public_confirmed if public_confirmed else "nenhum"

    body = {
        "ok": True,
        "reason": "",
        "resumo_factual": resumo,
        "lacunas": gaps,
        "decisao": decisao,
        "proximo_passo_confirmado": passo_txt,
        "insumos_para_proposta": insumos,
        "bloqueios_preco_prazo": blockers,
        "resumo factual": resumo,
        "decisão alcançada/não alcançada": decisao,
        "próximo passo confirmado": passo_txt,
        "insumos para proposta": insumos,
        "itens que impedem preço/prazo firme": blockers,
        "commercial_stage": stage or UNKNOWN,
        "work_kind": (plan or {}).get("work_kind") if isinstance(plan, dict) else UNKNOWN,
    }
    # Never ship CRM / outbound / calendar / transcript keys.
    for k in CRM_SIDE_EFFECT_KEYS:
        body.pop(k, None)
    return body


def attach_meeting_plan(dossier: dict, extras: dict | None = None) -> dict:
    """Copy authority meeting_plan onto a dossier. Invalid plan → treatable reason."""
    if not isinstance(dossier, dict):
        return dossier
    src = None
    if isinstance(extras, dict) and extras.get("meeting_plan") is not None:
        src = extras.get("meeting_plan")
    elif dossier.get("meeting_plan") is not None:
        src = dossier.get("meeting_plan")
    plan, reason = parse_meeting_plan(src) if src is not None else (None, "")
    if plan is not None:
        dossier["meeting_plan"] = plan
        dossier.pop("meeting_plan_reason", None)
    elif reason:
        dossier["meeting_plan_reason"] = reason
        # Keep the raw object out of the prompt path; reason is treatable.
        dossier.pop("meeting_plan", None)
    return dossier
