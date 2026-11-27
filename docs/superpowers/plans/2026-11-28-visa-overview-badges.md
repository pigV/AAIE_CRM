# VISA overview unified badges

User approved the screenshot `STAGING_DATA/qa/visa-color-preview-unified.png` on 2026-11-28, authorizing isolated changes and tests, with existing functions unchanged.

## Scope

Render type, Subclass, status and Decision as the approved Figma-compatible tags. Status and Decision share one label-to-tone map and one markup/CSS component: Granted green, Refused red, Withdrawn gray, Pending amber. Draft gray and Lodged blue match the screenshot. Student/500 blue, Guardian/590 purple, Visitor/600 cyan, 485 indigo, unknown values neutral gray. Preserve source labels, escaped output, all original data, search, links, permissions and sorting.

## Steps

- [x] Add a regression test using the actual overview renderer; watch it fail on the existing plain-cell output.
- [x] Add a small presentation helper `visa_overview_ui.py`, call it only for the four overview cells in `app.py`, and style scoped `.visa-overview-tag` classes in the existing CSS.
- [x] Run focused and related regressions, then verify actual renderer output without preview cell rewriting at desktop and mobile sizes. Check exact computed-style equality for same-name status/Decision, text contrast, empty/unknown labels and all original controls.
- [x] Review the isolated diff and preserve evidence; no production deployment/restart, MAIN merge, commit or push.

## Boundaries

Reuse the existing task worktree and prior approved scheme-A CSS changes. No database or schema changes, no new dependencies. Browser acceptance uses synthetic fixtures and actual overview rendering/shared shell, not production data or full database-backed business-flow acceptance.

## Evidence

- Baseline new tests: 4 expected failures, 1 existing-control preservation pass.
- Final related regression suite: 39 tests passed in 29.173s.
- `STAGING_DATA/qa/implemented-badge-checks.json`: actual production renderer markup with synthetic fixtures, 32 new tags, zero preview tags/injected preview CSS; all five equal-label pairs have identical computed style.
- Desktop and mobile screenshots: `implemented-badges-desktop.png`, `implemented-badges-mobile.png`. At 390px document width is 390px; table scrolls within 342px region; minimum tag contrast 4.577.
- No production data or full database-backed business-flow acceptance was performed.
- Independent review: no material findings; reviewer reran all 5 badge tests and diff checks successfully.
