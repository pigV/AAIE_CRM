# Production public pages implementation plan

Goal: Remove simulated-login controls and obsolete environment wording from the production CRM.
Architecture: Keep existing OAuth routes and callbacks. Replace unavailable login controls with a clear support message; reject manual identity POSTs before database access. Preserve NAS roots and business data.
Spec: User approved cleanup of all four public entrypoints on 2026-11-01.

- [x] Execute real extracted route/rendering functions against synthetic fixtures: OAuth on/off, manual POST rejection, token errors and OAuth redirects.
- [x] Update ui.py and the four login routes in app.py; clean obsolete production/MVP messaging and use 检查/验证 for operational diagnostics.
- [x] Run targeted public authentication, parent form and neighboring regression tests with workspace-local test outputs. Review patch and render public pages.
- [x] Prepare production restart details and request permission before restarting production services, as required by root AGENTS.md.

Constraints: No data/schema/credential changes, no commit/push, preserve pre-existing edits. Restore sources from SAFEBOX/20261101-production-public-pages only after comparing intervening edits.
Review focus: Missing OAuth credentials; POST without OAuth even if credentials exist; expired links; valid OAuth redirect; unavailable state must not imply successful verification.
Ruling: Execute in the specified source directory with file snapshots because it contains the approved production-runtime changes; do not discard or replace them.

Production activation approved and completed; see SAFEBOX/20261101-production-public-pages/production-restart.json.
