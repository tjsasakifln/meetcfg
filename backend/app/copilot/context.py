"""Contexto estruturado por lead — CONFENGE_SALES_CONTEXT/1.0.

Complementa, não substitui, o `sales_context.md` de texto livre: aquele carrega
posicionamento e estratégia da CONFENGE; este carrega o lead específico da
reunião — de que canal ele veio, o que ele já recebeu, e o que **não** pode ser
afirmado sobre ele.

Fail-closed on purpose: a document that fails any check is discarded whole, and
the caller falls back to manual mode. Partially trusting a lead dossier is worse
than ignoring it — a half-parsed dossier is exactly the material the model turns
into a confident false claim about someone else's company.

Stdlib only, and deliberately no `..config` import: tools/pre_call_brief.py
imports this module standalone, and the caller passes an already-resolved path.
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path

log = logging.getLogger("meetcfg.context")

SCHEMA_ID = "CONFENGE_SALES_CONTEXT/1.0"
# Collection/index is a different contract. Warmbly's live export still tags the
# collection with SCHEMA_ID — Meetcfg must never treat that envelope as a dossier.
SCHEMA_EXPORT = "CONFENGE_SALES_CONTEXT_EXPORT/1.0"

_CNPJ_FMT_RE = re.compile(r"^\d{2}\.\d{3}\.\d{3}/\d{4}-\d{2}$")
_CNPJ_DIGITS_RE = re.compile(r"^\d{14}$")

# Compatibility names used only for human labels.  Validation does not close
# this set: acquisition taxonomy is authority-owned and UNKNOWN is valid.
CHANNELS = (
    "OUTBOUND_FIRST_TOUCH",
    "INTEL_SEED",
    "INBOUND_LIVE",
    "PARTNER",
    "OTHER",
    "UNKNOWN",
)

CHANNEL_LABELS = {
    "OUTBOUND_FIRST_TOUCH": "outbound frio, primeiro contato",
    "INTEL_SEED": "prospecção a partir de informação pública",
    "INBOUND_LIVE": "inbound — o lead levantou a mão",
    "PARTNER": "indicação de parceiro",
    "OTHER": "outro canal",
}

# engagement.type é dado inerte: nada ramifica por ele (nem prompt, nem código) —
# por isso NÃO existe lista fechada de valores, seria enum sem função. Mas ele é
# interpolado no prompt, então passa por um filtro de FORMA, não de significado:
# token curto e maiúsculo passa como está, qualquer outra coisa vira UNKNOWN, e
# nunca é fatal. O texto livre continua em engagement.detail.
_ENGAGEMENT_TYPE_RE = re.compile(r"^[A-Z0-9_]{1,32}$")
ENGAGEMENT_TYPE_UNKNOWN = "UNKNOWN"


# -- validation --------------------------------------------------------------
def _is_str(v) -> bool:
    return isinstance(v, str) and v.strip() != ""


def _req_str(obj: dict, key: str, where: str) -> str:
    """Empty/whitespace-only counts as missing: fail-closed, no silent blanks."""
    if not _is_str(obj.get(key)):
        return f"campo obrigatório ausente ou vazio: {where}"
    return ""


def _opt_str_or_null(obj: dict, key: str, where: str) -> str:
    v = obj.get(key)
    if v is None or isinstance(v, str):
        return ""
    return f"{where} deve ser string ou null"


def _opt_str_list(doc: dict, key: str) -> str:
    v = doc.get(key)
    if v is None:
        return ""
    if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
        return f"{key} deve ser uma lista de strings"
    return ""


def _opt_dict(doc: dict, key: str) -> str:
    v = doc.get(key)
    if v is not None and not isinstance(v, dict):
        return f"{key} deve ser um objeto"
    return ""


def looks_like_cnpj(value) -> bool:
    """True only for a 14-digit or formatted CNPJ. Slugs/UUIDs/refs are not CNPJ."""
    if not isinstance(value, str):
        return False
    s = value.strip()
    return bool(_CNPJ_FMT_RE.match(s) or _CNPJ_DIGITS_RE.match(s))


def is_collection(doc) -> bool:
    """True for a hand-raiser index/export, including the live Warmbly collision.

    Warmbly GET /confenge/sales-context tags the *collection* as SCHEMA_ID.
    Shape (items[] + export metadata), not the tag, is what makes it an index.
    """
    if not isinstance(doc, dict):
        return False
    if doc.get("schema") == SCHEMA_EXPORT:
        return True
    items = doc.get("items")
    if not isinstance(items, list):
        return False
    return any(k in doc for k in ("total", "by_engine", "generated_at", "organization_id", "unattributed"))


def _validate(doc) -> str:
    """Returns "" when the document is usable, or a one-line reason why not."""
    if not isinstance(doc, dict):
        return "documento não é um objeto JSON"

    if is_collection(doc):
        tag = doc.get("schema")
        return (f"coleção recusada como dossiê (schema {tag!r}); "
                f"índice é {SCHEMA_EXPORT}, dossiê do copiloto é {SCHEMA_ID}")

    if doc.get("schema") != SCHEMA_ID:
        return f"schema deve ser exatamente {SCHEMA_ID!r} (veio {doc.get('schema')!r})"

    # Acquisition taxonomy belongs to the upstream authority.  MeetCFG only
    # checks transport shape; absent/UNKNOWN is a valid partial context and a
    # future channel must not require a consumer release.
    for key in ("acquisition_channel", "conversation_channel", "context_status",
                "situation", "objective", "advancement_criterion", "next_state"):
        err = _opt_str_or_null(doc, key, key)
        if err:
            return err

    for key in ("company", "intent", "offer", "engagement", "claim_safety"):
        err = _opt_dict(doc, key)
        if err:
            return err

    company = doc.get("company")
    if isinstance(company, dict):
        err = _opt_str_or_null(company, "name", "company.name") or \
            _opt_str_or_null(company, "cnpj", "company.cnpj")
        if err:
            return err

    intent = doc.get("intent")
    if isinstance(intent, dict):
        err = _opt_str_or_null(intent, "kind", "intent.kind") or \
            _opt_str_or_null(intent, "reply_reason", "intent.reply_reason")
        if err:
            return err

    offer = doc.get("offer")
    if isinstance(offer, dict):
        for key in ("id", "family", "next_state", "current", "price_band"):
            err = _opt_str_or_null(offer, key, f"offer.{key}")
            if err:
                return err

    for key in ("source_as_of", "provenance"):
        err = _opt_str_or_null(doc, key, key)
        if err:
            return err

    engagement = doc.get("engagement")
    if isinstance(engagement, dict):
        # engagement.type is descriptive, not behavioural: só o tipo Python é
        # exigido aqui — o conteúdo é saneado por forma em engagement_type(),
        # nunca rejeitado. acquisition_channel também é opaco ao consumer.
        err = _opt_str_or_null(engagement, "type", "engagement.type") or \
            _opt_str_or_null(engagement, "detail", "engagement.detail")
        if err:
            return err

    for key in ("public_facts", "opportunities", "evidence", "limits"):
        err = _opt_str_list(doc, key)
        if err:
            return err

    claim_safety = doc.get("claim_safety")
    if isinstance(claim_safety, dict):
        for key in ("never_assert", "safe_to_reference"):
            err = _opt_str_list(claim_safety, key)
            if err:
                return f"claim_safety.{err}"

    citable = doc.get("citable_facts")
    if citable is not None:
        if not isinstance(citable, list):
            return "citable_facts deve ser uma lista"
        for i, fact in enumerate(citable):
            if not isinstance(fact, dict):
                return f"citable_facts[{i}] deve ser um objeto"
            for key in ("claim", "source"):
                err = _req_str(fact, key, f"citable_facts[{i}].{key}")
                if err:
                    return err
            err = _opt_str_or_null(
                fact, "source_as_of", f"citable_facts[{i}].source_as_of"
            )
            if err:
                return err

    for key in ("constraints", "conflicts"):
        err = _opt_str_list(doc, key)
        if err:
            return err
    roles = doc.get("participant_roles")
    if roles is not None:
        if not isinstance(roles, list):
            return "participant_roles deve ser uma lista"
        for i, role in enumerate(roles):
            if isinstance(role, str):
                continue
            if not isinstance(role, dict):
                return f"participant_roles[{i}] deve ser string ou objeto"
            for key in ("name", "role", "papel"):
                err = _opt_str_or_null(role, key, f"participant_roles[{i}].{key}")
                if err:
                    return err
    gaps = doc.get("gaps")
    if gaps is not None:
        if not isinstance(gaps, list):
            return "gaps deve ser uma lista"
        for i, gap in enumerate(gaps):
            if not isinstance(gap, dict):
                return f"gaps[{i}] deve ser um objeto"
            for key in ("id", "question"):
                err = _req_str(gap, key, f"gaps[{i}].{key}")
                if err:
                    return err
            for key in ("status", "answer", "repeat_reason"):
                err = _opt_str_or_null(gap, key, f"gaps[{i}].{key}")
                if err:
                    return err
            if gap.get("blocking") is not None and not isinstance(gap.get("blocking"), bool):
                return f"gaps[{i}].blocking deve ser booleano"

    touchpoints = doc.get("touchpoints")
    if touchpoints is not None:
        if not isinstance(touchpoints, list):
            return "touchpoints deve ser uma lista"
        for i, tp in enumerate(touchpoints):
            if not isinstance(tp, dict):
                return f"touchpoints[{i}] deve ser um objeto"
            err = _req_str(tp, "summary", f"touchpoints[{i}].summary")
            if err:
                return err
            for key in ("at", "channel", "delivered"):
                err = _opt_str_or_null(tp, key, f"touchpoints[{i}].{key}")
                if err:
                    return err

    return ""


def load_sales_context_v1(path: str | Path) -> tuple[dict | None, str]:
    """Load + validate a CONFENGE_SALES_CONTEXT/1.0 document.

    Returns (context, "") only when the whole document is valid; otherwise
    (None, reason). Never raises and never returns a partial object — the caller
    must not use a document that failed, not even the fields that parsed fine.
    """
    p = Path(path)
    try:
        raw = p.read_text(encoding="utf-8")
    except OSError as e:
        return _reject(f"contexto estruturado ilegível ({p}): {e}")
    try:
        doc = json.loads(raw)
    except json.JSONDecodeError as e:
        return _reject(f"contexto estruturado com JSON inválido ({p}): {e}")
    reason = _validate(doc)
    if reason:
        return _reject(f"contexto estruturado inválido ({p}): {reason}")
    if not never_assert_list(doc):
        # Guarda ADVISORY, não bloqueio: um dossiê sem nenhum limite declarado é
        # válido e roda normalmente, mas quem lê o log precisa saber que a lista
        # NÃO AFIRME saiu vazia — ninguém revisou os limites deste lead. O que
        # NÃO se faz aqui é inventar limites nem anunciar a ausência ao modelo:
        # "não há limites declarados" no prompt é um passo de inferência de
        # "então posso afirmar o que quiser".
        log.info("contexto estruturado PARCIAL (%s): sem limits nem "
                 "claim_safety.never_assert — nenhum limite de afirmação "
                 "declarado para este lead", p)
    return doc, ""


def _reject(reason: str) -> tuple[None, str]:
    log.warning("%s", reason)
    return None, reason


# -- rendering ---------------------------------------------------------------
def _strs(value) -> list[str]:
    return [s.strip() for s in value if isinstance(s, str) and s.strip()] \
        if isinstance(value, list) else []


def _section(parts: list[str], title: str, items: list[str]) -> None:
    """Empty sections are dropped: prompt budget, and an empty header reads to
    the model like a fact it should fill in."""
    if not items:
        return
    parts.append(title)
    parts.extend(f"- {it}" for it in items)


def engagement_type(ctx: dict) -> str:
    """Sanitised `engagement.type`, "" when the field is absent/null/blank.

    Nunca rejeita o documento: um valor estranho aqui não invalida um dossiê
    inteiro, só perde a forma de token e vira UNKNOWN. Diferente de
    `acquisition_channel`, que permanece dado opaco recebido da autoridade.
    """
    engagement = ctx.get("engagement")
    if not isinstance(engagement, dict):
        return ""
    raw = engagement.get("type")
    if not _is_str(raw):
        return ""
    token = raw.strip().upper()
    return token if _ENGAGEMENT_TYPE_RE.match(token) else ENGAGEMENT_TYPE_UNKNOWN


def never_assert_list(ctx: dict) -> list[str]:
    """`limits` and `claim_safety.never_assert` are the same boundary; two
    separate lists would let the model treat one of them as the softer one."""
    claim_safety = ctx.get("claim_safety")
    raw = _strs(ctx.get("limits"))
    if isinstance(claim_safety, dict):
        raw += _strs(claim_safety.get("never_assert"))
    seen: list[str] = []
    for item in raw:
        if item not in seen:
            seen.append(item)
    return seen


def citable_facts(ctx: dict) -> list[dict]:
    """Return only claims with an explicit source; unsourced claims stay silent."""
    status = str(ctx.get("context_status") or "").strip().upper()
    if status != "CURRENT":
        return []
    out: list[dict] = []
    # The new claim contract is usable only when freshness/consistency is
    # explicitly CURRENT.  Legacy dossiers below retain their provenance seam.
    structured = ctx.get("citable_facts") or []
    for fact in structured:
        if not isinstance(fact, dict):
            continue
        claim = fact.get("claim")
        source = fact.get("source")
        if not _is_str(claim) or not _is_str(source):
            continue
        out.append({
            "claim": claim.strip(),
            "source": source.strip(),
            "source_as_of": (
                fact.get("source_as_of").strip()
                if _is_str(fact.get("source_as_of")) else "UNKNOWN"
            ),
        })
    # Backward-compatible legacy facts are usable only with the dossier's
    # provenance.  A partial dossier without it fails closed at claim level.
    provenance = ctx.get("provenance")
    if _is_str(provenance):
        as_of = ctx.get("source_as_of")
        seen = {f["claim"] for f in out}
        legacy = _strs(ctx.get("public_facts"))
        safety = ctx.get("claim_safety")
        if isinstance(safety, dict):
            legacy += _strs(safety.get("safe_to_reference"))
        for claim in legacy:
            if claim not in seen:
                seen.add(claim)
                out.append({
                    "claim": claim,
                    "source": provenance.strip(),
                    "source_as_of": as_of.strip() if _is_str(as_of) else "UNKNOWN",
                })
    return out


def render_for_prompt(ctx: dict) -> str:
    """Render a validated context into the plain-text block the prompt expects.

    Compact on purpose — this is prepended to every Codex call.  Citations carry
    their source/provenance; raw `evidence` stays an audit trail, never a claim.
    """
    parts: list[str] = []

    channel = ctx.get("acquisition_channel") or "UNKNOWN"
    label = CHANNEL_LABELS.get(channel, "")
    parts.append(f"Canal de aquisição: {channel}" + (f" ({label})" if label else ""))

    company = ctx.get("company") or {}
    cnpj = company.get("cnpj")
    line = f"Empresa: {company.get('name') or 'UNKNOWN'}"
    # company_ref stuffed into cnpj is not a CNPJ — never present it as one.
    if looks_like_cnpj(cnpj):
        line += f" (CNPJ {cnpj.strip()})"
    parts.append(line)

    intent = ctx.get("intent") or {}
    line = f"Por que a conversa existe: {intent.get('kind') or 'UNKNOWN'}"
    if _is_str(intent.get("reply_reason")):
        line += f" — {intent['reply_reason'].strip()}"
    parts.append(line)

    if ctx.get("inbound_only") is True:
        parts.append("Identidade inbound-only: não habilita abordagem outbound, SMTP nem follow-up.")

    engagement = ctx.get("engagement") or {}
    bits = [t for t in [engagement_type(ctx)] if t]
    if _is_str(engagement.get("detail")):
        bits.append(engagement["detail"].strip())
    if bits:
        parts.append("Engajamento: " + " — ".join(bits))

    conversation_channel = ctx.get("conversation_channel") or "UNKNOWN"
    parts.append(f"Canal da conversa: {conversation_channel}")
    status = str(ctx.get("context_status") or "UNKNOWN").strip().upper()
    parts.append(f"Estado do contexto: {status}")
    if status != "CURRENT":
        parts.append(
            "Contexto não confiável para afirmações: confirme antes de citar; "
            "não avance por inferência."
        )

    facts = [
        f"{fact['claim']} [fonte: {fact['source']}; em: {fact['source_as_of']}]"
        for fact in citable_facts(ctx)
    ]
    _section(parts, "Fatos citáveis com fonte:", facts)
    if _is_str(ctx.get("provenance")):
        opportunities = [
            f"{item} [hipótese recebida de: {ctx['provenance'].strip()}]"
            for item in _strs(ctx.get("opportunities"))
        ]
        _section(parts, "Hipóteses/oportunidades a validar (não afirmar como fato):", opportunities)

    touchpoints = ctx.get("touchpoints")
    if isinstance(touchpoints, list):
        rendered = []
        for tp in touchpoints:
            if not isinstance(tp, dict) or not _is_str(tp.get("summary")):
                continue
            head = " · ".join(b.strip() for b in (tp.get("at"), tp.get("channel"))
                              if _is_str(b))
            item = f"{head} — {tp['summary'].strip()}" if head else tp["summary"].strip()
            if _is_str(tp.get("delivered")):
                item += f" [JÁ RECEBEU: {tp['delivered'].strip()}]"
            rendered.append(item)
        _section(parts, "Já falado/entregue ao lead (não repita venda introdutória):",
                 rendered)

    _section(parts, "NÃO AFIRME (limite duro, nem como hipótese):", never_assert_list(ctx))
    _section(parts, "O que NÃO sabemos (permanece ausente; não preencha):",
             _strs(ctx.get("unknown")))

    from .conversion import meeting_plan_of, render_plan_for_prompt
    plan, plan_reason = meeting_plan_of(ctx)
    if plan is not None or plan_reason:
        parts.append("Plano de reunião (mesma versão do briefing; estágio não se altera sozinho):")
        parts.append(render_plan_for_prompt(
            plan, answered=_strs(ctx.get("answered_questions")), reason=plan_reason,
        ))

    offer = ctx.get("offer") or {}
    if _is_str(offer.get("id")):
        parts.append(f"Oferta (id opaco): {offer['id'].strip()}")
    if _is_str(offer.get("family")):
        parts.append(f"Família (opaca): {offer['family'].strip()}")
    if _is_str(offer.get("current")):
        parts.append(f"Oferta em jogo: {offer['current'].strip()}")
    # Price is never promoted from context metadata into live guidance.  If it
    # matters, the authority must express the verifiable commitment and its
    # gates in meeting_plan.
    if plan is None:
        parts.append(f"Próximo passo alvo: {offer.get('next_state') or 'UNKNOWN'}")

    return "\n".join(parts)
