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
import ssl
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
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

# Published Governance authority loaded at Governance 22ad810a8c1d46d9a787efcfac825d6ba0336bff.
# Warmbly persists this exact authority on NetNewInboundReadback; Meetcfg is a
# consumer of that decision and never substitutes its local context digest.
GOVERNANCE_AUTHORITY_SHA = "22ad810a8c1d46d9a787efcfac825d6ba0336bff"
GOVERNANCE_AUTHORITY = "NET_NEW_INBOUND_HANDRAISER/1.0.0-draft.20260904"
GOVERNANCE_POLICY_HASH = "984f442690f7c74f309173b31008518631170d63733b5cc04c32abaf88c67e28"

# Campaign 14 pin. Runtime requires this exact context schema + hash.
# Fixtures are test-only; they are never a fallback when the pin is missing.
SCHEMA_CONTEXT = "MEETCFG_HANDRAISER_CONTEXT/1.0.0-draft.20260904"
PINNED_CONTRACTS = {
    "admission": GOVERNANCE_AUTHORITY,
    "catalog": "CONFENGE_OFFER_CATALOG/2.0.0-draft.20260904",
    "context": SCHEMA_CONTEXT,
    "intake": "CONFENGE_WEB_INTAKE/2.0.0-draft.20260904",
    "state": "CONFENGE_HANDRAISER_STATE/1.0.0-draft.20260904",
    "taxonomy": "CONFENGE_CORPORATE_TAXONOMY/1.0.0-draft.20260904",
}
PIN_HASH = hashlib.sha256(
    json.dumps(PINNED_CONTRACTS, sort_keys=True, separators=(",", ":")).encode("utf-8")
).hexdigest()

SOURCE_LANE = "CONFENGE_WEB"
OFFER_CANDIDATE = "private_project_technical_readiness_assessment"
PRIVATE_ASSET = "private_project_technical_readiness_v1"
NUCLEI = (
    "expert_evidence_assistance",
    "property_valuation",
    "building_engineering_documentation",
    "occupational_safety",
    "public_works_b2g",
)
NUCLEUS_LABELS = {
    "expert_evidence_assistance": "Assistência em prova pericial",
    "property_valuation": "Avaliação patrimonial",
    "building_engineering_documentation": "Documentação de engenharia predial",
    "occupational_safety": "Segurança do trabalho",
    "public_works_b2g": "Obras públicas (B2G)",
}
_ALLOWED_SOURCE_LANES = {
    "CONFENGE_WEB",
    "confenge_web",
    "NET_NEW_INBOUND",
    "net_new_inbound",
    "INBOUND_LIVE",
}
_CONFLICT_CLEARED = {"CLEAR", "RESTRICTED"}
_RESTRICTION_CLASS_RE = re.compile(r"^[A-Z0-9_]{1,64}$")
CRM_SIDE_EFFECT_KEYS = (
    "lead", "lead_id", "pipeline", "queue", "cadence",
    "offer_truth", "opportunity", "account",
)

# Warmbly main producer fingerprint for the persisted readback contract.
WARMBLY_PRODUCER_SHA = "33bd329437bc04a2e95ef0f4d562d26b85f34e35"
WARMBLY_PRODUCER_FILE = "internal/app/confenge/net_new_inbound_ingest.go"
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
IDENTITY_CONFLICT = "IDENTITY_CONFLICT"
PRODUCER_NOT_CONFIGURED = "PRODUCER_NOT_CONFIGURED"
PRODUCER_UNREACHABLE = "PRODUCER_UNREACHABLE"
PRODUCER_TIMEOUT = "PRODUCER_TIMEOUT"
PRODUCER_UNAUTHORIZED = "PRODUCER_UNAUTHORIZED"
PRODUCER_ERROR = "PRODUCER_ERROR"
SCHEMA_UNPINNED = "SCHEMA_UNPINNED"
SCHEMA_PIN_MISMATCH = "SCHEMA_PIN_MISMATCH"
MISSING_CONFLICT_CLEARANCE = "MISSING_CONFLICT_CLEARANCE"
NUCLEUS_UNKNOWN = "NUCLEUS_UNKNOWN"
SOURCE_LANE_MISMATCH = "SOURCE_LANE_MISMATCH"
OFFER_CANDIDATE_MISMATCH = "OFFER_CANDIDATE_MISMATCH"
OUTBOUND_NOT_ELIGIBLE = "OUTBOUND_NOT_ELIGIBLE"
READBACK_INCOMPLETE = "READBACK_INCOMPLETE"

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
        if payload.get("schema") in (SCHEMA_ID, SCHEMA_ITEM, SCHEMA_EXPORT, SCHEMA_ADMISSION, SCHEMA_CONTEXT):
            return payload
        return inner
    return payload


def classify_payload(payload) -> str:
    """Classify one supported runtime document without adapting its shape."""
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
    if schema == SCHEMA_CONTEXT:
        return "wrap"
    if schema == GOVERNANCE_AUTHORITY:
        return "native_readback"
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


def normalize_source_lane(raw: str) -> str:
    s = _str(raw)
    if s in ("CONFENGE_WEB", "confenge_web", "confenge_web_intent"):
        return SOURCE_LANE
    if s in ("NET_NEW_INBOUND", "net_new_inbound", "INBOUND_LIVE"):
        return SOURCE_LANE
    return s


def _contracts_of(doc: dict) -> dict:
    raw = doc.get("contracts")
    return dict(raw) if isinstance(raw, dict) else {}


def pin_reason(doc: dict) -> str:
    """Fail closed unless the envelope pins the campaign-14 contracts + hash."""
    if not isinstance(doc, dict):
        return SCHEMA_UNPINNED
    schema = _str(doc.get("schema"))
    if schema != SCHEMA_CONTEXT:
        return SCHEMA_UNPINNED
    hash_val = _str(doc.get("schema_hash") or doc.get("pin_hash"))
    if not hash_val:
        return SCHEMA_UNPINNED
    if hash_val != PIN_HASH:
        return SCHEMA_PIN_MISMATCH
    contracts = _contracts_of(doc)
    if not contracts:
        return SCHEMA_UNPINNED
    for key, expected in PINNED_CONTRACTS.items():
        got = _str(contracts.get(key))
        if not got:
            return SCHEMA_UNPINNED
        if got != expected:
            return SCHEMA_PIN_MISMATCH
    authority = doc.get("authority") if isinstance(doc.get("authority"), dict) else {}
    policy_version = _str(doc.get("policy_version") or authority.get("canonical_name"))
    policy_hash = _str(doc.get("policy_hash") or authority.get("policy_hash"))
    if not policy_version or not policy_hash:
        return SCHEMA_UNPINNED
    if policy_version != GOVERNANCE_AUTHORITY or policy_hash != GOVERNANCE_POLICY_HASH:
        return SCHEMA_PIN_MISMATCH
    return ""


def native_readback_reason(doc: dict) -> str:
    """Validate Warmbly's persisted NetNewInboundReadback, fail closed.

    IDs and acknowledgement are required fields of the complete readback
    shape. Persist provenance comes from the authenticated Warmbly runtime GET,
    outside this shape validator. The authority outcome is checked separately.
    """
    if _str(doc.get("schema")) != GOVERNANCE_AUTHORITY:
        return SCHEMA_UNPINNED
    policy_version = _str(doc.get("policy_version"))
    policy_hash = _str(doc.get("hash"))
    if not policy_version or not policy_hash:
        return SCHEMA_UNPINNED
    if policy_version != GOVERNANCE_AUTHORITY or policy_hash != GOVERNANCE_POLICY_HASH:
        return SCHEMA_PIN_MISMATCH
    if _str(doc.get("intake_schema")) != PINNED_CONTRACTS["intake"]:
        return SCHEMA_PIN_MISMATCH
    if _str(doc.get("state_schema")) != PINNED_CONTRACTS["state"]:
        return SCHEMA_PIN_MISMATCH
    if doc.get("inbound_only") is not True:
        return OUTBOUND_NOT_ELIGIBLE
    for key in ("outbound_eligible", "auto_send", "dispatch_attempted"):
        if doc.get(key) is not False:
            return OUTBOUND_NOT_ELIGIBLE
    if doc.get("meetcfg_handoff_allowed") is not True:
        return READBACK_INCOMPLETE
    if not all(_str(doc.get(key)) for key in (
        "logical_id", "receipt", "acknowledged_by", "acknowledged_at",
        "account_id", "action_id",
    )):
        return READBACK_INCOMPLETE
    if _parse_instant(doc.get("acknowledged_at")) is None:
        return READBACK_INCOMPLETE
    for key in ("account_id", "action_id"):
        try:
            persisted_id = uuid.UUID(_str(doc.get(key)))
        except (ValueError, AttributeError):
            return READBACK_INCOMPLETE
        if persisted_id.int == 0:
            return READBACK_INCOMPLETE
    return ""


def _conflict_blob(*srcs) -> dict | None:
    for src in srcs:
        if isinstance(src, dict) and isinstance(src.get("conflict"), dict):
            return src["conflict"]
    return None


def conflict_reason(*srcs) -> str:
    blob = _conflict_blob(*srcs)
    if not isinstance(blob, dict):
        return MISSING_CONFLICT_CLEARANCE
    status = _str(blob.get("status") or blob.get("clearance")).upper()
    if status not in _CONFLICT_CLEARED:
        return MISSING_CONFLICT_CLEARANCE
    return ""


def source_lane_reason(*srcs) -> str:
    for src in srcs:
        if not isinstance(src, dict):
            continue
        for key in ("source", "lane", "origin"):
            raw = _str(src.get(key))
            if raw:
                if raw not in _ALLOWED_SOURCE_LANES:
                    return SOURCE_LANE_MISMATCH
                return ""
        channel = _str(src.get("acquisition_channel") or src.get("acquisition_lane"))
        if channel:
            if channel not in _ALLOWED_SOURCE_LANES and channel not in ("confenge_web",):
                return SOURCE_LANE_MISMATCH
            return ""
    return SOURCE_LANE_MISMATCH


def nucleus_id_of(*srcs) -> str:
    for src in srcs:
        if not isinstance(src, dict):
            continue
        for key in ("nucleus_id", "nucleus"):
            v = _str(src.get(key))
            if v:
                return v
        tax = src.get("taxonomy") if isinstance(src.get("taxonomy"), dict) else None
        if tax:
            v = _str(tax.get("nucleus_id") or tax.get("nucleus"))
            if v:
                return v
    return ""


def nucleus_reason(*srcs) -> str:
    nid = nucleus_id_of(*srcs)
    if nid not in NUCLEI:
        return NUCLEUS_UNKNOWN
    return ""


def eligibility_reason(*srcs) -> str:
    for src in srcs:
        if not isinstance(src, dict):
            continue
        if src.get("outbound_eligible") is True or src.get("auto_send") is True:
            return OUTBOUND_NOT_ELIGIBLE
    return ""


def offer_candidate_of(*srcs) -> str:
    for src in srcs:
        if not isinstance(src, dict):
            continue
        offer = src.get("offer") if isinstance(src.get("offer"), dict) else {}
        v = _str(src.get("offer_candidate") or offer.get("candidate") or offer.get("current"))
        if v:
            return v
    return ""


def offer_reason(*srcs) -> str:
    v = offer_candidate_of(*srcs)
    if not v:
        return ""
    if v != OFFER_CANDIDATE:
        return OFFER_CANDIDATE_MISMATCH
    return ""


def _restriction_class(conflict: dict | None) -> str:
    """Class token only. Free-text process/employee/document detail is dropped."""
    if not isinstance(conflict, dict):
        return UNKNOWN
    raw = _str(conflict.get("restriction") or conflict.get("restriction_class"))
    token = raw.upper().replace("-", "_").replace(" ", "_")
    if _RESTRICTION_CLASS_RE.match(token):
        return token
    status = _str(conflict.get("status")).upper()
    return status if status in _CONFLICT_CLEARED else UNKNOWN


def suggested_questions(unknown: list[str], handoff: dict) -> list[str]:
    """Questions from permitted gaps only. Never invent a commercial claim."""
    qs: list[str] = []
    seen: set[str] = set()

    def add(q: str) -> None:
        if q and q not in seen:
            seen.add(q)
            qs.append(q)

    unknown_l = [u.lower() for u in unknown if isinstance(u, str)]
    mapping = (
        ("cnpj", "Qual o CNPJ da organização, se for público e puder informar?"),
        ("cargo", "Qual o papel de quem fala nesta conversa?"),
        ("decisor", "Quem decide o próximo passo?"),
        ("prazo", "Qual o prazo desta decisão?"),
        ("prova", "Há evidência técnica já disponível para esta conversa?"),
        ("empresa", "Qual o nome da organização, se puder informar?"),
    )
    for needle, question in mapping:
        if any(needle in u for u in unknown_l):
            add(question)
    if _str(handoff.get("decision_role")) in ("", UNKNOWN):
        add("Quem decide nesta conversa?")
    if _str(handoff.get("document_availability_class")) in ("", UNKNOWN, "unknown"):
        add("Quais documentos técnicos já existem e podem ser compartilhados?")
    if _str(handoff.get("urgency")) in ("", UNKNOWN):
        add("Qual a urgência real desta decisão?")
    if _str(handoff.get("city_service_area_class")) in ("", UNKNOWN):
        add("Em qual município ou área de serviço isso se aplica?")
    return qs[:6]


def crm_side_effect_keys(obj: dict | None) -> list[str]:
    """Keys that would mean Meetcfg created a CRM record. Producer refs are not these."""
    if not isinstance(obj, dict):
        return []
    return [k for k in CRM_SIDE_EFFECT_KEYS if k in obj]


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

    lane_raw = (
        _str(extras.get("lane"))
        or _str(extras.get("source"))
        or _str(src.get("lane"))
        or _str(src.get("source"))
        or _str(src.get("acquisition_channel"))
        or _str(admission.get("acquisition_lane"))
        or _str(admission.get("origin"))
    )
    origin = (
        _str(extras.get("origin"))
        or _str(extras.get("source"))
        or _str(admission.get("origin"))
        or _str(src.get("origin"))
        or lane_raw
    )
    lane = normalize_source_lane(lane_raw) or normalize_source_lane(origin) or SOURCE_LANE
    origin = normalize_source_lane(origin) or lane
    channel = map_channel(lane)

    company_src = src.get("company") if isinstance(src.get("company"), dict) else {}
    company_name = (
        _str(company_src.get("name"))
        or _str(src.get("company_name"))
        or _str(extras.get("company_name"))
    ) or UNKNOWN
    cnpj, leftover_ref = _sanitize_cnpj(company_src.get("cnpj"))
    identity_blob = extras.get("identity") if isinstance(extras.get("identity"), dict) else {}
    if not identity_blob and isinstance(src.get("identity"), dict):
        identity_blob = src["identity"]
    identity_ref = (
        _str(company_src.get("identity_ref"))
        or _str(src.get("company_ref"))
        or leftover_ref
        or _str(identity_blob.get("inbound_ref"))
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
        _str(admission.get("receipt_id"))
        or _str(extras.get("receipt"))
        or _str(src.get("receipt"))
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

    situation = extras.get("situation") if isinstance(extras.get("situation"), dict) else {}
    if not situation and isinstance(src.get("situation"), dict):
        situation = src["situation"]
    why_now = (
        _str(facts.get("why_now"))
        or _str(situation.get("why_now"))
        or _str(intent_kind)
    )
    permitted = extras.get("permitted") if isinstance(extras.get("permitted"), dict) else {}
    if not permitted and isinstance(src.get("permitted"), dict):
        permitted = src["permitted"]
    permitted_facts = permitted.get("facts") if isinstance(permitted.get("facts"), list) else []
    permitted_facts = [x for x in permitted_facts if isinstance(x, str) and x.strip()]
    if permitted_facts and not public_facts:
        public_facts = list(permitted_facts)
    if _str(permitted.get("provenance")):
        provenance = _str(permitted.get("provenance"))
    owner_links = permitted.get("owner_links") if isinstance(permitted.get("owner_links"), list) else extras.get("owner_links")
    if not isinstance(owner_links, list):
        owner_links = []
    owner_links = [x for x in owner_links if isinstance(x, str) and x.strip().startswith("https://")]

    nucleus = nucleus_id_of(extras, src, admission)
    offer_candidate = offer_candidate_of(extras, src, admission) or UNKNOWN
    conflict = _conflict_blob(extras, src, admission)
    decision_role = (
        _str(identity_blob.get("decision_role"))
        or _str(extras.get("decision_role"))
        or UNKNOWN
    )
    qualification_state = (
        _str(extras.get("qualification_state"))
        or _str(src.get("qualification_state"))
        or outcome
        or UNKNOWN
    )

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

    dossier["nucleus_id"] = nucleus or UNKNOWN
    dossier["offer_candidate"] = offer_candidate
    dossier["private_asset"] = _str(extras.get("private_asset") or src.get("private_asset")) or PRIVATE_ASSET
    dossier["decision_role"] = decision_role
    dossier["urgency"] = _str(situation.get("urgency")) or UNKNOWN
    dossier["city_service_area_class"] = _str(situation.get("city_service_area_class")) or UNKNOWN
    dossier["desired_decision"] = _str(situation.get("desired_decision")) or UNKNOWN
    dossier["document_availability_class"] = _str(situation.get("document_availability_class")) or UNKNOWN
    dossier["problema"] = _str(situation.get("problema")) or UNKNOWN
    dossier["conflict_status"] = _str((conflict or {}).get("status")).upper() or UNKNOWN
    dossier["conflict_restriction"] = _restriction_class(conflict)
    dossier["qualification_state"] = qualification_state
    dossier["owner_links"] = owner_links
    dossier["permitted_facts"] = permitted_facts or public_facts
    dossier["schema_context"] = SCHEMA_CONTEXT
    dossier["schema_hash"] = _str(extras.get("schema_hash") or src.get("schema_hash")) or PIN_HASH
    dossier["policy_version"] = (
        _str(extras.get("policy_version") or src.get("policy_version"))
        or GOVERNANCE_AUTHORITY
    )
    dossier["policy_hash"] = (
        _str(extras.get("policy_hash") or src.get("policy_hash"))
        or GOVERNANCE_POLICY_HASH
    )
    dossier["source"] = SOURCE_LANE
    dossier["outbound_eligible"] = False
    dossier["auto_send"] = False
    if dossier.get("lane") not in (SOURCE_LANE, UNKNOWN):
        dossier["lane"] = normalize_source_lane(dossier.get("lane") or "") or SOURCE_LANE
    dossier["unknown"] = _missing_commercial(dossier)
    extra_unknown = []
    if decision_role in ("", UNKNOWN):
        extra_unknown.append("papel de quem decide")
    if dossier["document_availability_class"] in ("", UNKNOWN, "unknown"):
        extra_unknown.append("disponibilidade documental")
    if dossier["urgency"] in ("", UNKNOWN):
        extra_unknown.append("urgência")
    seen_u = list(dossier["unknown"])
    for item in extra_unknown:
        if item not in seen_u:
            seen_u.append(item)
    dossier["unknown"] = seen_u
    return dossier


def map_readback_to_dossier(readback: dict) -> dict:
    """Project an authoritative Warmbly readback into the in-memory context.

    Only fields present on NetNewInboundReadback are copied. Commercial stage,
    company, participant roles, work kind, next state, and technical evidence
    remain UNKNOWN because the persisted receipt does not establish them.
    """
    logical_id = _str(readback.get("logical_id"))
    acknowledged_at = _str(readback.get("acknowledged_at"))
    why_now = _str(readback.get("why_now")) or UNKNOWN
    canonical_entity_id = _str(readback.get("canonical_entity_id"))
    conflict_ref = _str(readback.get("conflict_ref"))
    dossier: dict[str, Any] = {
        "schema": SCHEMA_ID,
        "handraiser_id": logical_id,
        "origin": SOURCE_LANE,
        "lane": SOURCE_LANE,
        "source": SOURCE_LANE,
        "acquisition_channel": "INBOUND_LIVE",
        "company": {"name": UNKNOWN, "cnpj": None},
        "intent": {"kind": UNKNOWN, "reply_reason": None},
        "offer": {"current": None, "next_state": UNKNOWN},
        "source_as_of": acknowledged_at,
        "freshness": {"as_of": acknowledged_at},
        "provenance": "warmbly_net_new_inbound_readback",
        "public_facts": [],
        "permitted_facts": [],
        "opportunities": [],
        "evidence": [],
        "limits": [],
        "touchpoints": [],
        "situacao": "ACCEPTED",
        "receipt": _str(readback.get("receipt")),
        "outcome": "ACCEPTED",
        "why_now": why_now,
        "next_state": UNKNOWN,
        "account_id": _str(readback.get("account_id")),
        "action_id": _str(readback.get("action_id")),
        "inbound_only": True,
        "nucleus_id": _str(readback.get("nucleus")),
        "offer_candidate": _str(readback.get("offer_candidate")) or UNKNOWN,
        "private_asset": _str(readback.get("source_asset")) or UNKNOWN,
        "decision_role": UNKNOWN,
        "urgency": _str(readback.get("urgency")) or UNKNOWN,
        "city_service_area_class": _str(readback.get("city_class")) or UNKNOWN,
        "desired_decision": UNKNOWN,
        "document_availability_class": UNKNOWN,
        "problema": UNKNOWN,
        # ACCEPTED proves the Governance gates passed. It does not tell Meetcfg
        # whether the protected conflict result was CLEAR or RESTRICTED.
        "conflict_status": UNKNOWN,
        "conflict_restriction": UNKNOWN,
        "conflict_ref": conflict_ref or None,
        "qualification_state": "ACCEPTED",
        "owner_links": [],
        "schema_context": SCHEMA_CONTEXT,
        "schema_hash": PIN_HASH,
        "policy_version": GOVERNANCE_AUTHORITY,
        "policy_hash": GOVERNANCE_POLICY_HASH,
        "authority_source_sha": GOVERNANCE_AUTHORITY_SHA,
        "outbound_eligible": False,
        "auto_send": False,
        "dispatch_attempted": False,
        "meetcfg_handoff_allowed": True,
        "acknowledged_by": _str(readback.get("acknowledged_by")),
        "acknowledged_at": acknowledged_at,
    }
    if canonical_entity_id:
        dossier["identity_ref"] = canonical_entity_id
        dossier["company"]["identity_ref"] = canonical_entity_id
    dossier["unknown"] = _missing_commercial(dossier)

    handoff = {
        "decision_role": UNKNOWN,
        "document_availability_class": UNKNOWN,
        "urgency": dossier["urgency"],
        "city_service_area_class": dossier["city_service_area_class"],
    }
    from .conversion import SCHEMA_MEETING_PLAN, parse_meeting_plan
    raw_plan = {
        "schema": SCHEMA_MEETING_PLAN,
        "objective": UNKNOWN,
        "participant_roles": [{"name": UNKNOWN, "role": UNKNOWN}],
        "unanswered_questions": suggested_questions(dossier["unknown"], handoff),
        "answered_questions": [],
        "evidence_to_confirm": [],
        "scope_limits": [
            "inbound_only=true",
            "outbound_eligible=false",
            "auto_send=false",
            "dispatch_attempted=false",
        ],
        "conflict_limits": ([f"protected conflict_ref={conflict_ref}"] if conflict_ref else []),
        "advancement_criterion": UNKNOWN,
        "work_kind": UNKNOWN,
    }
    plan, reason = parse_meeting_plan(raw_plan)
    if plan is not None:
        dossier["meeting_plan"] = plan
    elif reason:
        dossier["meeting_plan_reason"] = reason
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
    freshness_src = dossier.get("freshness") if isinstance(dossier.get("freshness"), dict) else {}
    freshness = _str(freshness_src.get("as_of")) or _str(dossier.get("source_as_of")) or UNKNOWN
    canal = (
        _str(dossier.get("acquisition_channel"))
        or _str(dossier.get("lane"))
        or UNKNOWN
    )

    nucleus_id = _str(dossier.get("nucleus_id"))
    nucleo = NUCLEUS_LABELS.get(nucleus_id, UNKNOWN)
    problema = _str(dossier.get("problema")) or UNKNOWN
    nucleo_problema = nucleo if problema in ("", UNKNOWN) else f"{nucleo} — {problema}"
    permitted = [s for s in (dossier.get("permitted_facts") or facts) if isinstance(s, str) and s.strip()]
    evidencia = [s for s in (dossier.get("evidence") or []) if isinstance(s, str) and s.strip()]
    restriction = _str(dossier.get("conflict_restriction")) or UNKNOWN
    conflict_status = _str(dossier.get("conflict_status")) or UNKNOWN
    conflict_ref = _str(dossier.get("conflict_ref"))
    if conflict_ref and conflict_status == UNKNOWN:
        limites_conflito = f"referência protegida do produtor: {conflict_ref}; classe UNKNOWN"
    elif conflict_status == "CLEAR" and restriction in ("", "NONE", UNKNOWN):
        limites_conflito = "sem restrição declarada pelo produtor"
    else:
        limites_conflito = f"{conflict_status} · {restriction}"
    handoff = {
        "decision_role": _str(dossier.get("decision_role")) or UNKNOWN,
        "document_availability_class": _str(dossier.get("document_availability_class")) or UNKNOWN,
        "urgency": _str(dossier.get("urgency")) or UNKNOWN,
        "city_service_area_class": _str(dossier.get("city_service_area_class")) or UNKNOWN,
    }
    perguntas = suggested_questions(unknown if isinstance(unknown, list) else [], handoff)
    resumo = nucleo
    if why and why != UNKNOWN:
        resumo = f"{nucleo}: {why}" if nucleo != UNKNOWN else why
    offer_candidate = _str(dossier.get("offer_candidate")) or UNKNOWN
    if offer_candidate not in (UNKNOWN, OFFER_CANDIDATE, ""):
        offer_candidate = UNKNOWN  # never render an unpinned commercial offer as truth
    if offer_candidate == OFFER_CANDIDATE and opps == []:
        opps = [OFFER_CANDIDATE]

    return {
        "empresa": _str(company.get("name")) or UNKNOWN,
        "por_que_chegou_agora": why,
        "canal": canal,
        "intencao": _str(intent.get("kind")) or UNKNOWN,
        "fatos_verificaveis": facts,
        "o_que_nao_sabemos": unknown,
        "oportunidade_contrato": opps[0] if opps else UNKNOWN,
        "ultimo_touch_outcome": last_touch,
        "proximo_estado_comercial": next_state,
        "inbound_only": dossier.get("inbound_only"),
        "freshness": freshness,
        "status": DECISION_ACCEPTED,
        "situacao": _str(dossier.get("situacao")) or UNKNOWN,
        "handraiser_id": _str(dossier.get("handraiser_id")),
        "lane": SOURCE_LANE,
        "source": SOURCE_LANE,
        "identity_ref": _str(dossier.get("identity_ref")) or _str(company.get("identity_ref")),
        "resumo": resumo or UNKNOWN,
        "nucleo": nucleo,
        "nucleo_id": nucleus_id or UNKNOWN,
        "problema": problema,
        "nucleo_problema": nucleo_problema,
        "o_que_ja_se_sabe": permitted or facts or [UNKNOWN],
        "o_que_e_unknown": unknown,
        "perguntas_sugeridas": perguntas,
        "limites_conflito": limites_conflito,
        "proximo_estado": next_state,
        "evidencia_tecnica": evidencia or [UNKNOWN],
        "offer_candidate": offer_candidate if offer_candidate else UNKNOWN,
        "decision_role": handoff["decision_role"],
        "urgency": handoff["urgency"],
        "city_service_area_class": handoff["city_service_area_class"],
        "desired_decision": _str(dossier.get("desired_decision")) or UNKNOWN,
        "document_availability_class": handoff["document_availability_class"],
        "conflict_status": conflict_status,
        "qualification_state": _str(dossier.get("qualification_state")) or UNKNOWN,
        "owner_links": dossier.get("owner_links") if isinstance(dossier.get("owner_links"), list) else [],
        "receipt": _str(dossier.get("receipt")) or UNKNOWN,
        "as_of": freshness,
        "provenance": _str(dossier.get("provenance")) or UNKNOWN,
        "outbound_eligible": False,
        "auto_send": False,
        "dispatch_attempted": bool(dossier.get("dispatch_attempted")),
        "meetcfg_handoff_allowed": dossier.get("meetcfg_handoff_allowed"),
        "schema": SCHEMA_CONTEXT,
        "schema_hash": _str(dossier.get("schema_hash")) or PIN_HASH,
        "policy_version": _str(dossier.get("policy_version")) or UNKNOWN,
        "policy_hash": _str(dossier.get("policy_hash")) or UNKNOWN,
        "detalhe": {
            "handraiser_id": _str(dossier.get("handraiser_id")),
            "nucleus_id": nucleus_id or UNKNOWN,
            "schema": SCHEMA_CONTEXT,
            "schema_hash": _str(dossier.get("schema_hash")) or PIN_HASH,
            "policy_version": _str(dossier.get("policy_version")) or UNKNOWN,
            "policy_hash": _str(dossier.get("policy_hash")) or UNKNOWN,
            "receipt": _str(dossier.get("receipt")),
            "source": SOURCE_LANE,
            "lane": SOURCE_LANE,
        },
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


_SEQ_RECEIPT = re.compile(r"^(.*?)(\d+)$")


def _receipt_rank(receipt: str, dossier: dict) -> tuple[float, int]:
    """Monotonic rank: (source_as_of, sequential suffix). Opaque hashes use as_of only."""
    as_of = _parse_instant(dossier.get("source_as_of"))
    freshness = dossier.get("freshness") if isinstance(dossier.get("freshness"), dict) else {}
    if as_of is None:
        as_of = _parse_instant(freshness.get("as_of"))
    ts = as_of.timestamp() if as_of else 0.0
    s = receipt or ""
    m = _SEQ_RECEIPT.match(s)
    if m and (s.startswith("rcpt_") or "_" in s):
        try:
            return ts, int(m.group(2))
        except ValueError:
            return ts, 0
    return ts, 0


def _is_older_receipt(receipt: str, dossier: dict, existing: AcceptedRecord) -> bool:
    incoming = _receipt_rank(receipt, dossier)
    held = _receipt_rank(existing.receipt, existing.dossier)
    return incoming < held


def _identity_key(dossier: dict) -> tuple[str, str, str, str]:
    company = dossier.get("company") if isinstance(dossier.get("company"), dict) else {}
    ref = _str(dossier.get("identity_ref")) or _str(company.get("identity_ref"))
    account = _str(dossier.get("account_id"))
    action = _str(dossier.get("action_id"))
    cnpj = _str(company.get("cnpj")) if looks_like_cnpj(company.get("cnpj")) else ""
    return ref, account, action, cnpj


def _identity_conflict(existing: dict, incoming: dict) -> bool:
    """Same handraiser_id with two filled-and-different identity refs fails closed."""
    a = _identity_key(existing)
    b = _identity_key(incoming)
    for left, right in zip(a, b):
        if left and right and left != right:
            return True
    return False


def _native_identity_conflict(existing: dict, incoming: dict) -> bool:
    """Persisted readback identity is stable, including optional ref presence."""
    return _identity_key(existing) != _identity_key(incoming)


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
            body["canal"] = self.conversation.get("canal")
            body["freshness"] = self.conversation.get("freshness")
            body["status"] = self.conversation.get("status")
            body["resumo"] = self.conversation.get("resumo")
            body["nucleo"] = self.conversation.get("nucleo")
            body["nucleo_id"] = self.conversation.get("nucleo_id")
            body["nucleo_problema"] = self.conversation.get("nucleo_problema")
            body["proximo_estado"] = self.conversation.get("proximo_estado")
            body["source"] = self.conversation.get("source")
            body["schema"] = self.conversation.get("schema")
            body["outbound_eligible"] = self.conversation.get("outbound_eligible")
            body["auto_send"] = self.conversation.get("auto_send")
            body["dispatch_attempted"] = self.conversation.get("dispatch_attempted")
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

    def records(self) -> list[AcceptedRecord]:
        return list(self._by_id.values())


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
        for k in (
            "handraiser_id", "origin", "lane", "source", "receipt", "inbound_only",
            "source_as_of", "freshness", "situacao", "nucleus_id", "nucleus",
            "offer_candidate", "private_asset", "conflict", "identity",
            "situation", "permitted", "auto_send", "outbound_eligible",
            "qualification_state", "schema_hash", "contracts", "owner_links",
            "policy_version", "policy_hash", "decision_role", "decision",
            "meeting_plan", "commercial_stage",
        ):
            if k in doc:
                extras[k] = doc[k]
    elif kind == "admission":
        admission = doc
        item = doc.get("item") if isinstance(doc.get("item"), dict) else {}
        if is_collection(item):
            return _fail(SCHEMA_MISMATCH_COLLECTION)

    if item is not None and is_collection(item):
        return _fail(SCHEMA_MISMATCH_COLLECTION)

    if kind == "native_readback":
        # NetNewInboundReadback.outcome is the authoritative persisted result.
        # An extra alias must never mask UNKNOWN/REJECTED or contradict it.
        closed = _str(doc.get("outcome")) or None
        if "decision" in doc and _str(doc.get("decision")) != closed:
            return _fail(UNKNOWN_OUTCOME)
    else:
        closed = _closed_state(doc) or _closed_state(admission or {}) or _closed_state(item or {})
    if closed == DECISION_REJECTED:
        return _fail(REJECTED_WITH_REASON)
    if closed == DECISION_UNKNOWN:
        return _fail(UNKNOWN_OUTCOME)
    if closed is not None and closed != DECISION_ACCEPTED:
        return _fail(UNKNOWN_OUTCOME)

    if kind == "native_readback":
        readback_fail = native_readback_reason(doc)
        if readback_fail:
            log.info("handraiser readback refused reason=%s", readback_fail)
            return _fail(readback_fail)
        nuc_fail = nucleus_reason(doc)
        if nuc_fail:
            return _fail(nuc_fail)
        offer_fail = offer_reason(doc)
        if offer_fail:
            return _fail(offer_fail)
    else:
        pin = pin_reason(doc)
        if pin:
            log.info("handraiser consume failed reason=%s schema=%s", pin, _str(doc.get("schema")))
            return _fail(pin)
        lane_fail = source_lane_reason(doc, extras, admission or {}, item or {})
        if lane_fail:
            return _fail(lane_fail)
        nuc_fail = nucleus_reason(doc, extras, admission or {}, item or {})
        if nuc_fail:
            return _fail(nuc_fail)
        conf_fail = conflict_reason(doc, extras, admission or {}, item or {})
        if conf_fail:
            return _fail(conf_fail)
        elig_fail = eligibility_reason(doc, extras, admission or {}, item or {})
        if elig_fail:
            return _fail(elig_fail)
        offer_fail = offer_reason(doc, extras, admission or {}, item or {})
        if offer_fail:
            return _fail(offer_fail)
    if closed is None:
        # Pinned runtime still requires an explicit ACCEPTED. Native unpinned
        # already failed SCHEMA_UNPINNED; a pinned envelope without decision
        # is not an implicit accept.
        return _fail(UNKNOWN_OUTCOME)

    if kind == "native_readback":
        dossier = map_readback_to_dossier(doc)
        hid = _str(dossier.get("handraiser_id"))
        reason = _validate(dossier)
        if reason:
            log.info("handraiser readback dossier invalid reason_code=%s detail=%s", DOSSIER_INVALID, reason)
            return _fail(DOSSIER_INVALID, handraiser_id=hid)
    elif kind == "dossier":
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
    from .conversion import attach_meeting_plan
    if kind == "native_readback":
        # The native readback has no meeting_plan field. Preserve the limited
        # plan derived above; never trust an extra caller-supplied plan.
        attach_meeting_plan(dossier)
    else:
        plan_src = extras if isinstance(extras, dict) else {}
        if isinstance(doc, dict) and doc.get("meeting_plan") is not None:
            plan_src = {**plan_src, "meeting_plan": doc["meeting_plan"]}
        if isinstance(item, dict) and item.get("meeting_plan") is not None:
            plan_src = {**plan_src, "meeting_plan": item["meeting_plan"]}
        attach_meeting_plan(dossier, plan_src)
    conversation = render_conversation_layer(dossier)
    inbound_only = dossier.get("inbound_only") if "inbound_only" in dossier else None

    existing = store.get(hid)
    if kind == "native_readback":
        for held in store.records():
            if held.handraiser_id != hid and held.receipt == receipt:
                log.info("handraiser consume failed reason=%s handraiser_id=%s", IDENTITY_CONFLICT, hid)
                return _fail(
                    IDENTITY_CONFLICT, handraiser_id=hid, session_id=held.session_id,
                )
        if existing is not None and existing.receipt != receipt:
            log.info("handraiser consume failed reason=%s handraiser_id=%s", IDENTITY_CONFLICT, hid)
            return _fail(
                IDENTITY_CONFLICT, handraiser_id=hid, session_id=existing.session_id,
            )

    identity_changed = existing is not None and (
        _native_identity_conflict(existing.dossier, dossier)
        if kind == "native_readback"
        else _identity_conflict(existing.dossier, dossier)
    )
    if identity_changed:
        log.info("handraiser consume failed reason=%s handraiser_id=%s", IDENTITY_CONFLICT, hid)
        return _fail(IDENTITY_CONFLICT, handraiser_id=hid, session_id=existing.session_id)

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

    if existing is not None and _is_older_receipt(receipt, dossier, existing):
        if bind_session:
            _bind(existing)
        log.info("handraiser consume ignored out-of-order receipt handraiser_id=%s session_id=%s version=%s",
                 hid, existing.session_id, existing.version)
        return ConsumeResult(
            ok=True, reason="", session_id=existing.session_id, handraiser_id=hid,
            receipt=existing.receipt, version=existing.version, updated=False,
            replayed=False, dossier=existing.dossier, conversation=existing.conversation,
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
    plan = record.dossier.get("meeting_plan") if isinstance(record.dossier, dict) else None
    session.meeting_plan = plan if isinstance(plan, dict) else None


def context_for_session(session, *, refresh: bool = False, enabled: bool = True) -> dict | None:
    """Dossier bound to this meeting.

    `refresh` is ignored on purpose: the producer is never fetched on a copilot
    tick. Call refresh_conversations() from startup or the explicit control.
    """
    del refresh, enabled  # orientation path is memory-only; flags kept for callers
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
    session.handraiser_context = rec.dossier
    session.handraiser_version = rec.version
    return rec.dossier


class ProducerTransportError(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _redact_url(url: str) -> str:
    """Host+path only. Query, fragment, and userinfo never reach logs."""
    try:
        p = urlsplit(url)
    except ValueError:
        return ""
    host = p.hostname or ""
    if p.port:
        host = f"{host}:{p.port}"
    return urlunsplit((p.scheme, host, p.path, "", ""))


def producer_headers(token: str = "") -> dict[str, str]:
    headers = {"Accept": "application/json"}
    tok = (token or "").strip()
    if tok:
        headers["Authorization"] = f"Bearer {tok}"
    return headers


def default_transport(url: str, headers: dict, timeout: float) -> tuple[int, bytes]:
    """TLS-verified GET. Never logs headers, body, or token."""
    ctx = ssl.create_default_context()
    req = Request(url, headers=headers, method="GET")
    try:
        with urlopen(req, timeout=timeout, context=ctx) as resp:  # noqa: S310 - operator URL
            return int(resp.getcode() or 200), resp.read()
    except HTTPError as e:
        body = b""
        try:
            body = e.read() or b""
        except Exception:  # noqa: BLE001
            body = b""
        return int(e.code), body
    except TimeoutError as e:
        raise ProducerTransportError(PRODUCER_TIMEOUT) from e
    except URLError as e:
        reason = e.reason
        if isinstance(reason, TimeoutError) or "timed out" in str(e).lower():
            raise ProducerTransportError(PRODUCER_TIMEOUT) from e
        raise ProducerTransportError(PRODUCER_UNREACHABLE) from e
    except OSError as e:
        raise ProducerTransportError(PRODUCER_UNREACHABLE) from e


def _settings_attr(name: str, default=""):
    try:
        from ..config import settings
        return getattr(settings, name, default)
    except Exception:  # noqa: BLE001
        return default


def producer_url(*, url: str | None = None, base_url: str | None = None,
                 path: str | None = None, organization_id: str | None = None) -> str:
    full = (url if url is not None else _settings_attr("warmbly_sales_context_url", "")).strip()
    if full:
        target = full
    else:
        base = (base_url if base_url is not None else _settings_attr("warmbly_base_url", "")).strip().rstrip("/")
        if not base:
            return ""
        rel = (path if path is not None else _settings_attr("warmbly_sales_context_path", "/confenge/sales-context")) or "/confenge/sales-context"
        if not rel.startswith("/"):
            rel = "/" + rel
        target = base + rel
    org = organization_id if organization_id is not None else _settings_attr("warmbly_organization_id", "")
    org = (org or "").strip()
    if org:
        p = urlsplit(target)
        q = dict(parse_qsl(p.query, keep_blank_values=True))
        q.setdefault("organization_id", org)
        target = urlunsplit((p.scheme, p.netloc, p.path, urlencode(q), p.fragment))
    return target


def producer_configured(*, url: str | None = None, base_url: str | None = None) -> bool:
    return bool(producer_url(url=url, base_url=base_url))


@dataclass
class RefreshResult:
    ok: bool
    reason: str = ""
    accepted: int = 0
    refused: int = 0
    schema: str | None = None
    configured: bool = False
    conversations: list = field(default_factory=list)

    def as_http(self) -> dict:
        return {
            "ok": self.ok,
            "reason": self.reason,
            "accepted": self.accepted,
            "refused": self.refused,
            "schema": self.schema,
            "configured": self.configured,
            "conversations": self.conversations,
            "fetch": get_fetch_state(),
        }


_fetch_state: dict[str, Any] = {
    "configured": False,
    "ok": False,
    "reason": PRODUCER_NOT_CONFIGURED,
    "at": None,
    "accepted": 0,
    "schema": None,
}


def get_fetch_state() -> dict:
    """Public fetch status. Never includes token, URL userinfo, or payload."""
    return {
        "configured": bool(_fetch_state.get("configured")),
        "ok": bool(_fetch_state.get("ok")),
        "reason": _fetch_state.get("reason") or "",
        "at": _fetch_state.get("at"),
        "accepted": int(_fetch_state.get("accepted") or 0),
        "schema": _fetch_state.get("schema"),
    }


def reset_fetch_state() -> None:
    _fetch_state.update(
        configured=False, ok=False, reason=PRODUCER_NOT_CONFIGURED,
        at=None, accepted=0, schema=None,
    )


def list_conversations(store: ReceiptStore | None = None) -> list[dict]:
    """Short accepted list for the copilot picker. Not a CRM."""
    store = store if store is not None else get_store()
    rows: list[tuple[str, dict]] = []
    for rec in store.records():
        conv = rec.conversation or {}
        rows.append((rec.accepted_at, {
            "handraiser_id": rec.handraiser_id,
            "session_id": rec.session_id,
            "titulo": conv.get("resumo") or conv.get("empresa") or UNKNOWN,
            "resumo": conv.get("resumo") or UNKNOWN,
            "nucleo": conv.get("nucleo") or UNKNOWN,
            "empresa": conv.get("empresa") or UNKNOWN,
            "canal": conv.get("canal") or conv.get("lane") or SOURCE_LANE,
            "intencao": conv.get("intencao") or UNKNOWN,
            "inbound_only": rec.inbound_only,
            "freshness": conv.get("freshness") or UNKNOWN,
            "status": conv.get("status") or DECISION_ACCEPTED,
            "version": rec.version,
        }))
    rows.sort(key=lambda x: x[0], reverse=True)
    return [row for _, row in rows]


def consume_export(
    payload,
    *,
    enabled: bool = True,
    store: ReceiptStore | None = None,
    now: datetime | None = None,
    bind_session: bool = False,
) -> RefreshResult:
    """Admit items from a canonical export. Mis-tagged collection fails closed."""
    store = store if store is not None else get_store()
    now = now or datetime.now(timezone.utc)
    conversations = lambda: list_conversations(store)
    if not enabled:
        return RefreshResult(ok=False, reason=CONSUMER_DISABLED, conversations=conversations())
    doc = _unwrap(payload)
    if not isinstance(doc, dict):
        return RefreshResult(ok=False, reason=MALFORMED, conversations=conversations())
    if is_collection(doc):
        tag = doc.get("schema")
        if tag != SCHEMA_EXPORT:
            log.info("handraiser export refused reason=%s", SCHEMA_MISMATCH_COLLECTION)
            return RefreshResult(
                ok=False, reason=SCHEMA_MISMATCH_COLLECTION, schema=str(tag) if tag else None,
                conversations=conversations(),
            )
        items = doc.get("items") if isinstance(doc.get("items"), list) else []
        accepted = 0
        refused = 0
        for it in items:
            result = consume(it, enabled=True, store=store, now=now, bind_session=bind_session)
            if result.ok:
                accepted += 1
            else:
                refused += 1
        return RefreshResult(
            ok=True, reason="", accepted=accepted, refused=refused,
            schema=SCHEMA_EXPORT, conversations=conversations(),
        )
    result = consume(doc, enabled=True, store=store, now=now, bind_session=bind_session)
    if result.ok:
        return RefreshResult(
            ok=True, reason="", accepted=1, refused=0,
            schema=_str(doc.get("schema")) or None, conversations=conversations(),
        )
    return RefreshResult(
        ok=False, reason=result.reason, accepted=0, refused=1,
        schema=_str(doc.get("schema")) or None, conversations=conversations(),
    )


def refresh_conversations(
    *,
    enabled: bool = True,
    store: ReceiptStore | None = None,
    now: datetime | None = None,
    url: str | None = None,
    token: str | None = None,
    timeout: float | None = None,
    retries: int | None = None,
    transport: Callable[..., tuple[int, bytes]] | None = None,
    organization_id: str | None = None,
    base_url: str | None = None,
    bind_session: bool = False,
) -> RefreshResult:
    """Explicit producer pull. Never called per copilot orientation.

    Last-known-good is the in-memory store: a failed fetch does not wipe
    already-accepted conversations. Nothing is written to disk.
    """
    store = store if store is not None else get_store()
    now = now or datetime.now(timezone.utc)
    target = producer_url(url=url, base_url=base_url, organization_id=organization_id)
    tok = token if token is not None else _settings_attr("warmbly_token", "")
    configured = bool(target)
    _fetch_state["configured"] = configured
    _fetch_state["at"] = now.isoformat()

    if not enabled:
        _fetch_state.update(ok=False, reason=CONSUMER_DISABLED, schema=None)
        return RefreshResult(
            ok=False, reason=CONSUMER_DISABLED, configured=configured,
            conversations=list_conversations(store),
        )
    if not target:
        _fetch_state.update(ok=False, reason=PRODUCER_NOT_CONFIGURED, schema=None)
        return RefreshResult(
            ok=False, reason=PRODUCER_NOT_CONFIGURED, configured=False,
            conversations=list_conversations(store),
        )

    if timeout is None:
        try:
            timeout = float(_settings_attr("warmbly_fetch_timeout_s", 3.0) or 3.0)
        except (TypeError, ValueError):
            timeout = 3.0
    if retries is None:
        try:
            retries = int(_settings_attr("warmbly_fetch_retries", 1) or 0)
        except (TypeError, ValueError):
            retries = 1
    transport = transport or default_transport
    attempts = max(1, int(retries) + 1)
    headers = producer_headers(str(tok or ""))
    last_reason = PRODUCER_UNREACHABLE
    status = None
    raw = b""
    for _ in range(attempts):
        try:
            status, raw = transport(target, headers, float(timeout))
        except ProducerTransportError as e:
            last_reason = e.reason
            if e.reason not in (PRODUCER_TIMEOUT, PRODUCER_UNREACHABLE, PRODUCER_ERROR):
                break
            continue
        if status in (401, 403):
            last_reason = PRODUCER_UNAUTHORIZED
            break
        if status is not None and status >= 500:
            last_reason = PRODUCER_ERROR
            continue
        if status != 200:
            last_reason = PRODUCER_ERROR
            break
        last_reason = ""
        break

    log.info("handraiser producer fetch reason=%s status=%s url=%s",
             last_reason or "ok", status, _redact_url(target))
    if last_reason:
        _fetch_state.update(ok=False, reason=last_reason, schema=None)
        return RefreshResult(
            ok=False, reason=last_reason, configured=True,
            conversations=list_conversations(store),
        )
    try:
        payload = json.loads((raw or b"").decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        _fetch_state.update(ok=False, reason=MALFORMED, schema=None)
        return RefreshResult(
            ok=False, reason=MALFORMED, configured=True,
            conversations=list_conversations(store),
        )

    result = consume_export(payload, enabled=True, store=store, now=now, bind_session=bind_session)
    result.configured = True
    _fetch_state.update(
        ok=result.ok, reason=result.reason, accepted=result.accepted, schema=result.schema,
    )
    return result


def refresh_from_producer(handraiser_id: str, *, store: ReceiptStore | None = None,
                          url: str | None = None, timeout: float = 3.0,
                          transport: Callable[..., tuple[int, bytes]] | None = None,
                          token: str | None = None) -> str:
    """Re-read one item. Collection is never treated as a dossier.

    A mis-tagged collection (Warmbly still labeling the export as the dossier
    schema) fails closed with SCHEMA_MISMATCH_COLLECTION — items are not picked
    out of a colliding envelope. Canonical SCHEMA_EXPORT may yield the matching
    item. Returns a reason string, or empty on success / when no URL is set.
    """
    store = store if store is not None else get_store()
    target = producer_url(url=url)
    if not target:
        return ""
    target = target.replace("{id}", handraiser_id).replace("{action_id}", handraiser_id)
    tok = token if token is not None else _settings_attr("warmbly_token", "")
    try:
        status, raw = (transport or default_transport)(
            target, producer_headers(str(tok or "")), timeout,
        )
    except ProducerTransportError as e:
        log.info("handraiser producer unread reason=%s", e.reason)
        return e.reason
    if status in (401, 403):
        return PRODUCER_UNAUTHORIZED
    if status != 200:
        return PRODUCER_ERROR if (status or 0) >= 500 else PRODUCER_UNREACHABLE
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return SCHEMA_MISMATCH
    doc = _unwrap(payload)
    if is_collection(doc) and isinstance(doc, dict):
        if doc.get("schema") != SCHEMA_EXPORT:
            log.info("handraiser producer collection refused reason=%s", SCHEMA_MISMATCH_COLLECTION)
            return SCHEMA_MISMATCH_COLLECTION
        items = doc.get("items") if isinstance(doc.get("items"), list) else []
        match = None
        for it in items:
            if not isinstance(it, dict):
                continue
            if _str(it.get("action_id")) == handraiser_id or _str(it.get("handraiser_id")) == handraiser_id:
                match = it
                break
        if match is None:
            log.info("handraiser producer collection had no item")
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
