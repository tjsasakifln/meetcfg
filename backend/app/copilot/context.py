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

# Hard enum: it steers copilot behaviour (um inbound que levantou a mão não
# recebe a mesma abordagem de um outbound frio), so an unrecognised value is an
# invalid document — never silently coerced to OTHER.
CHANNELS = (
    "OUTBOUND_FIRST_TOUCH",
    "INTEL_SEED",
    "INBOUND_LIVE",
    "PARTNER",
    "OTHER",
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


def _validate(doc) -> str:
    """Returns "" when the document is usable, or a one-line reason why not."""
    if not isinstance(doc, dict):
        return "documento não é um objeto JSON"

    if doc.get("schema") != SCHEMA_ID:
        return f"schema deve ser exatamente {SCHEMA_ID!r} (veio {doc.get('schema')!r})"

    channel = doc.get("acquisition_channel")
    if channel not in CHANNELS:
        return (f"acquisition_channel inválido ({channel!r}); "
                f"use um de: {', '.join(CHANNELS)}")

    for key in ("company", "intent", "offer", "engagement", "claim_safety"):
        err = _opt_dict(doc, key)
        if err:
            return err

    company = doc.get("company")
    if not isinstance(company, dict):
        return "campo obrigatório ausente: company"
    err = _req_str(company, "name", "company.name") or \
        _opt_str_or_null(company, "cnpj", "company.cnpj")
    if err:
        return err

    intent = doc.get("intent")
    if not isinstance(intent, dict):
        return "campo obrigatório ausente: intent"
    err = _req_str(intent, "kind", "intent.kind") or \
        _opt_str_or_null(intent, "reply_reason", "intent.reply_reason")
    if err:
        return err

    offer = doc.get("offer")
    if not isinstance(offer, dict):
        return "campo obrigatório ausente: offer"
    err = _req_str(offer, "next_state", "offer.next_state") or \
        _opt_str_or_null(offer, "current", "offer.current")
    if err:
        return err

    err = _req_str(doc, "source_as_of", "source_as_of") or \
        _req_str(doc, "provenance", "provenance")
    if err:
        return err

    engagement = doc.get("engagement")
    if isinstance(engagement, dict):
        # engagement.type is descriptive, not behavioural: só o tipo Python é
        # exigido aqui — o conteúdo é saneado por forma em engagement_type(),
        # nunca rejeitado. acquisition_channel é o único enum duro.
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
    `acquisition_channel`, que é enum duro porque de fato orienta a abordagem.
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


def render_for_prompt(ctx: dict) -> str:
    """Render a validated context into the plain-text block the prompt expects.

    Compact on purpose — this is prepended to every Codex call. `evidence`,
    `provenance` e `source_as_of` ficam de fora: são rastro de auditoria para o
    briefing humano, não material novo para o modelo afirmar.
    """
    parts: list[str] = []

    channel = ctx.get("acquisition_channel", "")
    label = CHANNEL_LABELS.get(channel, "")
    parts.append(f"Canal de aquisição: {channel}" + (f" ({label})" if label else ""))

    company = ctx.get("company") or {}
    cnpj = company.get("cnpj")
    line = f"Empresa: {company.get('name', '')}"
    if _is_str(cnpj):
        line += f" (CNPJ {cnpj.strip()})"
    parts.append(line)

    intent = ctx.get("intent") or {}
    line = f"Por que a conversa existe: {intent.get('kind', '')}"
    if _is_str(intent.get("reply_reason")):
        line += f" — {intent['reply_reason'].strip()}"
    parts.append(line)

    engagement = ctx.get("engagement") or {}
    bits = [t for t in [engagement_type(ctx)] if t]
    if _is_str(engagement.get("detail")):
        bits.append(engagement["detail"].strip())
    if bits:
        parts.append("Engajamento: " + " — ".join(bits))

    facts = _strs(ctx.get("public_facts"))
    claim_safety = ctx.get("claim_safety")
    if isinstance(claim_safety, dict):
        facts += [s for s in _strs(claim_safety.get("safe_to_reference")) if s not in facts]
    _section(parts, "Fatos públicos que podem ser citados:", facts)
    _section(parts, "Oportunidades mapeadas:", _strs(ctx.get("opportunities")))

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

    offer = ctx.get("offer") or {}
    if _is_str(offer.get("current")):
        parts.append(f"Oferta em jogo: {offer['current'].strip()}")
    parts.append(f"Próximo passo alvo: {offer.get('next_state', '')}")

    return "\n".join(parts)
