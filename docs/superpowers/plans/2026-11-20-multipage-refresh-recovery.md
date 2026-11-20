# Multi-page refresh recovery implementation plan

**Goal:** Prevent expensive snapshot reads from accumulating while keeping committed student, dashboard metrics and distribution changes visible across open pages. Staging only; no production, migration or deployment.

**Architecture:** Reuse schema checks and Visa authority only within a caller-owned, read-only SQLite snapshot. Do not retain business results across requests or transactions. Bound simultaneous full-snapshot API reads, reject excess immediately with an explicit retryable busy response, and stagger browser polling/retries. Keep version checks, permissions and minimum-version fences intact.

**Evidence:** Current profile: 5.970 seconds, 179839 SQLite execute calls, 3676 repeated resolve_case_state calls, 5121 schema validations. Previous multi-page reproduction caused 30/40-second timeouts and long-running snapshot threads.

- [x] RED: actual snapshot repeated-schema/case-query tests; scope and mutation-isolation tests; bounded-concurrency and busy-recovery JS tests.
- [x] Implement read-snapshot-local memo ownership in snapshot_read_cache.py and Database.read_snapshot; cache enabled/schema/table checks and Visa authority without affecting writers.
- [x] Implement a bounded, non-queuing full-snapshot admission guard and explicit busy status; stagger browser timers while preserving abort/disposal/version behavior.
- [x] GREEN: targeted tests, before/after profile and equivalent source output; restart exact task staging service only.
- [x] Mixed real-HTTP concurrency waves: dashboard, student and mobile snapshots; record latency, busy responses, successful retry and drain of backlog.
- [x] Keep multiple actual browser pages open during a fresh student contract-to-grant journey; verify student status, all dashboard metric cards, distribution counts/total and hidden review row, including final independent readback.
- [x] Run native/JS regressions, save screenshots/report, close test polling pages; do not deploy or commit.
