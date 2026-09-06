"""Meeting plan, next-step board, and explicit operational output.

Pure functions over dicts and transcript lines. No Codex, no whisper, no
producer fetch, no CRM write, no calendar, no SMTP, no stage mutation.

Commercial stage, offer and work family are opaque authority data and are never
auto-changed or matched against a local catalogue.
"""
from __future__ import annotations

import copy
import logging
import re
from typing import Any

from ..conversation import ROLE_COUNTERPARTY, ROLE_OPERATOR, normalize_role

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

CONVERSION_DISABLED = "CONVERSION_DISABLED"
PLAN_SCHEMA_INVALID = "MEETING_PLAN_SCHEMA_INVALID"
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

# Deliberation verbs: "I need to look at / talk about / take this somewhere"
# before deciding. Note the list deliberately excludes delivery verbs
# (enviar, mandar, agendar): "vou enviar o memorial até sexta" is a real
# commitment and must keep reaching MUTUALLY_CONFIRMED.
_DELIB_VERB = (
    r"(?:ver|falar|conversar|levar|consultar|alinhar|checar|verificar|discutir|"
    r"validar|submeter|apresentar|passar|olhar|estudar|analisar|avaliar|pensar|"
    r"revisar|conferir|entender|pesquisar)"
)
# ...with somebody who is not the person in the room: the hedge is that the
# decision is elsewhere.
_DELIB_TARGET = (
    r"(?:a equipe|o time|meu time|minha equipe|nosso time|nossa equipe|"
    r"internamente|no interno|os socios|o socio|meu socio|meus socios|"
    r"a diretoria|o diretor|o juridico|o financeiro|o conselho|meu chefe|"
    r"a matriz|meus colegas|meu superior|a area tecnica|o setor|"
    r"a gestao|o gestor|o board|a controladoria|os envolvidos|"
    r"quem decide|o decisor|a engenharia)"
)
_EVAL_RE = re.compile(
    # 1) literal hedges already covered, plus close equivalents
    r"\b(?:talvez|provavelmente|possivelmente|acho que|creio que|vou tentar|"
    r"tentarei|se der|vou avaliar|vou pensar|vou analisar|vou estudar|vou verificar|"
    r"vou dar uma olhada|vamos nos falando|vamos se falando|preciso avaliar|"
    r"preciso pensar|preciso analisar|preciso estudar|preciso de um tempo|"
    r"vou ver internamente|vou verificar internamente|vou checar internamente|"
    r"deixa eu pensar|deixe[- ]me pensar|me deixa pensar|deixa eu ver|"
    r"depois eu vejo|depois eu retorno com|ainda nao posso decidir|"
    r"nao sou eu que decido|nao depende so de mim|nao decido sozinho|"
    r"preciso da aprovacao|depende de aprovacao|depende do meu socio|"
    r"depende da diretoria|tenho que ver isso|preciso ver isso)\b"
    # 2) structural: deliberation verb + a third party / internal target
    r"|\b(?:vou|preciso|tenho que|tenho de|vamos|quero|gostaria de|deixa eu|"
    r"deixe[- ]me|teria que|iria|pretendo|devo)\s+(?:\w+\s+){0,2}?"
    + _DELIB_VERB + r"\b[^.;!?]{0,40}?\b" + _DELIB_TARGET + r"\b",
    re.I,
)
# Global refusal: the whole thread stops. Action-local refusal ("não vou
# enviar o memorial") is handled by _negated_any in _extract_commitment, so
# that one refused deliverable does not revoke every other live commitment.
_REFUSAL_RE = re.compile(
    r"\b(n[aã]o quero|n[aã]o vamos seguir|recuso|sem interesse|"
    r"n[aã]o tenho interesse|n[aã]o vamos fechar|n[aã]o vamos continuar|"
    r"n[aã]o quero seguir|n[aã]o vamos prosseguir|n[aã]o vamos avan[cç]ar|"
    r"n[aã]o h[aá] interesse|vamos parar por aqui|n[aã]o quero mais)\b",
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

# --- utterance classification -------------------------------------------
# A next step only becomes MUTUALLY_CONFIRMED when BOTH halves are real
# human affirmations. A question, a hedge, a refusal or a copilot suggestion
# is never one half of a mutual agreement.
UTT_QUESTION = "QUESTION"
UTT_HEDGE = "HEDGE"
UTT_REFUSAL = "REFUSAL"
UTT_CONFIRM = "CONFIRM"
UTT_COMMIT = "COMMIT"
UTT_ASSERT = "ASSERT"

#: classes that may open a next step (the first half of a mutual pair)
AFFIRMATIVE_CLASSES = (UTT_CONFIRM, UTT_COMMIT, UTT_ASSERT)
#: classes that may close a next step (the confirming half). Strictly
#: narrower: restating or asserting is not agreeing.
CONFIRMING_CLASSES = (UTT_CONFIRM, UTT_COMMIT)

_INTERROG = (
    r"(?:quando|quem|como|por que|qual|quais|onde|quanto|quantos|quantas|"
    r"sera que|pode me dizer|me diz)"
)
# A transcriber often drops the "?". An unpunctuated question puts the
# interrogative at the start ("Quando você envia") or at the end ("Enviar o
# memorial quando"). Mid-sentence it is usually comparative ("faremos como
# você pediu") and must not disqualify a real confirmation.
_INTERROG_START_RE = re.compile(r"^\s*(?:e\s+|entao\s+|mas\s+)?" + _INTERROG + r"\b", re.I)
_INTERROG_END_RE = re.compile(r"\b" + _INTERROG + r"\b(?:\s+\w+){0,2}\s*[.!]*\s*$", re.I)
# First person singular: the speaker is taking the action.
_FIRST_PERSON_SG_RE = re.compile(
    r"\b(eu|vou|posso|consigo|preciso|te envio|te mando|envio|enviarei|mando|"
    r"mandarei|retorno|retornarei|fico de|me comprometo|garanto|providencio|"
    r"providenciarei|assumo|separo|preparo|levo|trago|marco|agendo)\b",
    re.I,
)
# First person plural: a joint action; the owner is both sides, which is a
# resolvable owner for a joint decision but NOT for a deliverable.
_FIRST_PERSON_PL_RE = re.compile(
    r"\b(vamos|fechamos|combinamos|incluimos|revisamos|agendamos|marcamos|"
    r"faremos|enviaremos|revisaremos|fecharemos|incluiremos)\b",
    re.I,
)
_SECOND_PERSON_RE = re.compile(r"\b(voce|vc|voces)\b", re.I)

JOINT_OWNER = "lead+founder"
#: actions whose owner must be one identified party — "vamos enviar" does not
#: say who sends, so it must not confirm.
_DELIVERABLE_ACTIONS = ("enviar", "enviar documento", "enviar proposta", "retornar")

_FOLD_ACCENTS = str.maketrans(
    "áàãâäéèêëíìîïóòõôöúùûüçÁÀÃÂÄÉÈÊËÍÌÎÏÓÒÕÔÖÚÙÛÜÇ",
    "aaaaaeeeeiiiiooooouuuucAAAAAEEEEIIIIOOOOOUUUUC",
)


def _str(v) -> str:
    return v.strip() if isinstance(v, str) and v.strip() else ""


def _fold(s: str) -> str:
    return _str(s).translate(_FOLD_ACCENTS).lower()


def is_question(text: str) -> bool:
    """A question is never an affirmation.

    Any '?' counts, and so does an interrogative marker in a short reply —
    "Enviar o memorial de cálculo quando?" and "Enviar quando" are the same
    utterance with and without punctuation from the transcriber.
    """
    t = _str(text)
    if not t:
        return False
    if "?" in t:
        return True
    low = _fold(t)
    if len(low.split()) > 12:
        return False
    return bool(_INTERROG_START_RE.search(low) or _INTERROG_END_RE.search(low))


def classify_utterance(text: str) -> str:
    """Classify one transcript line for the mutual-confirmation gate.

    Order matters: a hedge or a question dressed in agreement words is still
    a hedge or a question. Never returns a class that makes confirmation
    easier than the raw text warrants.
    """
    t = _str(text)
    if not t:
        return UTT_ASSERT
    low = _fold(t)
    if _REVOKE_RE.search(low) or _REFUSAL_RE.search(low):
        return UTT_REFUSAL
    if _EVAL_RE.search(low):
        return UTT_HEDGE
    if is_question(t):
        return UTT_QUESTION
    if _CONFIRM_RE.search(low):
        return UTT_CONFIRM
    if _FIRST_PERSON_SG_RE.search(low) or _FIRST_PERSON_PL_RE.search(low):
        return UTT_COMMIT
    return UTT_ASSERT


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
    # Authority-owned stage: normalise only its transport shape. New stages
    # must not require a MeetCFG code change or local alias table.
    token = re.sub(r"[^A-Z0-9]+", "_", _fold(s).upper()).strip("_")
    return token[:64] if token else UNKNOWN


def _norm_kind(raw) -> str:
    s = _str(raw)
    if not s:
        return ""
    token = re.sub(r"[^A-Z0-9]+", "_", _fold(s).upper()).strip("_")
    return token[:64] if token else UNKNOWN


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
    for i, r in enumerate(roles):
        if isinstance(r, str) and r.strip():
            clean_roles.append({"name": UNKNOWN, "role": r.strip()})
        elif isinstance(r, dict):
            for key in ("name", "role", "papel"):
                if r.get(key) is not None and not isinstance(r.get(key), str):
                    return None, f"participant_roles[{i}].{key} deve ser string ou null"
            clean_roles.append({
                "name": _str(r.get("name")) or UNKNOWN,
                "role": _str(r.get("role") or r.get("papel")) or UNKNOWN,
            })
        else:
            return None, f"participant_roles[{i}] deve ser string ou objeto"
    if not clean_roles:
        clean_roles = [{"name": UNKNOWN, "role": UNKNOWN}]

    def _str_list(key, *alts):
        for k in (key, *alts):
            v = raw.get(k)
            if isinstance(v, list):
                if not all(isinstance(x, str) for x in v):
                    return None
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
    criterion_origin = "authority" if criterion else "unknown"
    if not criterion:
        criterion = UNKNOWN

    raw_gaps = raw.get("gaps") or raw.get("lacunas")
    gaps: list[dict] = []
    if raw_gaps is not None:
        if not isinstance(raw_gaps, list):
            return None, "gaps deve ser uma lista"
        for i, gap in enumerate(raw_gaps):
            if not isinstance(gap, dict):
                return None, f"gaps[{i}] deve ser um objeto"
            gap_id = _str(gap.get("id"))
            question = _str(gap.get("question") or gap.get("pergunta"))
            status = _str(gap.get("status")).upper() or UNKNOWN
            if not gap_id or not question:
                return None, f"gaps[{i}] exige id e question"
            if not isinstance(gap.get("blocking", True), bool):
                return None, f"gaps[{i}].blocking deve ser booleano"
            answer = _str(gap.get("answer") or gap.get("resposta")) or UNKNOWN
            if status == "ANSWERED" and answer == UNKNOWN:
                status = UNKNOWN
            gaps.append({
                "id": gap_id,
                "question": question,
                "status": status,
                "answer": answer,
                "blocking": gap.get("blocking", True),
                "repeat_reason": _str(gap.get("repeat_reason")),
            })
    else:
        answered_folded = {_fold(x) for x in answered}
        for i, question in enumerate(unanswered):
            gaps.append({
                "id": f"question_{i + 1}",
                "question": question,
                "status": "ANSWERED" if _fold(question) in answered_folded else "OPEN",
                "answer": UNKNOWN,
                "blocking": True,
                "repeat_reason": "",
            })

    repeat_reasons = raw.get("repeat_reasons")
    if repeat_reasons is None:
        repeat_reasons = {}
    if not isinstance(repeat_reasons, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in repeat_reasons.items()
    ):
        return None, "repeat_reasons deve ser um objeto de strings"

    constraints = _str_list("constraints", "limites")
    if constraints is None:
        return None, "constraints deve ser uma lista de strings"

    offer = raw.get("offer") if isinstance(raw.get("offer"), dict) else {}
    offer_id = _str(raw.get("offer_id") or offer.get("id")) or UNKNOWN
    offer_family = _str(raw.get("offer_family") or offer.get("family")) or UNKNOWN
    next_state = _str(
        raw.get("next_state") or raw.get("proximo_estado") or offer.get("next_state")
    ) or UNKNOWN
    context_status = _str(raw.get("context_status")).upper() or UNKNOWN
    firm_allowed = raw.get("firm_commitment_allowed") is True

    limited = stage == UNKNOWN or objective == UNKNOWN
    plan = {
        "schema": SCHEMA_MEETING_PLAN,
        "commercial_stage": stage,
        "objective": objective,
        "participant_roles": clean_roles,
        "unanswered_questions": unanswered,
        "answered_questions": answered,
        "gaps": gaps,
        "repeat_reasons": repeat_reasons,
        "evidence_to_confirm": evidence,
        "scope_limits": scope_limits,
        "conflict_limits": conflict_limits,
        "advancement_criterion": criterion,
        "advancement_criterion_origin": criterion_origin,
        "work_kind": kind,
        "work_kind_label": kind,
        "offer_id": offer_id,
        "offer_family": offer_family,
        "next_state": next_state,
        "acquisition_channel": _str(raw.get("acquisition_channel")) or UNKNOWN,
        "conversation_channel": _str(raw.get("conversation_channel")) or UNKNOWN,
        "situation": _str(raw.get("situation") or raw.get("situacao")) or UNKNOWN,
        "intent": _str(raw.get("intent") or raw.get("intencao")) or UNKNOWN,
        "constraints": constraints,
        "context_status": context_status,
        "firm_commitment_allowed": firm_allowed,
        "limited": limited or context_status != "CURRENT",
    }
    return plan, ""


def meeting_plan_of(ctx: dict | None) -> tuple[dict | None, str]:
    if not isinstance(ctx, dict):
        return None, ""
    raw = ctx.get("meeting_plan")
    if raw is not None and not isinstance(raw, dict):
        return parse_meeting_plan(raw)
    offer = ctx.get("offer") if isinstance(ctx.get("offer"), dict) else {}
    intent = ctx.get("intent") if isinstance(ctx.get("intent"), dict) else {}
    projected = dict(raw or {})
    projected.setdefault("schema", SCHEMA_MEETING_PLAN)
    # Envelope safety wins over a nested plan. A stale/contradictory context
    # cannot become CURRENT because a producer left an old plan attached.
    if ctx.get("context_status") is not None:
        projected["context_status"] = ctx.get("context_status")
    for key, value in (
        ("commercial_stage", ctx.get("commercial_stage") or ctx.get("stage")),
        ("objective", ctx.get("objective")),
        ("participant_roles", ctx.get("participant_roles")),
        ("gaps", ctx.get("gaps")),
        ("advancement_criterion", ctx.get("advancement_criterion")),
        ("next_state", ctx.get("next_state") or offer.get("next_state")),
        ("offer_id", offer.get("id")),
        ("offer_family", offer.get("family")),
        ("acquisition_channel", ctx.get("acquisition_channel")),
        ("conversation_channel", ctx.get("conversation_channel")),
        ("situation", ctx.get("situation") or ctx.get("situacao")),
        ("intent", intent.get("kind") or ctx.get("intention")),
        ("constraints", ctx.get("constraints") or ctx.get("limits")),
        ("conflict_limits", ctx.get("conflicts")),
        ("context_status", ctx.get("context_status")),
    ):
        current = projected.get(key)
        missing = current in (None, "", UNKNOWN) or current == [] or current == {}
        incoming_known = value not in (None, "", UNKNOWN) and value != [] and value != {}
        if missing and incoming_known:
            projected[key] = value
    # A truly empty context keeps the old manual-mode seam.
    meaningful = [v for k, v in projected.items() if k != "schema" and v not in (None, "", [], {})]
    explicit_partial = any(
        key in ctx for key in (
            "acquisition_channel", "conversation_channel", "context_status",
            "situation", "situacao", "intent", "objective", "gaps",
            "advancement_criterion", "next_state", "offer",
        )
    )
    if raw is None and not meaningful and not explicit_partial:
        return None, ""
    return parse_meeting_plan(projected)


def questions_to_ask(plan: dict | None, answered: list[str] | None = None) -> list[str]:
    """Unanswered questions minus already-answered, unless repeat_reason is set."""
    if not isinstance(plan, dict):
        return []
    answered_l = [_fold(x) for x in (answered or []) + list(plan.get("answered_questions") or [])]
    repeat_reasons = plan.get("repeat_reasons") if isinstance(plan.get("repeat_reasons"), dict) else {}
    out: list[str] = []
    seen: set[str] = set()
    planned_questions = []
    for gap in plan.get("gaps") or []:
        if not isinstance(gap, dict):
            continue
        if _str(gap.get("status")).upper() == "ANSWERED" and not _str(gap.get("repeat_reason")):
            continue
        q = _str(gap.get("question"))
        if q:
            planned_questions.append(q)
            if _str(gap.get("repeat_reason")):
                repeat_reasons[q] = _str(gap.get("repeat_reason"))
    if not (plan.get("gaps") or []):
        planned_questions = list(plan.get("unanswered_questions") or [])
    for q in planned_questions:
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


def advancement_readiness(
    plan: dict | None, answered: list[str] | None = None,
) -> tuple[bool, list[str]]:
    """Generic objective/gap/criterion gate; never branches on offer or stage."""
    if not isinstance(plan, dict):
        return False, ["plano UNKNOWN"]
    blockers: list[str] = []
    if _str(plan.get("objective")) in ("", UNKNOWN):
        blockers.append("objetivo UNKNOWN")
    if _str(plan.get("advancement_criterion")) in ("", UNKNOWN):
        blockers.append("critério de avanço UNKNOWN")
    if _str(plan.get("next_state")) in ("", UNKNOWN):
        blockers.append("próximo estado UNKNOWN")
    status = _str(plan.get("context_status")).upper()
    if status != "CURRENT":
        blockers.append(f"contexto {status or UNKNOWN}")
    answered_folded = {_fold(x) for x in (answered or []) if _str(x)}
    for gap in plan.get("gaps") or []:
        if not isinstance(gap, dict) or gap.get("blocking") is False:
            continue
        question_answered = _fold(gap.get("question") or "") in answered_folded
        if _str(gap.get("status")).upper() != "ANSWERED" and not question_answered:
            blockers.append(f"lacuna aberta: {_str(gap.get('id')) or UNKNOWN}")
    return not blockers, blockers


def _role_known(plan: dict | None) -> bool:
    if not isinstance(plan, dict):
        return False
    for r in plan.get("participant_roles") or []:
        if isinstance(r, dict) and _str(r.get("role")) not in ("", UNKNOWN):
            return True
    return False


def firm_price_deadline_blockers(plan: dict | None, state: dict | None) -> list[str]:
    """Fail closed unless the received plan explicitly permits a firm promise.

    The gate is deliberately offer/stage agnostic.  The authority declares the
    missing facts as gaps and opts in only after price, deadline, capacity and
    attribution have actually been checked.
    """
    answered = list((state or {}).get("answered_questions") or []) \
        if isinstance(state, dict) else []
    _ready, blockers = advancement_readiness(plan, answered)
    if not isinstance(plan, dict) or plan.get("firm_commitment_allowed") is not True:
        blockers.append("preço/prazo firme não autorizado pelo contexto")
    return list(dict.fromkeys(blockers))


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
        f"Próximo estado possível: {plan.get('next_state') or UNKNOWN}",
        f"Oferta opaca: {plan.get('offer_id') or UNKNOWN}",
        f"Família opaca: {plan.get('offer_family') or UNKNOWN}",
        f"Canal de aquisição: {plan.get('acquisition_channel') or UNKNOWN}",
        f"Canal da conversa: {plan.get('conversation_channel') or UNKNOWN}",
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
    ready, blockers = advancement_readiness(plan, answered)
    parts.append("Pronto para avançar: " + ("sim" if ready else "não"))
    if blockers:
        parts.append("Bloqueios de avanço: " + "; ".join(blockers))
    parts.append(
        "Use somente o contexto recebido: hipótese/análise não vira conclusão, "
        "hesitação não vira aceite e escopo não vira proposta ou contratação."
    )
    if plan.get("limited"):
        parts.append("Contexto incompleto: modo limitado/manual.")
    parts.append(
        "Sem autorização explícita e fatos confirmados no plano, não sugira "
        "produto, preço, prazo, capacidade, ART, atribuição ou entrega firme."
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


def _refresh_pending_questions(state: dict) -> None:
    """Rebuild short questions from the live board. Filled fields drop."""
    qs: list[str] = []
    seen: set[str] = set()
    for item in state.get("board") or []:
        if not isinstance(item, dict) or item.get("state") == "REVOKED":
            continue
        item["questions"] = missing_field_questions(item)
        for q in item["questions"]:
            if q not in seen:
                seen.add(q)
                qs.append(q)
    state["pending_questions"] = qs


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
        # per-side utterance class, filled by _record_class; the mutual gate
        # refuses to promote a pair that is not two real affirmations.
        "utterance_classes": {},
        "echo_derived": False,
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
    return bool(re.search(rf"\b(?:nao|nunca|jamais|nem)\b.{{0,24}}\b{word}\b", low))


def _negated_any(low: str, *words: str) -> bool:
    return any(_negated(low, w) for w in words)


def _extract_owner(low: str, role: str) -> str:
    """Who is on the hook. Never invents; UNKNOWN blocks confirmation."""
    role = normalize_role(role) or ""
    if role not in (ROLE_OPERATOR, ROLE_COUNTERPARTY):
        return UNKNOWN
    speaker = "lead" if role == ROLE_COUNTERPARTY else "founder"
    other = "founder" if role == ROLE_COUNTERPARTY else "lead"
    if _FIRST_PERSON_SG_RE.search(low):
        return speaker
    if _SECOND_PERSON_RE.search(low):
        return other
    if _FIRST_PERSON_PL_RE.search(low):
        # "vamos fechar o escopo" — both sides own a joint decision. For a
        # deliverable this is refused later by _promote_mutual.
        return JOINT_OWNER
    return UNKNOWN


_ACTION_STOPWORDS = {
    "agora", "ainda", "amanha", "com", "como", "desta", "deste", "estado",
    "fazer", "para", "passo", "pode", "posso", "proximo", "seguinte",
    "sera", "vamos", "voce",
}


def _matches_authority_action(text: str, next_state: str) -> bool:
    """Conservative lexical link; false negatives are safer than invented close."""
    def tokens(value: str) -> set[str]:
        return {
            word for word in re.findall(r"[a-z0-9]{4,}", _fold(value))
            if word not in _ACTION_STOPWORDS
        }

    expected = tokens(next_state)
    heard = tokens(text)
    if not expected:
        return False
    required = 1 if len(expected) == 1 else 2
    return len(expected & heard) >= required


def _generic_spoken_action(text: str, next_state: str) -> str:
    """Keep the human verb plus shared target nouns; never relabel as outcome."""
    heard = re.findall(r"[a-z0-9]{4,}", _fold(text))
    expected = {
        word for word in re.findall(r"[a-z0-9]{4,}", _fold(next_state))
        if word not in _ACTION_STOPWORDS
    }
    aux = {"consigo", "irei", "posso", "vamos", "voce"}
    verb = ""
    for word in heard:
        if word in aux or word in expected or word in _ACTION_STOPWORDS:
            continue
        verb = word
        break
    shared: list[str] = []
    for word in heard:
        if word in expected and word not in shared:
            shared.append(word)
    return " ".join(([verb] if verb else []) + shared)[:80]


def _extract_commitment(text: str, role: str, plan: dict | None = None) -> dict | None:
    # Legacy direct callers may still pass mic|system. Normalize at the seam;
    # the conversion implementation below only reasons about roles.
    role = normalize_role(role) or ""
    low = _fold(text)
    # Mentioning a commercial object while rejecting/contesting it is not a
    # commitment about that object. Keep the disagreement as transcript data;
    # never relabel it as a close.
    if re.search(
        r"\b(discord\w*|rejeit\w*|contest\w*|question\w*|"
        r"nao\s+(?:concord\w*|aceit\w*))\b",
        low,
    ):
        return None
    action = ""
    input_ = UNKNOWN
    if (
        re.search(r"\b(envi\w+|mando|mandar|te mando|te envio)\b", low)
        # S3(c): "não vou enviar o memorial" is a refusal of this action, not
        # a commitment to it.
        and not _negated_any(low, r"envi\w+", r"mand\w+")
    ):
        if re.search(r"\b(documento|pdf|memorial|planta|laudo|art|projeto)\b", low):
            action = "enviar documento"
            input_ = _extract_doc_noun(low)
        elif "proposta" in low:
            action = "enviar proposta"
            input_ = "proposta"
        else:
            action = "enviar"
    elif (
        re.search(r"\b(inclu\w+|chamar)\b", low)
        and re.search(r"\b(socio|diretor|decisor|quem decide)\b", low)
        and not _negated_any(low, r"inclu\w+", r"cham\w+", "socio", "diretor", "decisor")
    ):
        action = "incluir decisor"
        input_ = _extract_role(low)
    elif (
        re.search(r"\b(retorn\w+|me liga|depois a gente)\b", low)
        and not _negated_any(low, r"retorn\w+", r"lig\w+")
    ):
        action = "retornar"
    elif (
        re.search(r"\b(visita|inspecao|inspeccao|campo)\b", low)
        and not _negated_any(low, "visita", "inspecao", "inspeccao", "campo", r"agend\w+")
    ):
        action = "agendar inspeção/campo"
    elif "escopo" in low and not _negated(low, "escopo"):
        action = "fechar escopo"
        input_ = "escopo"
    elif "proposta" in low and not _negated(low, "proposta"):
        action = "tratar proposta"
        input_ = "proposta"
    elif (
        isinstance(plan, dict)
        and _str(plan.get("next_state")) not in ("", UNKNOWN)
        and classify_utterance(text) == UTT_COMMIT
        and _matches_authority_action(text, _str(plan.get("next_state")))
    ):
        # Unknown offers still need a next-step board.  A first-person human
        # commitment can use the authority-provided next state as its action;
        # MeetCFG does not need a noun/offer dictionary to understand it.
        action = _generic_spoken_action(text, _str(plan.get("next_state")))
    if not action:
        return None
    window = _extract_window(text) or UNKNOWN
    owner = _extract_owner(low, role)
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


def _record_class(item: dict, speaker_state: str, utt_class: str,
                  echo_derived: bool = False) -> None:
    """Remember how each side phrased itself, per side, for the mutual gate."""
    classes = item.get("utterance_classes")
    if not isinstance(classes, dict):
        classes = {}
        item["utterance_classes"] = classes
    classes[speaker_state] = utt_class
    if echo_derived and item.get("state") == speaker_state:
        item["echo_derived"] = True


def _stated_class(item: dict) -> str:
    """Class of the utterance that opened this item, from its own side."""
    classes = item.get("utterance_classes")
    if isinstance(classes, dict):
        got = _str(classes.get(item.get("state")))
        if got:
            return got
    return UTT_ASSERT


def _promote_mutual(item: dict, *, confirming_class: str,
                    confirming_echo: bool) -> bool:
    """The ONLY door to MUTUALLY_CONFIRMED. Every gate lives here.

    A next step is confirmed only when two different human speakers each
    produced a real affirmation about the same action, and the snapshot can
    name an owner and a date/window. Anything short of that stays pending and
    keeps its missing-field questions.
    """
    item_id = item.get("id")

    def _block(reason: str) -> bool:
        log.info("conversion promote refused reason=%s item_id=%s", reason, item_id)
        return False

    if item.get("state") == "SUGGESTED":
        return _block("COPILOT_SUGGESTION_NOT_COMMITMENT")
    if item.get("state") == "REVOKED":
        return _block("REVOKED")
    if _fold(item.get("action") or "") == "avaliar":
        return _block("EVALUATION_NOT_ACCEPTANCE")

    # S3(e): a line the echo suppressor produced by retracting the other
    # stream's near-duplicate is known-duplicated speech, not a fresh
    # utterance from the other party. It may never be a confirming half.
    if confirming_echo:
        return _block("ECHO_DERIVED_NOT_INDEPENDENT")
    if item.get("echo_derived") is True:
        return _block("ECHO_DERIVED_NOT_INDEPENDENT")

    # S3(b): both halves must be actual affirmations. A question, a hedge or
    # a refusal restating the same action is not agreement.
    stated = _stated_class(item)
    if stated not in AFFIRMATIVE_CLASSES:
        return _block(f"NOT_AN_AFFIRMATION_{stated}")
    if confirming_class not in CONFIRMING_CLASSES:
        return _block(f"NOT_AN_AFFIRMATION_{confirming_class}")
    # Invariant, kept explicit so it survives any widening of the class sets:
    # at least one half must carry real agreement or commitment language.
    if stated not in CONFIRMING_CLASSES and confirming_class not in CONFIRMING_CLASSES:
        return _block("NO_AGREEMENT_LANGUAGE")

    # S5: no date and no owner means there is nothing a human can act on.
    owner = _str(item.get("owner"))
    window = _str(item.get("window"))
    if owner in ("", UNKNOWN):
        return _block("OWNER_UNRESOLVED")
    if window in ("", UNKNOWN):
        return _block("WINDOW_UNRESOLVED")
    if owner == JOINT_OWNER and _fold(item.get("action") or "") in _DELIVERABLE_ACTIONS:
        # "vamos enviar o memorial" never says who sends it.
        return _block("OWNER_NOT_INDIVIDUAL_FOR_DELIVERABLE")

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


def _line_parts(line) -> tuple[str, str, bool]:
    """(role, text, echo_derived), normalizing legacy source-only fixtures."""
    if isinstance(line, dict):
        role = normalize_role(line.get("role"), legacy_source=line.get("source")) or ""
        return (role, _str(line.get("text")),
                bool(line.get("echo_derived")))
    role = normalize_role(
        getattr(line, "role", None), legacy_source=getattr(line, "source", None),
    ) or ""
    echo_untrusted = getattr(line, "echo_untrusted", None)
    return (role, _str(getattr(line, "text", "")),
            bool(echo_untrusted() if callable(echo_untrusted)
                 else getattr(line, "echo_derived", False)))


def _absorb_answers(state: dict, plan: dict | None, text: str, role: str) -> None:
    if role != ROLE_COUNTERPARTY or not isinstance(plan, dict):
        return
    low = _fold(text)
    if len(low) < 8:
        return
    if re.search(
        r"\b(?:nao sei|desconheco|nao tenho essa informacao|sem informacao|"
        r"nao posso responder|nao ficou claro)\b",
        low,
    ):
        return
    questions = list(plan.get("unanswered_questions") or [])
    for gap in plan.get("gaps") or []:
        if isinstance(gap, dict) and _str(gap.get("question")):
            questions.append(_str(gap.get("question")))
    for q in dict.fromkeys(questions):
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
    role, text, echo_derived = _line_parts(line)
    if not text:
        return state
    low = _fold(text)
    speaker_state = (
        "SAID_BY_LEAD" if role == ROLE_COUNTERPARTY
        else "SAID_BY_FOUNDER" if role == ROLE_OPERATOR
        else "UNKNOWN"
    )
    # S3(d): an unrecognised role is not a third speaker. It must never
    # satisfy _open_from_other's "different speaker" test.
    if speaker_state == "UNKNOWN":
        log.warning("conversion observe reason=UNKNOWN_ROLE_IGNORED role=%r", role)
        return state
    origin = "lead" if speaker_state == "SAID_BY_LEAD" else "founder"
    utt_class = classify_utterance(text)

    if _REVOKE_RE.search(low) or _REFUSAL_RE.search(low):
        _revoke_live(state, origin=origin, span=text)
        _absorb_answers(state, plan, text, role)
        _refresh_pending_questions(state)
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
        _record_class(item, speaker_state, utt_class, echo_derived)
        state["board"].append(item)
        log.info("conversion observe reason=EVALUATION_NOT_ACCEPTANCE item_id=%s", item["id"])
        _absorb_answers(state, plan, text, role)
        _refresh_pending_questions(state)
        return state

    extracted = _extract_commitment(text, role, plan)
    confirmed_now = bool(_CONFIRM_RE.search(low))

    if extracted:
        pending = _open_from_other(state, speaker_state)
        if pending and _same_action(pending.get("action") or "", extracted["action"]):
            _merge_fields(pending, extracted)
            _record_class(pending, speaker_state, utt_class, echo_derived)
            if confirmed_now or speaker_state != pending.get("state"):
                # Other side restated the same action: that is mutual only if
                # both halves are real affirmations with owner and window.
                if confirmed_now or (
                    speaker_state in ("SAID_BY_LEAD", "SAID_BY_FOUNDER")
                    and pending.get("state") in ("SAID_BY_LEAD", "SAID_BY_FOUNDER")
                    and speaker_state != pending.get("state")
                ):
                    _promote_mutual(pending, confirming_class=utt_class,
                                    confirming_echo=echo_derived)
            _absorb_answers(state, plan, text, role)
            _refresh_pending_questions(state)
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
        _record_class(item, speaker_state, utt_class, echo_derived)
        if confirmed_now:
            # Confirmation words on a first mention do not make it mutual.
            pass
        state["board"].append(item)
        log.info("conversion observe reason=%s item_id=%s class=%s",
                 speaker_state, item["id"], utt_class)
        _absorb_answers(state, plan, text, role)
        _refresh_pending_questions(state)
        return state

    if confirmed_now:
        pending = _open_from_other(state, speaker_state)
        if pending:
            # Merge what the confirming line carries BEFORE the gate runs:
            # "combinado, você envia até sexta" supplies the owner and window
            # the promotion rule requires.
            extra = _extract_commitment(text, role, plan) or {}
            if extra:
                _merge_fields(pending, extra)
            win = _extract_window(text)
            if win and _str(pending.get("window")) in ("", UNKNOWN):
                pending["window"] = win
            if re.search(r"\b(voce|vc)\b", low) and _str(pending.get("owner")) in ("", UNKNOWN):
                pending["owner"] = (
                    "lead" if role == ROLE_OPERATOR else "founder"
                )
            _record_class(pending, speaker_state, utt_class, echo_derived)
            _promote_mutual(pending, confirming_class=utt_class,
                            confirming_echo=echo_derived)
            pending["questions"] = missing_field_questions(pending)
        _absorb_answers(state, plan, text, role)
        _refresh_pending_questions(state)
        return state

    _absorb_answers(state, plan, text, role)
    _refresh_pending_questions(state)
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
    _refresh_pending_questions(state)
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
    _refresh_pending_questions(state)
    live = live_commitments(state)
    revoked = [i for i in state.get("board") or [] if i.get("state") == "REVOKED"]
    answered = list(state.get("answered_questions") or [])
    gaps = questions_to_ask(plan, answered)
    for q in state.get("pending_questions") or []:
        if q not in gaps:
            gaps.append(q)

    if live:
        decisao = "alcançada"
        # Intentional: "próximo passo confirmado" is the single next step the
        # founder acts on, so it shows the most recent confirmed item. Every
        # confirmed item is still listed in resumo_factual.
        confirmed = live[-1]
    elif revoked:
        decisao = "não alcançada"
        confirmed = None
    else:
        decisao = "não alcançada"
        confirmed = None

    blockers = firm_price_deadline_blockers(plan, state)
    advancement_ready, advancement_blockers = advancement_readiness(plan, answered)
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
    seen_insumos: set[str] = set()
    if confirmed and _str(confirmed.get("input")) not in ("", UNKNOWN):
        insumos.append(confirmed["input"])
        seen_insumos.add(_fold(confirmed["input"]))
    if isinstance(plan, dict):
        # Plan evidence is still TO BE confirmed. Label it so the founder does
        # not read it as an input already in hand. Dedup folded: the confirmed
        # input comes back accent-folded from _extract_doc_noun.
        for x in plan.get("evidence_to_confirm") or []:
            if isinstance(x, str) and x.strip() and _fold(x) not in seen_insumos:
                seen_insumos.add(_fold(x))
                insumos.append(f"{x.strip()} (a confirmar)")

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
        "advancement_ready": advancement_ready,
        "advancement_blockers": advancement_blockers,
        "resumo factual": resumo,
        "decisão alcançada/não alcançada": decisao,
        "próximo passo confirmado": passo_txt,
        "insumos para proposta": insumos,
        "itens que impedem preço/prazo firme": blockers,
        "pronto para avançar": advancement_ready,
        "bloqueios de avanço": advancement_blockers,
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
