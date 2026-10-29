# AAIE CRM

This repository contains the source snapshot of the local CRM test build
`test-features-20261030-c7a69e6b04c2`, prepared on 2026-10-30 (Australia/Melbourne).

## Included updates

- Chinese display labels for student details and school applications.
- Transfer contract source verification and payment/installment workflow updates.
- School map tile URL correction and load-failure feedback.
- Offer school-name filtering that preserves the selected school and prevents Enter from submitting.
- Local test service launchers and isolation checks.
- Removal of the additional test-build title prefix.

Exact source commit references and application file hashes are recorded in
`TEST_BUILD.json`. This is an integrated source snapshot, not a copy of the
original repository's Git history.

## Data and local setup

No business database, student documents, account/password file, field-encryption
key, service token, production environment file, log, audit history, runtime
dependency bundle, OCR model, or historical Office template is included.
Internal operational/history documents are also excluded.

The local copy was configured for macOS and the original machine's Python
runtime. Paths in launchers remain as implemented in that copy. A fresh clone
is **source only**: it requires its own dependency/runtime setup, isolated
configuration, empty test database or separately authorized backup, document
templates, and locally created credentials. Do not run production service
installation/control scripts or point this clone at a production database.

The configured test service ports were Admin `18880`, Parent `18881`, and
Content Extraction `18892`, bound to localhost. The test sandbox blocks
production volume access and external network requests; browser CSP also
blocks external resources. Online map loading is therefore intentionally
unavailable under that test policy.

## Validation of the source build

The local integrated build passed 168 targeted feature/permission/contract
tests and 4 isolation tests. Authenticated HTTP checks covered login,
student pages, the actual Offer tab, application materials, contracts,
and the local content-extraction connection. The title-only follow-up was
verified by HTTP and service health checks.

Full browser business end-to-end flows, video extraction, and live online map
loading were not validated. These results describe the configured local build;
they do not establish that a fresh clone is configured or ready for production.
