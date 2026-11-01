# Contract download handoff implementation plan

User approved the minimal fix on 2026-11-01, preserving the original logic.

Goal: keep authenticated viewing inside the existing page, and let the external browser download only the exact final PDF authorized by that session.

Architecture: a focused contract_download_grants.py module issues Fernet-encrypted, purpose-scoped grants valid for 300 seconds using the existing runtime cipher. Bind to link, contract, signed signature, WeChat identity digest, link token hash, and final PDF path/content digest. Existing status/revocation/file guards remain before grant authorization. Original cookie-only PDF routes remain supported. No schema/config changes.

Implementation and verification:
- [x] In project-local candidate files, write failing tests for cookie-less handoff, same-cookie legacy PDF, no grant/wrong cookie rejection, tampering/expiry/revocation/wrong artifact and link, and in-page preview actions.
- [x] Add grant module and bounded handler integration. Redact grant query values from application access logs. Use no-referrer/no-store responses.
- [x] Run focused tests plus existing public-login/WeChat regression tests. Verify with two independent HTTP clients using synthetic fixtures only.
- [x] Review diff for scope and auth regressions. Preserve existing edits and test candidate before applying bounded changes.
- [x] Apply verified code with rollback snapshot; request production restart approval at final deployment step if not already authorized. No Git commit/push.

Review focus: stale page grants must expire with a clear refresh-in-WeChat instruction; changed PDF or signature invalidates grant; revoked/inactive contracts remain inaccessible; invalid existing identity cookie must not be rescued by grant; no private credentials in URLs or logs; original login, binding and signing code remains unchanged.

Isolation ruling: use an isolated file-based candidate and SAFEBOX under reports/ because native managed worktree placement cannot honor the user's CRM-only path constraint; no branch switch or dependencies installed. Tests extract actual handlers without importing production runtime. Scope is this single fix.

Verification: applied-source focused tests and existing public WeChat regressions: 42 passed. One additional legacy identity integration test cannot import the app without an explicit separated runtime configuration; not counted as passing. Syntax checked for the five changed/new Python files. Existing class methods differ only in the four documented integration points; all other class methods are unchanged. Independent reviewer found no remaining issue. Real WeChat phone verification remains pending.

Production services have NOT been restarted. Root AGENTS.md explicitly requires confirmation before restarting production. Existing runtime_control.py coordinates Admin, Parent and map together; use the supported control entrypoint after approval and verify health. No commit, push, schema, production data or config changes were made for this download fix.

User authorized production restart. Restart completed and verified on 2026-11-01T14:37:46.185590+11:00. Admin 8780, Parent 8781 and map health checks passed with new process IDs. An initial port-unavailable preflight cleared before retry; no force kill or configuration change. Evidence: reports/contract-download-fix-20261101/restart-verification.json. Real WeChat phone acceptance remains pending.
