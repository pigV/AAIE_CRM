# VISA Overview Figma A Implementation Plan

**Goal:** Apply the user-selected compact list proposal to the existing Visa overview without changing behavior.

**Architecture:** Reuse the existing `visa-management-compact` markup and shared Premium V2 tokens. Limit the runtime diff to scoped CSS in `assets/ui-premium-v2.css`; keep handlers, routes, fields, permissions, queries, sorting and shared navigation unchanged.

**Tech stack:** Existing Python-rendered HTML and CSS; existing unittest checks and browser acceptance.

**Design authority:** Figma `jiCuzF4IunjJ3ZMtauLBoY`, Dashboard `2:3` (read live), and approved `../visa-layout-proposals-20261127/design-preview/figma-scheme-a.png`. Royal blue `#315bef`, white cards, 16px corners, existing typography and shared shell.

## Constraints

- Preserve all eight table columns, links, search GET form, applicant creation, four metric values/notes and existing warning disclosure.
- Keep styling under the overview selector; do not restyle case workspaces or other pages.
- Preserve MAIN's unrelated dirty state. No deployment, restart of production, commit or push.
- Test artifacts, synthetic data, configuration and storage remain in this task worktree.

## Tasks

- [x] Record the approved global Figma rule in root `adjustment.md` and read it back.
- [x] Capture the baseline overview with actual renderer/shared shell and synthetic data; run relevant existing tests.
- [x] Style a single search row, quiet compact metric cards, warning disclosure and compact aligned table using existing tokens. Keep narrow-screen controls usable and confine table overflow to its existing scroll area.
- [x] Run the same relevant tests; browser-check 1440px and 390px, search, case links, creation link, warning expansion, empty results and long names.
- [x] Review the complete diff and save screenshots/results. Report development status distinctly from production.

## Review focus

Long names and status labels must not overlap dates; mobile must retain every column in its table scroller; keyboard focus remains visible; no filtered-state text or warning detail disappears; other pages and shared navigation must keep their existing computed styles.

## Execution notes

The user already approved scheme A and isolated implementation/testing. Execute inline without another approval round. The native worktree tool cannot accept a destination path, so Git created the checkout under the user-required `/Volumes/Data/AAIE_CRM/worktrees/` directory.

## Completion evidence

- Baseline: 12 related tests passed. Final: 34 related tests passed; `git diff --check` clean.
- Browser evidence: `STAGING_DATA/qa/acceptance.json`, `browser-checks.json`, desktop/mobile/empty/long-name screenshots.
- Independent read-only reviewer: no material findings; no changes outside scoped overview CSS.
- Synthetic presentation fixture only, no database-backed business-flow acceptance or production access.
- Branch remains uncommitted in the isolated worktree; no MAIN merge, deployment or push.
