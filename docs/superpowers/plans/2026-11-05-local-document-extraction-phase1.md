# Local document extraction phase 1 implementation plan

**Goal:** Build and evaluate a local-only standalone document extraction lab.
**Spec:** ../specs/2026-11-05-local-document-extraction-design.md
**Architecture:** Docling exports block IDs, table structure and page coordinates; local MLX model returns field claims; evidence gate checks quotes and values before acceptance. Existing Apple Vision remains the local scanned-page OCR option.
**Constraints:** No production edits/services/database. Student data and model outputs stay under STAGING_DATA. Downloads cannot access corpus; processing cannot connect to any network. No installation into bundled or production Python. No Git commit/push.

## Tasks
- [x] Set up isolated venv and prefetch Docling + Qwen3 4B/8B 4-bit snapshots, recording exact revisions/checksums.
- [x] Test then implement evidence validation: reject missing evidence, fabricated quote, unsupported value, ambiguous conflict and invalid dates; accept supported complete school names. Files: local_document_lab/evidence.py and tests/test_local_document_evidence.py.
- [x] Implement bounded local document parse and model runners. Files: local_document_lab/parse.py, infer.py, run.py. Runtime JSON stays private under STAGING_DATA.
- [x] Verify deny-network sandbox, original/synthetic regression cases and each downloaded model. Record per-document outputs, abstentions, errors, timing/memory and sample limitations.
- [x] Summarize model comparison and readiness; leave CRM integration and formal blind evaluation for later authorization.

## Review focus
Evidence-matched wrong roles; abbreviated names that remain substrings; multiple course/date rows; empty language fields; corpus instructions overriding extraction system prompt. These are separate from a JSON-schema-valid response and must be checked in evaluation.

## Rulings
User clarified the 30–50 sample target is not required to begin. Existing small corpus is used for exploratory verification only, never production acceptance. Continue independently while the newest source PDF is located.

## Progress and experimental decisions
- Local-only rule appended to migrated adjustment.md with explicit user confirmation.
- Dedicated venv installed: Docling 2.147.0, MLX-LM 0.32.2, MLX 0.32.4, ocrmac 1.0.1; full lock is private under STAGING_DATA.
- Ten original PDFs collected privately, including the user-provided new CoE Cover Letter. Samples are exploratory/reported regressions, not a blind test set.
- OS sandbox external connect attempt returned EPERM. Processing has no network grants; publicly downloaded artifacts are separate from document processing.
- 10 prototype tests passed, including actual over-budget worker termination. Review findings fixed: source/parser cache signatures, ambiguous multipage coordinates, input-size/page limits, process-group RSS monitoring and job deadlines.
- First synthetic 4B check succeeded after correcting a missing public chat-template resource in the downloaded model package; the receipt records the added asset from the pinned model revision.
- Ruling: RSS monitoring is a sampled process-group budget, not a strict instantaneous memory ceiling. Document this limit rather than claim a hard OS memory guarantee.
- First real new-sample 4B extraction returned full institution names/dates in raw program records but inverted language/main roles and emitted plain program strings without evidence claims. A second frozen prompt variant adds generic CRM program-role definitions and an explicit claim schema. It will run after the unchanged original two-model comparison, preserving comparability and serial memory use. It is not a new per-file name-matching rule and does not change production.

## Phase 1 result
First round: 20/20 worker runs ended normally, 10 original PDFs parsed. Generic extraction failed quality targets. Explicit-role 8B + validated unique-program projection matched all 4 new-sample summary fields and all 3 full program records. Only that new sample is validated for the improved variant; no production acceptance. Fourteen prototype tests passed. Report: 2026-11-05-local-document-extraction-phase1-verification.md.
