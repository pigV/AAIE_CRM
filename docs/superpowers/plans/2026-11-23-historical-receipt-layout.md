# Historical receipt layout implementation plan

> **For agentic workers:** Use superpowers:executing-plans inline; final fresh-context review only.

**Goal:** Deliver the confirmed record-first historical collection workbench using existing AAIE Figma tokens.
**Architecture:** Keep page_data and all financial mutations intact. Refactor historical_receipt_views render into record, editor and document helpers; page-scoped CSS and progressive JS handle presentation and conditional fields.
**Tech Stack:** Python server-rendered HTML, existing CSS variables, vanilla external JavaScript; no dependencies.
**Spec:** reports/historical-client-receipts/布局核查与修改建议.md, approach A confirmed by user.

## Constraints
- Existing feature worktree; no production changes, migrations, commits, push or deploy this round.
- Same actions, CSRF, revisions and idempotency tokens; native mobile remains read-only.
- Figma Dashboard2:3 tokens already verified; preserve existing system shell and financial rules.

## Review focus
- Existing/cancelled review editor preserves correct uid, revision, cycle and kind; no accidental duplicate new record.
- CNY does not submit hidden AUD settlement; conditional visa field never silently retains unrelated ownership.
- Missing facts remain visible; client readiness never replaces server validation.
- Multiple historical records and escaped source notes/student names remain safe and readable.
- Mobile read-only, keyboard operation, CSS/JS absent usable fallbacks.

### Task 1: Record-first view, scoped styling and progressive interactions
- [x] Preserve baseline screenshots/source in task evidence; inspect existing field and validation contracts.
- [x] Refactor revenue/historical_receipt_views.py; retain render signature, page_data and render_case_summary contract.
- [x] Add page-scoped CSS assets/historical-receipts.css using revenue variables; 14px text, 44px targets, 18px checkbox and compact query header.
- [x] Add assets/historical-receipts.js for conditional visa/AUD fields, safe new-editor disclosure and keyboard focus; no financial posting logic.
- [x] Verify desktop existing draft, editing, new entry, cancelled reopen, confirmed record and CNY on browser.

### Task 2: Verification and acceptance
- [x] Run prior95 related regressions and syntax/diff checks; add meaningful regression coverage only for changed behavior where needed.
- [x] Review1440 desktop, intermediate width,390mobile; measure layout/checkbox/form boundaries, test screenshot upload roundtrip if changed upload markup affects it.
- [x] Dispatch one fresh-context whole layout review per executing-plans; address material findings with evidence.
- [x] Record exact changes, tests, limitations and service health; leave staged preview for user acceptance, no production release.

Ruling: User confirmation already authorizes this concrete layout plan; continue without another plan approval. Preserve worktree until acceptance; no commit or cleanup authority.

验收记录：reports/historical-client-receipts/布局改版验收.md；98项相关测试通过，桌面/中等宽度/手机及完整模拟表单、附件回读已验证。独立复核完成。Ruling: 保留先前附件重复登记的已知限制，避免扩大本轮布局授权到文档领域服务。
