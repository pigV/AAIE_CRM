# Isolated auxiliary services implementation plan

> Execute inline using executing-plans and test-driven-development.

**Goal:** Start Content Extraction on 29892 and Agent Reach on 29890 for the existing separated staging CRM.

**Spec:** User-approved proposal in this chat (2026-11-01).
**Architecture:** Keep the existing runtime identity. Derive the staging Agent Reach port as admin + 10. Add a staging-only auxiliary controller and server entrypoint with native filesystem/network confinement. Reuse installed Python dependencies read-only; copy only backend source and OCR models into the staging runtime. No production data or credentials.
**Tech stack:** Python 3.12, SQLite, native macOS sandbox, existing HTTP/Flask services.

## Constraints and review focus
- Production ports 8780/8781/8790/8792 and their processes remain untouched.
- All generated state stays under the CRM workspace; no package installation, commits, or migration of existing business databases.
- Existing uncommitted changes belong to prior work and must be preserved.
- Verify port collisions, mismatched PID ownership, production configuration rejection, no production gateway fallback, and restart with auxiliaries already listening.
- Only the test Admin/Parent/map may restart to load updated gateway configuration.

## Tasks
- [x] Add regression tests for isolated gateway routing and auxiliary ownership/confinement.
- [x] Extend runtime port validation and gateway routing; add auxiliary control/server scripts.
- [x] Prepare dependencies and fresh auxiliary databases under the existing staging root, start both services, restart test CRM.
- [x] Run targeted tests, HTTP/CRM integration probes, sandbox negative probes, and compare production PID/listener identities.

## Execution ledger
- Initial read: current source already has uncommitted separated-runtime changes. Keep working here as explicitly approved; do not move the running source or create a detached copy.
- Native inline execution authorized by the user's “执行”; no additional plan approval needed.
- Startup findings resolved: dependency metadata visibility, Paddle macOS CPU probe executables, launcher exec-transition identity timing, and backend support-package imports.
- Native smoke verified both services, production read/write/network denial, timed-out subprocess reaping and actual owned parent/grandchild cleanup. Five gateway reads and extraction authentication pass; Paddle recognized `AAIE TEST 2026`.
- Independent review found two subprocess lifecycle gaps; fixed same-sandbox signal permissions and opt-in verified process-group shutdown/cleanup.
- Fresh Agent Reach test DB required explicit v4→v5→v6 initialization for monitoring APIs; applied only to this newly-created empty test DB under its native sandbox. Scheduler remains disabled.
- Final regression: 112 tests run, 111 passed, 1 skipped. `git diff --check` passed. Production listeners unchanged: 8780 PID 10491, 8781 PID 10493, 8790 PID 10362, 8792 PID 9779. Test listeners: 29880 PID 64693, 29881 PID 64697, 29890 PID 64647, 29892 PID 64624, 29893 PID 64691.
