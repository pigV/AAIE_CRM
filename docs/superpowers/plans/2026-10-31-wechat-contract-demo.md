# Mobile WeChat contract acceptance

**Approved scope:** The user approved an independent phone-accessible test on this branch, using synthetic students/contracts, then explicitly chose simulated parent identity instead of real WeChat OAuth. No Production mutation or routing changes.

**Goal:** Open a private test link in WeChat, draw a parent signature, submit, then view/download the complete PDF with approved company signature, seal and the signing date on the same page.

**Architecture:** A dedicated loopback test server reuses the current app's contract methods and real PDF renderer. A fresh synthetic database and storage live under this checkout's STAGING_DATA. An exact route allowlist exposes only generated demo contract tokens. A separate Cloudflare Quick Tunnel connects to this server; the existing public CRM remains untouched. Pages state that identity and contract are simulated. Actual OAuth is outside this accepted scope.

**Execution:** Use superpowers:executing-plans in the existing user-selected branch. No new worktree, commit, push, installation, real messages or credentials. Tests before implementation follow superpowers:test-driven-development. Preserve existing changes.

## Tasks
- [x] Add route/presentation tests, observe failure, implement `tools/wechat_contract_demo.py` and `tests/test_wechat_contract_demo.py`.
- [x] Add isolated launcher `tools/wechat_contract_demo_control.py`: native sandbox, no outbound network from CRM, deny reading/writing live Staging database and production, bind only 127.0.0.1:18883, and run-local outputs.
- [x] Seed separate manual and automated contracts through the real token-generation method. Never consume the manual token during automated acceptance.
- [x] Run local HTTP acceptance with the production signing/download methods, inspect the complete PDF and confirm source pages plus all three signature/seal images and date.
- [x] Start a separate temporary HTTPS tunnel, verify public access and disallowed paths, hand the fresh manual URL to the user for actual WeChat touch/download acceptance.
- [x] Review complete diff and record precise results, limitations, process identities and shutdown instructions.

## Review focus
- Unknown tokens, admin routes, path traversal and direct assets must return 404.
- Request sizes are bounded; signatures are processed serially to avoid concurrent duplicate submissions in this demo.
- No production or backup-derived personal data is reachable from the demo process.
- All pages and source PDF identify the test; real WeChat identity is never claimed.
- Final PDF access retains the existing signing identity cookie, status and archive checks.
- Preserve evidence on failure; public acceptance must not sign the user's manual test contract.

## Evidence ledger
- Prior focused native-sandbox run: 29 tests passed; direct old runner failed its Staging startup guard. This work preserves the startup guard and uses native sandbox execution.

- Final run: `STAGING_DATA/wechat-contract-demo/run-9m3dt4oy/`; 4 new tests pass, fresh full HTTPS signing/view/download/reopen passes, 6-page final PDF inspected. At handoff the manual contract was active and unbound. On 2026-10-31 the user confirmed “上面已经测试可以使用”; mobile WeChat simulated-parent acceptance is now recorded as passed. Real OAuth and Production deployment remain outside this acceptance.
- Review finding corrected: initial demo profile inherited broad Staging access. Native test reproduced denied-boundary failure, then passed after restricting all data access to the current run and exact read-only assets/config. Reviewer confirmed no remaining blockers.
- Existing Git `._pack` index warnings remain; no Git repair, commit or push.
