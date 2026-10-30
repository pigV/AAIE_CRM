# Parent contract signing implementation plan

> Execute in this session with superpowers:executing-plans; user approved the four-part design and implementation on 2026-10-30.

**Goal:** Produce the complete signed PDF after parent submission, with AAIE signature, seal and Melbourne signing date within the pictured left cell, and display its authenticated download link.
**Architecture:** Retain the existing PDF overlay, archive and WeChat identity flow. Correct asset defaults, constrain layout and date, and gate completion on the actual final artifact. Reuse the existing admin archive retry; preserve saved parent evidence on failure.
**Tech Stack:** Python, ReportLab, pypdf, pdfplumber, unittest.
**Spec:** User-approved chat proposal: automated AAIE signature/seal/date; image 2 layout; full PDF download/reopen; honest failure with preserved signature.

## Constraints
- All artifacts under this checkout. Isolated synthetic DB/NAS only. No Production mutations, migrations, environment configuration changes, commits, pushes or release.
- Read existing approved signature/seal assets; never synthesize a legal signature or seal.
- Preserve original signing date on retry. Server timezone must not alter Melbourne date.
- Preserve WeChat identity and revoked/inactive link checks.

## Review focus
- Missing or corrupt assets must never produce a completed state.
- PDF preview failure must not hide a valid download.
- Failed archive/retry must preserve parent evidence and never download a stale incomplete file.
- Large date settings and missing layout anchors must never draw outside the AAIE cell.
- UTC/DST boundaries and repeat download must preserve the original signing date/artifact.

## Tasks
- [x] Add behavior tests for failed/reopened signing page, download readiness, Melbourne date and confined PDF layout; run RED.
- [x] Correct default asset directory, date conversion and bounded layout in app.py; retain master template unless evidence requires changing it.
- [x] Gate final download and truthful success/failure pages; retain current authenticated endpoints and archive retry.
- [x] Run focused new tests plus existing contract/master-layout/archive/access regressions with a workspace-only test runner.
- [x] Generate a synthetic full contract with actual approved assets, render and inspect signature page; test HTTP submit/view/download/reopen with synthetic data if feasible.
- [x] Review diff independently; record results and limits. Keep branch uncommitted for user review.

## Confirmed layout revision
- [x] Enlarge signature/seal 15%, top-align below date. Actual PDF gap 1.73mm; 25 focused tests and HTTP acceptance passed. Evidence: SAFEBOX/contract-signing-20261030/layout-r2/.
