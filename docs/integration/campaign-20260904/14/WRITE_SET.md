# Campaign 14 — WRITE_SET / DO_NOT_TOUCH_SET

CAMPAIGN_ID=14
REPOSITORY=tjsasakifln/meetcfg
BRANCH=feat/campaign-20260904-multivertical-handraiser-handoff-v3
WORKTREE=/home/tjsasakifln/code/.worktrees/meetcfg/c20260904-14-meetcfg-handoff
BASE_SHA=26f79e3fd414b12e765b6ec243e47a90c8a9b226
OWNER=Meetcfg consumer/view only. Does not create lead, account, pipeline, queue, cadence, offer truth, or outcome.

## WRITE_SET

- backend/app/copilot/handraiser.py
- backend/app/main.py
- backend/tools/handraiser_selftest.py
- frontend/index.html
- frontend/static/app.js
- frontend/static/style.css
- fixtures/handraiser/accepted.json
- fixtures/handraiser/accepted_inbound_only.json
- fixtures/handraiser/accepted_other.json
- fixtures/handraiser/cnpj_is_ref.json
- fixtures/handraiser/rejected.json
- fixtures/handraiser/unknown.json
- fixtures/handraiser/stale_freshness.json
- fixtures/handraiser/invalid_freshness.json
- fixtures/handraiser/unpinned_legacy.json
- fixtures/handraiser/missing_hash.json
- fixtures/handraiser/pin_mismatch.json
- fixtures/handraiser/missing_conflict.json
- fixtures/handraiser/unknown_nucleus.json
- fixtures/handraiser/export_pinned.json
- fixtures/handraiser/nucleus_expert_evidence_assistance.json
- fixtures/handraiser/nucleus_property_valuation.json
- fixtures/handraiser/nucleus_building_engineering_documentation.json
- fixtures/handraiser/nucleus_occupational_safety.json
- fixtures/handraiser/nucleus_public_works_b2g.json
- docs/integration/campaign-20260904/14/WRITE_SET.md
- docs/integration/campaign-20260904/14/warmbly-context-pin.md
- docs/integration/campaign-20260904/14/governance-admission-pin.md

Unchanged fixtures left in place on purpose (fail-closed evidence of current producer shape):

- fixtures/handraiser/native_warmbly_item.json
- fixtures/handraiser/export_valid.json
- fixtures/handraiser/schema_drift.json
- fixtures/handraiser/schema_collision_collection.json
- fixtures/handraiser/schema_mismatch_export.json
- fixtures/handraiser/malformed.json

## DO_NOT_TOUCH_SET

- package.json, lockfiles, `.github/**`, Makefile, global scripts, `backend/requirements.txt`
- `backend/app/copilot/context.py` (copilot dossier `CONFENGE_SALES_CONTEXT/1.0` remains the only dossier schema)
- `backend/app/copilot/engine.py` (loader is not replaced with a second commercial truth)
- `backend/app/config.py` (`HANDRAISER_CONSUMER_ENABLED` already exists)
- `backend/app/meeting.py`
- sister campaign worktrees and branches
