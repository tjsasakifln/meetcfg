#!/usr/bin/env python3
"""Drive the shipped hand-raiser consume/validate/session-bind path.

No Codex, no whisper. Failures are the expected outcome for rejected payloads.

Usage:
    python3 backend/tools/handraiser_selftest.py
    python3 backend/tools/handraiser_selftest.py --launch 1
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
os.environ.setdefault("HANDRAISER_CONSUMER_ENABLED", "true")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import reload as reload_settings  # noqa: E402
reload_settings()

from app.copilot.context import (  # noqa: E402
    SCHEMA_EXPORT, SCHEMA_ID, is_collection, looks_like_cnpj, render_for_prompt,
)
from app.copilot.engine import INSTRUCTION, load_structured_context  # noqa: E402
from app.copilot.handraiser import (  # noqa: E402
    CONSUMER_DISABLED, FRESHNESS_INVALID, FRESHNESS_STALE, MALFORMED,
    MISSING_IDENTITY, OVERSIZED, REJECTED_WITH_REASON, SCHEMA_MISMATCH,
    SCHEMA_MISMATCH_COLLECTION, UNKNOWN_OUTCOME, WARMBLY_ITEM_KEYS,
    ReceiptStore, assert_no_invented_fields, classify_payload, consume,
    get_store, prompt_safety_ok, render_conversation_layer, reset_store,
    session_id_for,
)

REPO = Path(__file__).resolve().parents[2]
FIXTURES = REPO / "fixtures" / "handraiser"
FRONTEND_JS = REPO / "frontend" / "static" / "app.js"
FRONTEND_HTML = REPO / "frontend" / "index.html"

FAILS = 0
NOW = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)

UX_FIELDS = (
    "empresa",
    "por que chegou agora",
    "intenção",
    "fatos verificáveis",
    "o que NÃO sabemos",
    "oportunidade/contrato relevante",
    "último touch/outcome",
    "próximo estado comercial",
)

PII_SAMPLES = (
    "visitor@example.com",
    "+5511999999999",
    "Visitante Exemplo",
    "123.456.789-00",
    "raw_message_secret_body",
)


def check(name, got, want=True):
    global FAILS
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {name}: got {got!r}, want {want!r}")
    FAILS += 0 if ok else 1
    return ok


def load_fx(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


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


def test_classify_and_collection_collision():
    print("schema collision: collection is never a dossier")
    export = load_fx("schema_mismatch_export.json")
    collision = load_fx("schema_collision_collection.json")
    check("export classified collection", classify_payload(export), "collection")
    check("mis-tagged Warmbly export classified collection",
          classify_payload(collision), "collection")
    check("is_collection(export)", is_collection(export), True)
    check("is_collection(collision) despite dossier tag", is_collection(collision), True)
    check("export schema is distinct", export["schema"], SCHEMA_EXPORT)
    check("collision tag equals dossier id (producer pitfall)",
          collision["schema"], SCHEMA_ID)
    r1 = consume(export, store=ReceiptStore(), now=NOW, bind_session=False)
    r2 = consume(collision, store=ReceiptStore(), now=NOW, bind_session=False)
    check("export fail-closed", r1.ok, False)
    check("export reason", r1.reason, SCHEMA_MISMATCH_COLLECTION)
    check("export no session", r1.session_id, None)
    check("collision fail-closed", r2.ok, False)
    check("collision reason", r2.reason, SCHEMA_MISMATCH_COLLECTION)
    check("collision no session", r2.session_id, None)


def test_accepted():
    print("accepted wrap → one session, context loaded")
    store = ReceiptStore()
    payload = load_fx("accepted.json")
    result = consume(payload, store=store, now=NOW, bind_session=True)
    check("accepted ok", result.ok, True)
    check("accepted reason empty", result.reason, "")
    check("handraiser_id", result.handraiser_id, "11111111-1111-1111-1111-111111111111")
    check("session id", result.session_id,
          session_id_for("11111111-1111-1111-1111-111111111111"))
    check("company display", result.conversation["empresa"],
          "Vertice Obras e Infraestrutura Ltda")
    check("intent", result.conversation["intencao"], "REQUEST_DEEP_DIVE")
    check("next state", result.conversation["proximo_estado_comercial"],
          "fechar o escopo do primeiro ciclo")
    check("inbound_only preserved", result.inbound_only, True)
    check("inbound_only on dossier", result.dossier.get("inbound_only"), True)
    check("company_ref is not CNPJ",
          looks_like_cnpj(result.dossier["company"].get("cnpj")), False)
    check("cnpj absent", result.dossier["company"].get("cnpj"), None)
    check("identity_ref kept", result.dossier["company"].get("identity_ref"), "vertice-obras")
    check("store size 1", len(store), 1)
    check("CNPJ listed as unknown", "CNPJ" in result.conversation["o_que_nao_sabemos"], True)
    check("no invented commercial fields", assert_no_invented_fields(result.dossier), [])
    # producer confidence must not become win chance
    blob = json.dumps(result.conversation, ensure_ascii=False).lower()
    check("conversation has no win probability", "probabilidade" in blob, False)
    check("confidence not copied as fact",
          any("historical_fit" in str(x) for x in result.dossier.get("public_facts") or []),
          False)
    return result


def test_inbound_only_net_new():
    print("net-new inbound-only admission (no account, no CNPJ)")
    store = ReceiptStore()
    result = consume(load_fx("accepted_inbound_only.json"), store=store, now=NOW, bind_session=True)
    check("net-new ok", result.ok, True)
    check("inbound_only", result.inbound_only, True)
    check("company not invented", result.conversation["empresa"], "UNKNOWN")
    check("cnpj not invented", result.dossier["company"].get("cnpj"), None)
    check("no cargo key", "cargo" in result.dossier, False)
    check("no decisor key", "decisor" in result.dossier, False)
    check("no chance key", "chance" in result.dossier, False)
    check("outbound_eligible not granted",
          result.dossier.get("outbound_eligible") in (None, False), True)


def test_fail_closed():
    print("rejected / UNKNOWN / stale / invalid freshness / drift fail closed")
    for name, reason in (
        ("rejected.json", REJECTED_WITH_REASON),
        ("unknown.json", UNKNOWN_OUTCOME),
        ("stale_freshness.json", FRESHNESS_STALE),
        ("invalid_freshness.json", FRESHNESS_INVALID),
        ("schema_drift.json", SCHEMA_MISMATCH),
        ("malformed.json", MALFORMED),
    ):
        store = ReceiptStore()
        result = consume(load_fx(name), store=store, now=NOW, bind_session=True)
        check(f"{name} refused", result.ok, False)
        check(f"{name} reason", result.reason, reason)
        check(f"{name} no session", result.session_id, None)
        check(f"{name} store empty", len(store), 0)


def test_replay_and_update():
    print("replay 100× same id → one logical session; new receipt updates in place")
    store = ReceiptStore()
    payload = load_fx("accepted.json")
    first = consume(payload, store=store, now=NOW, bind_session=True)
    ids = {first.session_id}
    replayed = 0
    for _ in range(99):
        r = consume(payload, store=store, now=NOW, bind_session=True)
        ids.add(r.session_id)
        replayed += 1 if r.ok and r.replayed and r.session_id == first.session_id else 0
    check("99 replays ok and same session", replayed, 99)
    check("100× unique session count", len(ids), 1)
    check("store still 1", len(store), 1)
    check("version still 1 after replay", store.get(first.handraiser_id).version, 1)

    updated_payload = json.loads(json.dumps(payload))
    updated_payload["admission"]["receipt_id"] = "rcpt_vertice_webinar_002"
    updated_payload["item"]["facts"]["why_now"] = "segunda mensagem: pediu proposta"
    updated_payload["item"]["next_action_type"] = "enviar proposta datada"
    second = consume(updated_payload, store=store, now=NOW, bind_session=True)
    check("update ok", second.ok, True)
    check("update same session", second.session_id, first.session_id)
    check("update not a second conversation", len(store), 1)
    check("version bumped", second.version, 2)
    check("context refreshed",
          second.conversation["proximo_estado_comercial"], "enviar proposta datada")
    check("why now refreshed",
          "segunda mensagem" in second.conversation["por_que_chegou_agora"], True)


def test_cnpj_ref_and_native():
    print("company_ref stuffed into cnpj is not CNPJ; native Warmbly item maps")
    store = ReceiptStore()
    stuffed = consume(load_fx("cnpj_is_ref.json"), store=store, now=NOW, bind_session=False)
    check("stuffed ref accepted as dossier", stuffed.ok, True)
    check("cnpj cleared", stuffed.dossier["company"].get("cnpj"), None)
    check("ref preserved as identity",
          stuffed.dossier["company"].get("identity_ref"), "acme-holdings")
    check("render does not say CNPJ acme",
          "CNPJ acme-holdings" in render_for_prompt(stuffed.dossier), False)

    native_payload = load_fx("native_warmbly_item.json")
    extra_keys = [k for k in native_payload if k not in WARMBLY_ITEM_KEYS]
    check("native fixture keys ⊆ Warmbly SalesContextItem", extra_keys, [])
    native = consume(native_payload, store=ReceiptStore(), now=NOW, bind_session=False)
    check("native item ok", native.ok, True)
    check("native company", native.conversation["empresa"],
          "Acme Holdings Engenharia Ltda")
    check("native channel mapped inbound",
          native.dossier["acquisition_channel"], "INBOUND_LIVE")
    check("native lane preserved", native.dossier["lane"], "confenge_web")
    check("native cnpj not invented", native.dossier["company"].get("cnpj"), None)
    check("native ref", native.dossier.get("identity_ref"), "acme-holdings")
    check("native inbound_only not invented", "inbound_only" in native.dossier, False)
    check("native person_name is not empresa",
          native.conversation["empresa"] == native_payload.get("person_name"), False)
    check("native person_name not cargo/decisor",
          "cargo" in native.dossier or "decisor" in native.dossier, False)
    check("native no invented fields", assert_no_invented_fields(native.dossier), [])
    check("native confidence not a public fact",
          any("historical_fit" in str(x) for x in native.dossier.get("public_facts") or []),
          False)

    wrapped_item = consume({"data": native_payload}, store=ReceiptStore(), now=NOW,
                           bind_session=False)
    check("HTTP {data: native item} ok", wrapped_item.ok, True)
    check("HTTP wrap same company", wrapped_item.conversation["empresa"],
          "Acme Holdings Engenharia Ltda")

    nil_account = json.loads(json.dumps(native_payload))
    nil_account["account_id"] = "00000000-0000-0000-0000-000000000000"
    nil_res = consume(nil_account, store=ReceiptStore(), now=NOW, bind_session=False)
    check("nil UUID account omitted", "account_id" in (nil_res.dossier or {}), False)


def test_adversarial():
    print("adversarial malformed / oversized / duplicate / missing identity")
    store = ReceiptStore()
    r = consume(["not", "an", "object"], store=store, now=NOW, bind_session=False)
    check("list payload malformed", r.reason, MALFORMED)
    r = consume("nope", store=store, now=NOW, bind_session=False)
    check("string payload malformed", r.reason, MALFORMED)
    r = consume({"schema": "CONFENGE_HANDRAISER_ITEM/1.0"}, store=store, now=NOW,
                bind_session=False)
    check("empty wrap missing identity", r.reason in (MALFORMED, MISSING_IDENTITY), True)
    r = consume(load_fx("accepted.json"), store=store, now=NOW, raw_size=300_000,
                bind_session=False)
    check("oversized", r.reason, OVERSIZED)
    check("oversized no session", r.session_id, None)

    dup_store = ReceiptStore()
    a = consume(load_fx("accepted.json"), store=dup_store, now=NOW, bind_session=False)
    b = consume(load_fx("accepted.json"), store=dup_store, now=NOW, bind_session=False)
    check("duplicate replayed", b.replayed, True)
    check("duplicate same session", b.session_id, a.session_id)

    wrapped = {"data": load_fx("schema_collision_collection.json")}
    r = consume(wrapped, store=ReceiptStore(), now=NOW, bind_session=False)
    check("HTTP {data: collection} refused", r.reason, SCHEMA_MISMATCH_COLLECTION)


def test_prompt_and_ux():
    print("prompt/render: NÃO AFIRME / o que não sabemos; no legal-enablement")
    result = consume(load_fx("accepted.json"), store=ReceiptStore(), now=NOW, bind_session=False)
    rendered = render_for_prompt(result.dossier)
    check("render has NÃO AFIRME", "NÃO AFIRME" in rendered, True)
    check("render has o que NÃO sabemos", "O que NÃO sabemos" in rendered, True)
    check("render inbound-only", "inbound-only" in rendered.lower(), True)
    check("prompt safety on render", prompt_safety_ok(rendered), True)
    check("prompt safety on instruction", prompt_safety_ok(INSTRUCTION), True)
    check("catches affirmative fit→legal",
          prompt_safety_ok("fit histórico é habilitação jurídica neste caso"), False)
    check("catches numeric win chance",
          prompt_safety_ok("probabilidade de vitória: 80"), False)
    check("instruction names habilitação legal as forbidden",
          "habilitação legal" in INSTRUCTION, True)
    check("instruction names probabilidade de vitória as forbidden",
          "probabilidade de vitória" in INSTRUCTION, True)
    # The instruction forbids converting fit into those claims; it must not
    # assert that historical fit IS legal enablement.
    check("instruction does not equate fit with legal enablement",
          "fit histórico" in INSTRUCTION.lower() and "não são habilitação legal" in INSTRUCTION.lower(),
          True)

    conv = render_conversation_layer(result.dossier)
    for key in ("empresa", "por_que_chegou_agora", "intencao", "fatos_verificaveis",
                "o_que_nao_sabemos", "oportunidade_contrato", "ultimo_touch_outcome",
                "proximo_estado_comercial"):
        check(f"conversation layer has {key}", key in conv, True)

    js = FRONTEND_JS.read_text(encoding="utf-8")
    html = FRONTEND_HTML.read_text(encoding="utf-8")
    src = js + "\n" + html
    for label in UX_FIELDS:
        check(f"UX source contains {label!r}", label in src, True)
    check("app.js is not a node module", "module.exports" in js, False)
    check("app.js has no require(", "require(" in js, False)
    # plain script src
    check("index uses script src", '<script src="/static/app.js"></script>' in html, True)
    return rendered


def test_kill_switch_and_pii():
    print("kill-switch preserves receipts; logs have no PII")
    store = get_store() if False else ReceiptStore()
    accepted = consume(load_fx("accepted.json"), store=store, now=NOW, bind_session=True)
    check("pre-disable accepted", accepted.ok, True)

    dirty = json.loads(json.dumps(load_fx("accepted.json")))
    dirty["item"]["person_name"] = "Visitante Exemplo"
    dirty["item"]["email"] = "visitor@example.com"
    dirty["item"]["phone"] = "+5511999999999"
    dirty["item"]["cpf"] = "123.456.789-00"
    dirty["item"]["raw_message"] = "raw_message_secret_body"
    buf, handler, root = _capture_logs()
    consume(dirty, store=store, now=NOW, bind_session=False)
    refused = consume(dirty, store=store, now=NOW, enabled=False, bind_session=False)
    text = _drop_logs(buf, handler, root)
    check("disabled refuse", refused.ok, False)
    check("disabled reason", refused.reason, CONSUMER_DISABLED)
    still = store.get(accepted.handraiser_id)
    check("prior receipt still stored", still is not None, True)
    check("prior session still the same", still.session_id, accepted.session_id)
    pii_present = [s for s in PII_SAMPLES if s in text]
    check("application log omits email/phone/name/cpf/raw_message", pii_present, [])

    # session still sees the bound dossier via shipped loader
    from app.meeting import get_or_create
    session = get_or_create(accepted.session_id)
    session.handraiser_id = accepted.handraiser_id
    session.handraiser_context = accepted.dossier
    loaded = load_structured_context(session)
    check("loader returns bound dossier", loaded is not None, True)
    check("loader company", (loaded or {}).get("company", {}).get("name"),
          "Vertice Obras e Infraestrutura Ltda")


def test_http(label: str, out_path: str | None = None) -> dict:
    """Launch the shipped FastAPI app via TestClient (no mock app)."""
    print(f"HTTP launch {label}: shipped app.main:app")
    reset_store()
    from fastapi.testclient import TestClient
    from app.main import app

    client = TestClient(app)
    accepted = load_fx("accepted.json")
    r1 = client.post("/api/handraiser/ingest", json=accepted)
    body = r1.json()
    print(f"  ingest status={r1.status_code} body_keys={sorted(body)}")
    if out_path:
        Path(out_path).write_text(json.dumps(body, ensure_ascii=False, indent=2) + "\n",
                                  encoding="utf-8")
        print(f"  wrote {out_path}")
    check("http accepted 200", r1.status_code, 200)
    check("http ok", body.get("ok"), True)
    check("http company in body", body.get("empresa"),
          "Vertice Obras e Infraestrutura Ltda")
    check("http intent in body", body.get("intencao"), "REQUEST_DEEP_DIVE")
    check("http next state in body", body.get("proximo_estado_comercial"),
          "fechar o escopo do primeiro ciclo")
    sid = body.get("session_id")
    hid = body.get("handraiser_id")

    r2 = client.post("/api/handraiser/ingest", json=accepted)
    body2 = r2.json()
    check("http replay same session", body2.get("session_id"), sid)
    check("http replay ok", body2.get("ok"), True)

    got = client.get(f"/api/handraiser/{hid}")
    check("http get accepted", got.status_code, 200)
    check("http get company", got.json().get("empresa"),
          "Vertice Obras e Infraestrutura Ltda")

    ctx = client.get(f"/api/session/context?meeting={sid}")
    check("http session context 200", ctx.status_code, 200)
    cbody = ctx.json()
    check("http session empresa", cbody.get("empresa"),
          "Vertice Obras e Infraestrutura Ltda")
    check("http session next state", cbody.get("proximo_estado_comercial"),
          "fechar o escopo do primeiro ciclo")

    for name, reason in (
        ("rejected.json", REJECTED_WITH_REASON),
        ("unknown.json", UNKNOWN_OUTCOME),
        ("schema_collision_collection.json", SCHEMA_MISMATCH_COLLECTION),
        ("schema_mismatch_export.json", SCHEMA_MISMATCH_COLLECTION),
        ("schema_drift.json", SCHEMA_MISMATCH),
        ("malformed.json", MALFORMED),
        ("invalid_freshness.json", FRESHNESS_INVALID),
    ):
        resp = client.post("/api/handraiser/ingest", json=load_fx(name))
        b = resp.json()
        check(f"http {name} not ok", b.get("ok"), False)
        check(f"http {name} reason", b.get("reason"), reason)
        check(f"http {name} no session", b.get("session_id"), None)

    # rollback: disable consumer, prior readable, new ingest refused
    from app.config import settings
    settings.handraiser_consumer_enabled = False
    try:
        refused = client.post("/api/handraiser/ingest", json=load_fx("native_warmbly_item.json"))
        check("http disabled reason", refused.json().get("reason"), CONSUMER_DISABLED)
        check("http disabled no session", refused.json().get("session_id"), None)
        still = client.get(f"/api/handraiser/{hid}")
        check("http prior still readable", still.status_code, 200)
        check("http prior company remains", still.json().get("empresa"),
              "Vertice Obras e Infraestrutura Ltda")
    finally:
        settings.handraiser_consumer_enabled = True
    return body


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
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
        test_http(label, out_path=out_path)
        if FAILS:
            print(f"\n{FAILS} FAILURES")
            return 1
        print("\nLAUNCH PASS")
        return 0

    tests = [
        test_classify_and_collection_collision,
        test_accepted,
        test_inbound_only_net_new,
        test_fail_closed,
        test_replay_and_update,
        test_cnpj_ref_and_native,
        test_adversarial,
        test_prompt_and_ux,
        test_kill_switch_and_pii,
        lambda: test_http("selftest"),
    ]
    for fn in tests:
        fn()
        print()
    if FAILS:
        print(f"{FAILS} FAILURES")
        return 1
    print("ALL PASS")
    print("SCHEMA_COLLISION=NO")
    print("MANUAL_CONTEXT_REBUILD_REQUIRED=NO")
    print("ACCEPTED_HANDRAISER_VISIBLE=YES")
    print("INBOUND_ONLY_PRESERVED=YES")
    print("REJECTED_UNKNOWN_FAIL_CLOSED=YES")
    print("REPLAY_100X_ONE_LOGICAL_CONTEXT=PASS")
    print("REPLAY_100_ONE_LOGICAL_CONTEXT=PASS")
    print("MEETCFG_HANDRAISER_CONSUMER=GO")
    print("MEETCFG_ISSUE_1=GO")
    return 0


if __name__ == "__main__":
    sys.exit(main())
