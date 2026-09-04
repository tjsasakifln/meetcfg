# Fragment — Warmbly must emit the Meetcfg context pin

- campaign_id: 14
- owner: Warmbly producer (issue #47 / campaign 07); ratify in goal 97/99
- target_path: Warmbly `GET /confenge/sales-context` item/export (not edited in meetcfg)
- operation: emit
- stable_key: MEETCFG_HANDRAISER_CONTEXT/1.0.0-draft.20260904
- dependency: Meetcfg consume path pins this contract + hash and fail-closes otherwise
- test: `python backend/tools/handraiser_selftest.py` cases `unpinned_legacy.json`, `native_warmbly_item.json`, `export_valid.json`, `export_pinned.json`
- rollback: `HANDRAISER_CONSUMER_ENABLED=false` on Meetcfg; existing receipts stay readable

Meetcfg does not copy the producer schema into a second authority. Runtime refuses unpinned native `SalesContextItem` and the current Warmbly export (`CONFENGE_SALES_CONTEXT_EXPORT/1.0` items without pin). Fixtures are test-only and are never a runtime fallback.

Producer payload (accepted item) must include:

- `schema`: `MEETCFG_HANDRAISER_CONTEXT/1.0.0-draft.20260904`
- `schema_hash`: `300a5970bbb5fc9c50682b67911e4fd067839e2c0c7de5fbc05ed05a076bffd5` (sha256 of the canonical contracts map below)
- `contracts.context`: `MEETCFG_HANDRAISER_CONTEXT/1.0.0-draft.20260904`
- `contracts.admission`: `NET_NEW_INBOUND_HANDRAISER/1.0.0-draft.20260904`
- `contracts.state`: `CONFENGE_HANDRAISER_STATE/1.0.0-draft.20260904`
- `contracts.taxonomy`: `CONFENGE_CORPORATE_TAXONOMY/1.0.0-draft.20260904`
- `contracts.catalog`: `CONFENGE_OFFER_CATALOG/2.0.0-draft.20260904`
- `contracts.intake`: `CONFENGE_WEB_INTAKE/2.0.0-draft.20260904`
- `source` / `lane`: `CONFENGE_WEB`
- `decision`: `ACCEPTED`
- `nucleus_id`: one of `expert_evidence_assistance`, `property_valuation`, `building_engineering_documentation`, `occupational_safety`, `public_works_b2g`
- `offer_candidate`: `private_project_technical_readiness_assessment` when present
- `conflict.status`: `CLEAR` or `RESTRICTED` (class only; no protected detail)
- `outbound_eligible`: `false`
- `auto_send`: `false`

Canonical contracts JSON (sort_keys, separators `,:`):

```json
{"admission":"NET_NEW_INBOUND_HANDRAISER/1.0.0-draft.20260904","catalog":"CONFENGE_OFFER_CATALOG/2.0.0-draft.20260904","context":"MEETCFG_HANDRAISER_CONTEXT/1.0.0-draft.20260904","intake":"CONFENGE_WEB_INTAKE/2.0.0-draft.20260904","state":"CONFENGE_HANDRAISER_STATE/1.0.0-draft.20260904","taxonomy":"CONFENGE_CORPORATE_TAXONOMY/1.0.0-draft.20260904"}
```
