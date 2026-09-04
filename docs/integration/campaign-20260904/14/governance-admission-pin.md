# Fragment — Governance admission version pin

- campaign_id: 14
- owner: Governance (issue #65 / campaign 06); ratify in goal 97/99
- target_path: Governance admission envelope (not edited in meetcfg)
- operation: emit
- stable_key: NET_NEW_INBOUND_HANDRAISER/1.0.0-draft.20260904
- dependency: Meetcfg consume requires this policy id on accepted inbound; missing/divergent pin fails closed
- test: `python backend/tools/handraiser_selftest.py` (`accepted_inbound_only.json`, `rejected.json`, `unknown.json`, `missing_conflict.json`)
- rollback: Meetcfg kill switch `HANDRAISER_CONSUMER_ENABLED=false`

Admission must carry:

- `schema_version` / `policy_id`: `NET_NEW_INBOUND_HANDRAISER/1.0.0-draft.20260904`
- `decision`: `ACCEPTED` | `REJECTED_WITH_REASON` | `UNKNOWN`
- `origin`: `CONFENGE_WEB`
- `inbound_only`: `true` for net-new
- `outbound_eligible`: `false` (never inferred true)
- `auto_send`: `false`
- conflict clearance on the context envelope (`conflict.status` present). Missing clearance is `MISSING_CONFLICT_CLEARANCE` in Meetcfg and creates no session.

State vocabulary, when present, is `CONFENGE_HANDRAISER_STATE/1.0.0-draft.20260904`. Meetcfg does not become the admission authority.
