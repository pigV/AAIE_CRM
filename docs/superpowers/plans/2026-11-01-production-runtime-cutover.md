# Production runtime cutover implementation plan

> Execute inline using executing-plans and test-driven-development; review before the live cutover.

**Spec:** ../specs/2026-11-01-production-runtime-cutover.md (user confirmed).
**Goal:** Default CRM startup uses releases/main code and runtime/production data, preserving live ports and existing capabilities.
**Architecture:** Managed runtime controls Admin, Parent, map and extraction. Agent Reach stays independently owned on 8790. Stage data/migration/path-rebase rehearsals first; stop only verified CRM-owned old processes for final synchronization. Open new business writes only after identity/read checks.

## Constraints
- No package installation, Git commit/push/reset, external system-service file edits, or new external data collection.
- Files, backups, credentials copies and reports stay in the CRM workspace.
- Preserve existing accounts, encryption key and immutable audit/snapshot content.
- Existing production remains live until rehearsal and rollback preparation pass.

## Tasks
- [x] Add production-mode configuration/profile/default-stack tests; implement NAS/LAN, extraction ownership, external Agent health and maintenance gate.
- [x] Prepare fresh rehearsal runtime, apply 043 explicitly and rebase only active local file references. Verify preserved data and real file access.
- [x] Freeze source/config hashes and prepare rollback; independently review changes.
- [x] Stop verified old CRM services, take final snapshot, publish runtime/production, migrate 043, launch closed, validate then open writes.
- [x] Verify final health/ports/source/database identity, update runtime documentation and evidence.

## Execution ledger
- User confirmed plan and adjustment.md addition. Long-term production directory rule appended on 2026-11-01.
- Ruling: retain independent Agent Reach live runtime and SQLite on 8790. Audit confirmed its source governance requires independence and copied confined worker would break OpenCLI, daemon19825 and TrendRadar18793 dependencies. CRM default stack verifies external PID/command/listener/health but never signals it. The imported Agent DB is an archive, not the live source. This narrows downtime and preserves actual capability; no new scheduler or collection authorization.
- Baseline old CRM listeners: admin10491/8780, parent10493/8781, extraction9779/8792. Independent Agent10362/8790, all parents PID1 with no controlling active LaunchAgent found. Original commands/births to be captured again immediately before cutover.
- Existing local references include 109 preview files omitted from initial import; copy cache/previews and any explicitly referenced existing files before rebase. Preserve historical missing upload-staging references and report them; never manufacture replacements.

- Completed 2026-11-01: new Admin67946/8780, Parent67951/8781, extraction67923/8792, map67944/8793; external Agent10362/8790 unchanged. Old10491/10493/9779 stopped after exact identity verification.
- Final post-stop snapshot, resource hashes and all-table row counts verified. Only schema_migrations count changed42→43. Closed login/read/write-rejection tests passed; LAN and public browser parent health identified new production. Gate opened and full file download checks passed. Rehearsal stopped.
- Legacy trend-history DTO error reproduced using both old and current gateway on identical live data; retained as baseline, not reported passing. OAuth credentials remain unconfigured as before; no real WeChat signing acceptance claimed.
- Reviewer findings resolved: reserve8793 against staging, require known gate, cleanup all newly owned services on aggregate startup failure, production Agent ownership must be external, pin approved external command/birth rather than trust fresh PID observation.
