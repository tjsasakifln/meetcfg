"""Consume an accepted Warmbly hand-raiser into one Meetcfg conversation.

Meetcfg is consumer/view only: it does not create CRM, lead, intent, outbound
eligibility, or a second commercial source of truth. The copilot still reads
exactly one dossier semantics — CONFENGE_SALES_CONTEXT/1.0. A collection/index
is a distinct contract (CONFENGE_SALES_CONTEXT_EXPORT/1.0) and is never the
copilot input, even when the producer mis-tags the export as the dossier schema.

Stdlib only. Pure functions over dicts so selftests can drive consume/validate/
session-bind without HTTP, whisper, or Codex.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .context import (
    CHANNELS,
    SCHEMA_EXPORT,
    SCHEMA_ID,
    _validate,
    is_collection,
    looks_like_cnpj,
    render_for_prompt,
)

log = logging.getLogger("meetcfg.handraiser")

# Consume envelope (item + optional admission). Distinct from the dossier and
# from the collection so schema_version cannot collide.
SCHEMA_ITEM = "CONFENGE_HANDRAISER_ITEM/1.0"
SCHEMA_ADMISSION = "net-new-inbound-handraiser-admission.v1"

# Warmbly origin/main producer fingerprint. Re-validate before merge; #47 is
# still open and may land a different item/export shape.
# SHA cc11a9ab22d54e08ceb24efc9b555e0ac9c25b23
# file internal/app/confenge/sales_context.go
# GET /confenge/sales-context → {data: SalesContextExport}
# Export.schema is SalesContextSchemaV1 = CONFENGE_SALES_CONTEXT/1.0 (COLLISION:
# that tag is the individual dossier here; collection is SCHEMA_EXPORT).
# Native item has no CNPJ and no inbound_only.
WARMBLY_PRODUCER_SHA = "cc11a9ab22d54e08ceb24efc9b555e0ac9c25b23"
WARMBLY_PRODUCER_FILE = "internal/app/confenge/sales_context.go"
WARMBLY_ITEM_KEYS = (
    "action_id", "account_id", "acquisition_channel", "company_ref",
    "company_name", "person_name", "candidate_id", "opportunity_id",
    "intent_reason", "reply_reason", "conversation_started", "facts",
    "touchpoints", "next_action_type", "next_action_at", "state", "created_at",
)
WARMBLY_EXPORT_KEYS = (
    "schema", "organization_id", "generated_at", "total", "by_engine",
    "unattributed", "items",
)

UNKNOWN = "UNKNOWN"
_NIL_UUID = "00000000-0000-0000-0000-000000000000"

# Governance closed states. Anything else with a decision field fails closed.
DECISION_ACCEPTED = "ACCEPTED"
DECISION_REJECTED = "REJECTED_WITH_REASON"
DECISION_UNKNOWN = "UNKNOWN"

# Visible reason codes (never a silent drop).
CONSUMER_DISABLED = "CONSUMER_DISABLED"
SCHEMA_MISMATCH_COLLECTION = "SCHEMA_MISMATCH_COLLECTION"
SCHEMA_MISMATCH = "SCHEMA_MISMATCH"
REJECTED_WITH_REASON = "REJECTED_WITH_REASON"
UNKNOWN_OUTCOME = "UNKNOWN_OUTCOME"
FRESHNESS_INVALID = "FRESHNESS_INVALID"
FRESHNESS_STALE = "FRESHNESS_STALE"
MALFORMED = "MALFORMED"
OVERSIZED = "OVERSIZED"
MISSING_IDENTITY = "MISSING_IDENTITY"
DOSSIER_INVALID = "DOSSIER_INVALID"
NOT_FOUND = "NOT_FOUND"

MAX_PAYLOAD_BYTES = 256_000
SESSION_PREFIX = "hr:"

# Warmbly engine lane → Meetcfg acquisition_channel. Unknown lanes become OTHER
# rather than a guessed commercial approach. Meetcfg enums stay intact.
_LANE_TO_CHANNEL = {
    "outbound_first_touch": "OUTBOUND_FIRST_TOUCH",
    "intel_seed": "INTEL_SEED",
    "intel_watch": "OTHER",
    "confenge_web": "INBOUND_LIVE",
    "unattributed": "OTHER",
    "": "OTHER",
    "net_new_inbound": "INBOUND_LIVE",
    "confenge_web_intent": "INBOUND_LIVE",
    "NET_NEW_INBOUND": "INBOUND_LIVE",
    "CONFENGE_WEB": "INBOUND_LIVE",
    "INBOUND_LIVE": "INBOUND_LIVE",
    "OUTBOUND_FIRST_TOUCH": "OUTBOUND_FIRST_TOUCH",
    "INTEL_SEED": "INTEL_SEED",
    "PARTNER": "PARTNER",
    "OTHER": "OTHER",
}

_INVENTED_COMMERCIAL = ("cnpj", "cargo", "decisor", "fit", "chance", "prazo", "prova")

_AS_OF_DATE = re.compile(r"^(\d{4}-\d{2}-\d{2})")


def _str(v) -> str:
    return v.strip() if isinstance(v, str) and v.strip() else ""


def _unwrap(payload):
    """Warmbly HTTP wraps the export as {data: ...}."""
    if isinstance(payload, dict) and "data" in payload and isinstance(payload["data"], (dict, list)):
        inner = payload["data"]
        # A dossier/item that happens to have a "data" field must not be unwrapped
        # if it already looks like a consume/dossier document.
        if payload.get("schema") in (SCHEMA_ID, SCHEMA_ITEM, SCHEMA_EXPORT, SCHEMA_ADMISSION):
            return payload
        return inner
    return payload


def classify_payload(payload) -> str:
    """One of: collection, dossier, native_item, admission, wrap, malformed."""
    if not isinstance(payload, dict):
        return "malformed"
    doc = _unwrap(payload)
    if not isinstance(doc, dict):
        return "malformed"
    if is_collection(doc):
        return "collection"
    schema = doc.get("schema")
    if schema == SCHEMA_EXPORT:
        return "collection"
    if schema == SCHEMA_ITEM:
        return "wrap"
    if schema == SCHEMA_ADMISSION or schema == "net-new-inbound-handraiser-admission.v1":
        return "admission"
    if schema == SCHEMA_ID:
        return "dossier"
    if schema not in (None, ""):
        return "schema_mismatch"
    # Native Warmbly SalesContextItem: action_id + no dossier schema.
    if doc.get("action_id") or doc.get("handraiser_id"):
        if any(k in doc for k in ("company_name", "company_ref", "account_id",
                                  "intent_reason", "facts", "acquisition_channel")):
            return "native_item"
        return "native_item"
    if isinstance(doc.get("admission"), dict) or isinstance(doc.get("item"), dict):
        return "wrap"
    return "malformed"


def session_id_for(handraiser_id: str) -> str:
    return f"{SESSION_PREFIX}{handraiser_id}"


def _closed_state(doc: dict) -> str | None:
    for src in (doc, doc.get("admission") if isinstance(doc.get("admission"), dict) else None):
        if not isinstance(src, dict):
            continue
        for key in ("decision", "outcome"):
            v = src.get(key)
            if isinstance(v, str) and v.strip():
                return v.strip()
    return None


def _parse_instant(raw, *, default=None) -> datetime | None:
    if raw is None:
        return default
    if isinstance(raw, datetime):
        dt = raw
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    s = _str(raw)
    if not s:
        return default
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        pass
    m = _AS_OF_DATE.match(s)
    if m:
        try:
            dt = datetime.strptime(m.group(1), "%Y-%m-%d").replace(tzinfo=timezone.utc)
            return dt
        except ValueError:
            return None
    return None


def _date_only(dt: datetime | None) -> str:
    return dt.date().isoformat() if dt else ""


def map_channel(lane: str) -> str:
    if lane in CHANNELS:
        return lane
    mapped = _LANE_TO_CHANNEL.get(lane) or _LANE_TO_CHANNEL.get(lane.lower(), "")
    return mapped if mapped in CHANNELS else "OTHER"


def _sanitize_cnpj(raw) -> tuple[str | None, str]:
    """Return (cnpj_or_None, leftover_identity_ref). Never treat a slug as CNPJ."""
    s = _str(raw) or None
    if s is None:
        return None, ""
    if looks_like_cnpj(s):
        return s, ""
    return None, s


def _map_touchpoints(raw) -> list[dict]:
    if not isinstance(raw, list):
        return []
    out: list[dict] = []
    for tp in raw:
        if not isinstance(tp, dict):
            continue
        summary = _str(tp.get("summary")) or _str(tp.get("subject")) or _str(tp.get("state"))
        if not summary:
            continue
        item = {"summary": summary}
        at = tp.get("at") or tp.get("sent_at")
        if _str(at) or isinstance(at, datetime):
            parsed = _parse_instant(at)
            item["at"] = _date_only(parsed) if parsed else _str(at)
        if _str(tp.get("channel")):
            item["channel"] = _str(tp.get("channel"))
        if tp.get("delivered") is None or isinstance(tp.get("delivered"), str):
            if _str(tp.get("delivered")):
                item["delivered"] = _str(tp.get("delivered"))
        out.append(item)
    return out


def _missing_commercial(dossier: dict) -> list[str]:
    """Projection of what the producer did not send. Does not invent values."""
    missing: list[str] = []
    company = dossier.get("company") if isinstance(dossier.get("company"), dict) else {}
    name = _str(company.get("name"))
    if not name or name == UNKNOWN:
        missing.append("empresa")
    if not looks_like_cnpj(company.get("cnpj")):
        missing.append("CNPJ")
    missing.extend(["cargo", "decisor", "fit", "chance", "prazo", "prova"])
    if dossier.get("inbound_only") is True:
        missing.append("habilitação outbound")
    seen: list[str] = []
    extra = dossier.get("unknown")
    for item in (list(extra) if isinstance(extra, list) else []) + missing:
        if isinstance(item, str) and item.strip() and item.strip() not in seen:
            seen.append(item.strip())
    return seen


def map_to_dossier(
    item: dict,
    *,
    admission: dict | None = None,
    extras: dict | None = None,
) -> dict:
    """Native Warmbly item and/or admission → CONFENGE_SALES_CONTEXT/1.0.

    Missing commercial fields stay absent or UNKNOWN. company_ref is never CNPJ.
    inbound_only is copied when the producer sent it; it is never invented as
    outbound eligibility.
    """
    admission = admission if isinstance(admission, dict) else {}
    extras = extras if isinstance(extras, dict) else {}
    src = item if isinstance(item, dict) else {}

    handraiser_id = (
        _str(extras.get("handraiser_id"))
        or _str(src.get("handraiser_id"))
        or _str(src.get("action_id"))
        or _str(admission.get("logical_admission_id"))
        or _str(admission.get("receipt_id"))
        or _str(admission.get("idempotency_key"))
    )

    lane = (
        _str(extras.get("lane"))
        or _str(src.get("lane"))
        or _str(src.get("acquisition_channel"))
        or _str(admission.get("acquisition_lane"))
        or _str(admission.get("origin"))
    )
    origin = (
        _str(extras.get("origin"))
        or _str(admission.get("origin"))
        or _str(src.get("origin"))
        or lane
    )
    channel = map_channel(lane)

    company_src = src.get("company") if isinstance(src.get("company"), dict) else {}
    company_name = (
        _str(company_src.get("name"))
        or _str(src.get("company_name"))
        or _str(extras.get("company_name"))
    ) or UNKNOWN
    cnpj, leftover_ref = _sanitize_cnpj(company_src.get("cnpj"))
    identity_ref = (
        _str(company_src.get("identity_ref"))
        or _str(src.get("company_ref"))
        or leftover_ref
        or _str(admission.get("subject_ref"))
        or _str(admission.get("account_ref"))
    )
    account_id = (
        _str(src.get("account_id"))
        or _str(extras.get("account_id"))
        or _str(admission.get("account_ref"))
        or None
    )
    if account_id in ("", _NIL_UUID):
        account_id = None

    intent_src = src.get("intent") if isinstance(src.get("intent"), dict) else {}
    intent_kind = (
        _str(intent_src.get("kind"))
        or _str(src.get("intent_reason"))
        or _str(admission.get("intent_kind"))
        or UNKNOWN
    )
    reply_reason = intent_src.get("reply_reason")
    if reply_reason is None:
        reply_reason = src.get("reply_reason")
    reply_reason = _str(reply_reason) or None

    facts = src.get("facts") if isinstance(src.get("facts"), dict) else {}
    public_facts = src.get("public_facts")
    if not isinstance(public_facts, list):
        public_facts = []
        for key in ("why_now", "factual_hook"):
            if _str(facts.get(key)):
                public_facts.append(_str(facts.get(key)))
    public_facts = [x for x in public_facts if isinstance(x, str)]

    opportunities = src.get("opportunities")
    if not isinstance(opportunities, list):
        opportunities = []
    opportunities = [x for x in opportunities if isinstance(x, str)]

    evidence = src.get("evidence")
    if not isinstance(evidence, list):
        evidence = facts.get("evidence_ids") if isinstance(facts.get("evidence_ids"), list) else []
    evidence = [x for x in evidence if isinstance(x, str)]

    limits = src.get("limits")
    if not isinstance(limits, list):
        limits = facts.get("stated_limits") if isinstance(facts.get("stated_limits"), list) else []
    limits = [x for x in limits if isinstance(x, str)]

    offer_src = src.get("offer") if isinstance(src.get("offer"), dict) else {}
    next_state = (
        _str(offer_src.get("next_state"))
        or _str(src.get("next_state"))
        or _str(src.get("next_action_type"))
        or UNKNOWN
    )
    current_offer = offer_src.get("current")
    if current_offer is None:
        current_offer = None
    elif not isinstance(current_offer, str):
        current_offer = None

    situacao = (
        _str(extras.get("situacao"))
        or _str(src.get("situacao"))
        or _str(src.get("state"))
        or UNKNOWN
    )
    receipt = (
        _str(extras.get("receipt"))
        or _str(src.get("receipt"))
        or _str(admission.get("receipt_id"))
        or ""
    )
    # Producer decision only. Native Warmbly items have none — do not invent ACCEPTED.
    outcome = _closed_state(extras) or _closed_state(admission) or _closed_state(src) or UNKNOWN

    freshness_src = None
    for cand in (extras.get("freshness"), src.get("freshness"), admission.get("freshness")):
        if isinstance(cand, dict):
            freshness_src = cand
            break
    source_as_of = (
        _str(src.get("source_as_of"))
        or _str(extras.get("source_as_of"))
        or _str((freshness_src or {}).get("as_of"))
        or _date_only(_parse_instant(src.get("created_at")))
        or _date_only(_parse_instant(admission.get("evaluated_at")))
    )
    provenance = (
        _str(src.get("provenance"))
        or ("warmbly_sales_context_item" if src.get("action_id") else "")
        or ("governance_admission" if admission else "")
        or "handraiser_consumer"
    )

    inbound_only = None
    for cand in (extras, admission, src, admission.get("metrics") if isinstance(admission.get("metrics"), dict) else None):
        if isinstance(cand, dict) and "inbound_only" in cand:
            inbound_only = cand.get("inbound_only")
            break
    if inbound_only is not None:
        inbound_only = bool(inbound_only)

    engagement = src.get("engagement") if isinstance(src.get("engagement"), dict) else None
    claim_safety = src.get("claim_safety") if isinstance(src.get("claim_safety"), dict) else None
    touchpoints = src.get("touchpoints")
    mapped_tps = _map_touchpoints(touchpoints) if isinstance(touchpoints, list) else []

    why_now = _str(facts.get("why_now")) or _str(intent_kind)

    dossier: dict[str, Any] = {
        "schema": SCHEMA_ID,
        "handraiser_id": handraiser_id,
        "origin": origin or UNKNOWN,
        "lane": lane or UNKNOWN,
        "acquisition_channel": channel,
        "company": {
            "name": company_name,
            "cnpj": cnpj,
        },
        "intent": {
            "kind": intent_kind,
            "reply_reason": reply_reason,
        },
        "offer": {
            "current": current_offer,
            "next_state": next_state,
        },
        "source_as_of": source_as_of or UNKNOWN,
        "provenance": provenance,
        "public_facts": public_facts,
        "opportunities": opportunities,
        "evidence": evidence,
        "limits": limits,
        "touchpoints": mapped_tps,
        "situacao": situacao,
        "receipt": receipt or None,
        "outcome": outcome,
        "why_now": why_now,
        "next_state": next_state,
    }
    if identity_ref:
        dossier["company"]["identity_ref"] = identity_ref
        dossier["identity_ref"] = identity_ref
    if account_id:
        dossier["account_id"] = account_id
    if inbound_only is not None:
        dossier["inbound_only"] = inbound_only
    if freshness_src is not None:
        dossier["freshness"] = {
            k: freshness_src[k]
            for k in ("as_of", "max_age_seconds")
            if k in freshness_src
        }
    opportunity_ref = _str(src.get("opportunity_id"))
    if opportunity_ref:
        dossier["opportunity_ref"] = opportunity_ref
    if engagement:
        dossier["engagement"] = engagement
    if claim_safety:
        dossier["claim_safety"] = claim_safety
    if src.get("price_band") or (isinstance(offer_src, dict) and offer_src.get("price_band")):
        dossier["offer"]["price_band"] = _str(src.get("price_band") or offer_src.get("price_band")) or None

    dossier["unknown"] = _missing_commercial(dossier)
    return dossier


def _check_freshness(dossier: dict, *, now: datetime, extra_max_age_s: float = 0) -> str:
    """Return a reason code or ""."""
    freshness = dossier.get("freshness") if isinstance(dossier.get("freshness"), dict) else {}
    as_of = _parse_instant(freshness.get("as_of")) or _parse_instant(dossier.get("source_as_of"))
    if dossier.get("source_as_of") in (None, "", UNKNOWN) and not as_of:
        return FRESHNESS_INVALID
    if dossier.get("source_as_of") and dossier.get("source_as_of") != UNKNOWN and as_of is None:
        return FRESHNESS_INVALID
    if as_of is None:
        return ""
    if as_of - now > timedelta(days=1):
        return FRESHNESS_INVALID
    max_age = freshness.get("max_age_seconds")
    ages: list[float] = []
    if max_age is not None:
        try:
            ages.append(float(max_age))
        except (TypeError, ValueError):
            return FRESHNESS_INVALID
    if extra_max_age_s and extra_max_age_s > 0:
        ages.append(float(extra_max_age_s))
    if ages:
        age = (now - as_of).total_seconds()
        if age > max(ages):
            return FRESHNESS_STALE
    return ""


def render_conversation_layer(dossier: dict) -> dict:
    """Eight fields for the conversation screen. Missing stays UNKNOWN."""
    company = dossier.get("company") if isinstance(dossier.get("company"), dict) else {}
    intent = dossier.get("intent") if isinstance(dossier.get("intent"), dict) else {}
    offer = dossier.get("offer") if isinstance(dossier.get("offer"), dict) else {}
    facts = [s for s in (dossier.get("public_facts") or []) if isinstance(s, str) and s.strip()]
    opps = [s for s in (dossier.get("opportunities") or []) if isinstance(s, str) and s.strip()]
    if not opps and _str(dossier.get("opportunity_ref")):
        opps = [dossier["opportunity_ref"]]
    if not opps and _str(offer.get("current")):
        opps = [offer["current"].strip()]

    last_touch = UNKNOWN
    tps = dossier.get("touchpoints")
    if isinstance(tps, list) and tps:
        last = tps[-1] if isinstance(tps[-1], dict) else None
        if last and _str(last.get("summary")):
            bits = [b for b in (_str(last.get("at")), _str(last.get("channel")), _str(last.get("summary"))) if b]
            last_touch = " · ".join(bits)
    # Prefer producer state over the consumer's own ACCEPTED/UNKNOWN decision.
    outcome = _str(dossier.get("situacao")) or _str(dossier.get("outcome"))
    if last_touch == UNKNOWN and outcome:
        last_touch = outcome
    elif outcome and last_touch != UNKNOWN and outcome not in (UNKNOWN, last_touch):
        last_touch = f"{last_touch} — {outcome}"

    why = _str(dossier.get("why_now")) or _str(intent.get("kind")) or UNKNOWN
    if _str(intent.get("reply_reason")):
        why = f"{why} — {intent['reply_reason'].strip()}"

    next_state = _str(offer.get("next_state")) or _str(dossier.get("next_state")) or UNKNOWN
    unknown = dossier.get("unknown") if isinstance(dossier.get("unknown"), list) else _missing_commercial(dossier)

    return {
        "empresa": _str(company.get("name")) or UNKNOWN,
        "por_que_chegou_agora": why,
        "intencao": _str(intent.get("kind")) or UNKNOWN,
        "fatos_verificaveis": facts,
        "o_que_nao_sabemos": unknown,
        "oportunidade_contrato": opps[0] if opps else UNKNOWN,
        "ultimo_touch_outcome": last_touch,
        "proximo_estado_comercial": next_state,
        "inbound_only": dossier.get("inbound_only"),
        "situacao": _str(dossier.get("situacao")) or UNKNOWN,
        "handraiser_id": _str(dossier.get("handraiser_id")),
        "lane": _str(dossier.get("lane")) or _str(dossier.get("acquisition_channel")),
        "identity_ref": _str(dossier.get("identity_ref")) or _str(company.get("identity_ref")),
    }


def _receipt_of(dossier: dict, raw: dict) -> str:
    explicit = _str(dossier.get("receipt"))
    if explicit:
        return explicit
    material = {
        "handraiser_id": dossier.get("handraiser_id"),
        "source_as_of": dossier.get("source_as_of"),
        "public_facts": dossier.get("public_facts"),
        "intent": dossier.get("intent"),
        "offer": dossier.get("offer"),
        "situacao": dossier.get("situacao"),
        "inbound_only": dossier.get("inbound_only"),
        "outcome": dossier.get("outcome"),
    }
    blob = json.dumps(material, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:32]


def _size_of(payload, raw_size: int | None) -> int:
    if raw_size is not None:
        return int(raw_size)
    try:
        return len(json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8"))
    except (TypeError, ValueError):
        return 0


@dataclass
class ConsumeResult:
    ok: bool
    reason: str = ""
    session_id: str | None = None
    handraiser_id: str | None = None
    receipt: str | None = None
    version: int = 0
    updated: bool = False
    replayed: bool = False
    dossier: dict | None = None
    conversation: dict | None = None
    inbound_only: bool | None = None

    def as_http(self) -> dict:
        body = {
            "ok": self.ok,
            "reason": self.reason,
            "session_id": self.session_id,
            "handraiser_id": self.handraiser_id,
            "receipt": self.receipt,
            "version": self.version,
            "updated": self.updated,
            "replayed": self.replayed,
            "inbound_only": self.inbound_only,
        }
        if self.ok and self.conversation is not None:
            body["conversation"] = self.conversation
            body["empresa"] = self.conversation.get("empresa")
            body["intencao"] = self.conversation.get("intencao")
            body["proximo_estado_comercial"] = self.conversation.get("proximo_estado_comercial")
            body["por_que_chegou_agora"] = self.conversation.get("por_que_chegou_agora")
        return body


@dataclass
class AcceptedRecord:
    handraiser_id: str
    session_id: str
    receipt: str
    version: int
    dossier: dict
    conversation: dict
    accepted_at: str
    inbound_only: bool | None = None


class ReceiptStore:
    """In-memory accepted receipts. Rollback disables consume; this stays."""

    def __init__(self) -> None:
        self._by_id: dict[str, AcceptedRecord] = {}

    def get(self, handraiser_id: str) -> AcceptedRecord | None:
        return self._by_id.get(handraiser_id)

    def put(self, record: AcceptedRecord) -> None:
        self._by_id[record.handraiser_id] = record

    def __len__(self) -> int:
        return len(self._by_id)

    def ids(self) -> list[str]:
        return list(self._by_id)


_store = ReceiptStore()


def get_store() -> ReceiptStore:
    return _store


def reset_store() -> ReceiptStore:
    """Test hook: empty the process store. Not an HTTP path."""
    global _store
    _store = ReceiptStore()
    return _store


def _fail(reason: str, **kwargs) -> ConsumeResult:
    log.info("handraiser consume failed reason=%s handraiser_id=%s",
             reason, kwargs.get("handraiser_id") or "")
    return ConsumeResult(ok=False, reason=reason, **kwargs)


def consume(
    payload,
    *,
    enabled: bool = True,
    store: ReceiptStore | None = None,
    now: datetime | None = None,
    raw_size: int | None = None,
    max_bytes: int = MAX_PAYLOAD_BYTES,
    freshness_max_age_s: float = 0,
    bind_session: bool = True,
) -> ConsumeResult:
    """Validate, map, and bind one accepted hand-raiser.

    Fail-closed: rejected/UNKNOWN/schema drift/stale freshness never create a
    session. Replay of the same handraiser_id returns the same session_id.
    A later receipt/version updates that conversation in place.
    """
    store = store if store is not None else get_store()
    now = now or datetime.now(timezone.utc)

    if not enabled:
        return _fail(CONSUMER_DISABLED)

    if _size_of(payload, raw_size) > max_bytes:
        return _fail(OVERSIZED)

    if not isinstance(payload, dict):
        return _fail(MALFORMED)

    doc = _unwrap(payload)
    if not isinstance(doc, dict):
        return _fail(MALFORMED)

    kind = classify_payload(doc)
    if kind == "collection":
        return _fail(SCHEMA_MISMATCH_COLLECTION)
    if kind == "schema_mismatch":
        return _fail(SCHEMA_MISMATCH)
    if kind == "malformed":
        return _fail(MALFORMED)

    admission = None
    item = doc
    extras: dict[str, Any] = {}
    if kind == "wrap":
        admission = doc.get("admission") if isinstance(doc.get("admission"), dict) else None
        inner = doc.get("item") if isinstance(doc.get("item"), dict) else None
        if inner is None:
            inner = doc.get("sales_context") if isinstance(doc.get("sales_context"), dict) else None
        if inner is not None and is_collection(inner):
            return _fail(SCHEMA_MISMATCH_COLLECTION)
        if inner is None and admission is None:
            return _fail(MALFORMED)
        item = inner if inner is not None else {}
        for k in ("handraiser_id", "origin", "lane", "receipt", "inbound_only",
                  "source_as_of", "freshness", "situacao"):
            if k in doc:
                extras[k] = doc[k]
    elif kind == "admission":
        admission = doc
        item = doc.get("item") if isinstance(doc.get("item"), dict) else {}
        if is_collection(item):
            return _fail(SCHEMA_MISMATCH_COLLECTION)

    if item is not None and is_collection(item):
        return _fail(SCHEMA_MISMATCH_COLLECTION)

    closed = _closed_state(doc) or _closed_state(admission or {}) or _closed_state(item or {})
    if closed == DECISION_REJECTED:
        return _fail(REJECTED_WITH_REASON)
    if closed == DECISION_UNKNOWN:
        return _fail(UNKNOWN_OUTCOME)
    if closed is not None and closed != DECISION_ACCEPTED:
        return _fail(UNKNOWN_OUTCOME)

    if kind == "dossier":
        # Dossier already in copilot semantics. Still refuse a collection tag,
        # sanitize CNPJ-as-ref, and require an identity.
        dossier = json.loads(json.dumps(item, default=str))
        company = dossier.get("company") if isinstance(dossier.get("company"), dict) else {}
        cnpj, leftover = _sanitize_cnpj(company.get("cnpj"))
        if isinstance(company, dict):
            company = dict(company)
            company["cnpj"] = cnpj
            if leftover and not _str(company.get("identity_ref")):
                company["identity_ref"] = leftover
            dossier["company"] = company
        hid = (
            _str(dossier.get("handraiser_id"))
            or _str(dossier.get("action_id"))
            or _str(extras.get("handraiser_id"))
        )
        if not hid:
            return _fail(MISSING_IDENTITY)
        dossier["handraiser_id"] = hid
        if "inbound_only" in extras:
            dossier["inbound_only"] = bool(extras["inbound_only"])
        elif admission is not None and "inbound_only" in admission:
            dossier["inbound_only"] = bool(admission["inbound_only"])
        if _str(extras.get("origin")):
            dossier["origin"] = extras["origin"]
        elif "origin" not in dossier:
            dossier["origin"] = _str(dossier.get("acquisition_channel")) or UNKNOWN
        if _str(extras.get("lane")):
            dossier["lane"] = extras["lane"]
        elif "lane" not in dossier:
            dossier["lane"] = _str(dossier.get("acquisition_channel")) or UNKNOWN
        dossier.setdefault("situacao", UNKNOWN)
        dossier.setdefault("outcome", closed or UNKNOWN)
        dossier.setdefault("next_state", (dossier.get("offer") or {}).get("next_state") if isinstance(dossier.get("offer"), dict) else UNKNOWN)
        dossier["unknown"] = _missing_commercial(dossier)
        reason = _validate(dossier)
        if reason:
            log.info("handraiser dossier invalid reason_code=%s detail=%s", DOSSIER_INVALID, reason)
            return _fail(DOSSIER_INVALID, handraiser_id=hid)
    else:
        dossier = map_to_dossier(item or {}, admission=admission, extras=extras)
        hid = _str(dossier.get("handraiser_id"))
        if not hid:
            return _fail(MISSING_IDENTITY)
        reason = _validate(dossier)
        if reason:
            log.info("handraiser mapped dossier invalid reason_code=%s detail=%s", DOSSIER_INVALID, reason)
            return _fail(DOSSIER_INVALID, handraiser_id=hid)

    fresh_reason = _check_freshness(dossier, now=now, extra_max_age_s=freshness_max_age_s)
    if fresh_reason:
        return _fail(fresh_reason, handraiser_id=dossier.get("handraiser_id"))

    hid = _str(dossier.get("handraiser_id"))
    receipt = _receipt_of(dossier, doc)
    dossier["receipt"] = receipt
    conversation = render_conversation_layer(dossier)
    inbound_only = dossier.get("inbound_only") if "inbound_only" in dossier else None

    existing = store.get(hid)
    if existing is not None and existing.receipt == receipt:
        if bind_session:
            _bind(existing)
        log.info("handraiser consume replay reason=REPLAY handraiser_id=%s session_id=%s version=%s",
                 hid, existing.session_id, existing.version)
        return ConsumeResult(
            ok=True, reason="", session_id=existing.session_id, handraiser_id=hid,
            receipt=existing.receipt, version=existing.version, updated=False,
            replayed=True, dossier=existing.dossier, conversation=existing.conversation,
            inbound_only=existing.inbound_only,
        )

    version = (existing.version + 1) if existing is not None else 1
    session_id = existing.session_id if existing is not None else session_id_for(hid)
    record = AcceptedRecord(
        handraiser_id=hid, session_id=session_id, receipt=receipt, version=version,
        dossier=dossier, conversation=conversation,
        accepted_at=now.isoformat(), inbound_only=inbound_only if isinstance(inbound_only, bool) else None,
    )
    store.put(record)
    if bind_session:
        _bind(record)
    log.info("handraiser consume ok handraiser_id=%s session_id=%s version=%s updated=%s",
             hid, session_id, version, existing is not None)
    return ConsumeResult(
        ok=True, reason="", session_id=session_id, handraiser_id=hid,
        receipt=receipt, version=version, updated=existing is not None,
        replayed=False, dossier=dossier, conversation=conversation,
        inbound_only=inbound_only if isinstance(inbound_only, bool) else None,
    )


def _bind(record: AcceptedRecord) -> None:
    try:
        from .. import meeting
    except ImportError:
        return
    session = meeting.get_or_create(record.session_id)
    session.handraiser_id = record.handraiser_id
    session.handraiser_context = record.dossier
    session.handraiser_version = record.version


def context_for_session(session, *, refresh: bool = True, enabled: bool = True) -> dict | None:
    """Dossier bound to this meeting, optionally re-read from the producer."""
    hid = getattr(session, "handraiser_id", None)
    if not hid:
        meeting_id = getattr(session, "meeting_id", "") or ""
        if isinstance(meeting_id, str) and meeting_id.startswith(SESSION_PREFIX):
            hid = meeting_id[len(SESSION_PREFIX):]
    if not hid:
        bound = getattr(session, "handraiser_context", None)
        return bound if isinstance(bound, dict) else None
    rec = get_store().get(hid)
    if rec is None:
        bound = getattr(session, "handraiser_context", None)
        return bound if isinstance(bound, dict) else None
    if refresh and enabled:
        refresh_from_producer(hid, store=get_store())
        rec = get_store().get(hid) or rec
        session.handraiser_context = rec.dossier
        session.handraiser_version = rec.version
    return rec.dossier


def refresh_from_producer(handraiser_id: str, *, store: ReceiptStore | None = None,
                          url: str | None = None, timeout: float = 3.0) -> str:
    """Re-read Warmbly. Collection is never treated as a dossier.

    Returns a reason string when the producer was unreachable or unusable;
    empty string on success or when no producer URL is configured.
    """
    store = store if store is not None else get_store()
    if not url:
        try:
            from ..config import settings
            url = getattr(settings, "warmbly_sales_context_url", "") or ""
        except Exception:  # noqa: BLE001
            url = ""
    url = (url or "").strip()
    if not url:
        return ""
    target = url.replace("{id}", handraiser_id).replace("{action_id}", handraiser_id)
    try:
        req = Request(target, headers={"Accept": "application/json"})
        with urlopen(req, timeout=timeout) as resp:  # noqa: S310 - operator-configured URL
            raw = resp.read()
    except (HTTPError, URLError, TimeoutError, OSError) as e:
        log.info("handraiser producer unread reason=PRODUCER_UNREACHABLE handraiser_id=%s", handraiser_id)
        return f"PRODUCER_UNREACHABLE:{type(e).__name__}"
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return SCHEMA_MISMATCH
    doc = _unwrap(payload)
    if is_collection(doc) and isinstance(doc, dict):
        items = doc.get("items") if isinstance(doc.get("items"), list) else []
        match = None
        for it in items:
            if isinstance(it, dict) and _str(it.get("action_id")) == handraiser_id:
                match = it
                break
        if match is None:
            log.info("handraiser producer collection had no item handraiser_id=%s", handraiser_id)
            return NOT_FOUND
        doc = match
    result = consume(doc, enabled=True, store=store, bind_session=True)
    return "" if result.ok else result.reason


def assert_no_invented_fields(dossier: dict) -> list[str]:
    """Return names of commercial fields that were filled without producer input.

    Used by tests: consume must not invent CNPJ/cargo/decisor/fit/chance/prazo/prova.
    """
    invented: list[str] = []
    company = dossier.get("company") if isinstance(dossier.get("company"), dict) else {}
    # A real CNPJ is only allowed if it looks like one AND came through as cnpj.
    # Tests pass a producer payload without cnpj and assert still None/absent.
    text = json.dumps(dossier, ensure_ascii=False).lower()
    for field in _INVENTED_COMMERCIAL:
        if field == "cnpj":
            if looks_like_cnpj(company.get("cnpj")):
                # presence is fine only when producer sent a CNPJ-shaped value;
                # callers compare against the input.
                continue
            if company.get("cnpj") not in (None, "", UNKNOWN):
                invented.append("cnpj")
            continue
        # These must not appear as filled commercial claims.
        for path in (dossier.get(field), (dossier.get("intent") or {}).get(field) if isinstance(dossier.get("intent"), dict) else None):
            if isinstance(path, str) and path.strip() and path.strip() not in (UNKNOWN,):
                invented.append(field)
    return invented


_AFFIRMATIVE_FIT = (
    re.compile(r"legalmente habilitad[oa]", re.I),
    re.compile(r"fit hist[oó]rico.{0,80}\bé\b.{0,40}habilita", re.I),
    re.compile(r"fit hist[oó]rico.{0,80}(significa|equivale).{0,40}habilita", re.I),
    re.compile(r"probabilidade de vit[oó]ria\s*[:=]\s*\d", re.I),
    re.compile(r"chance de vit[oó]ria\s*[:=]\s*\d", re.I),
    re.compile(r"win probability\s*[:=]\s*\d", re.I),
)


def prompt_safety_ok(text: str) -> bool:
    """False if copy *asserts* historical fit as legal enablement or win chance.

    Prohibition language (“não são habilitação legal”) is allowed — that is the
    guard. Affirmative conversion is not.
    """
    t = text or ""
    return not any(p.search(t) for p in _AFFIRMATIVE_FIT)
