# AAIE CRM

This repository contains the source snapshot of the local CRM test build
`test-features-20261030-c7a69e6b04c2-nas-ro-7dfac156-lan-af9bcfae-nas-rw-1ecb6688-ports-f7a4708e-agent-reach-fb4c2f48`, prepared on 2026-10-30 (Australia/Melbourne).

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

The current service ports are Admin `8780`, Parent `8781`, and Content
Extraction `8792`. Admin can bind to the LAN when explicitly configured;
Parent and Content Extraction remain on localhost. CRM can connect to the
independently deployed Agent Reach backend on `127.0.0.1:8790`. Its deployed
source commit is recorded in `TEST_BUILD.json`; its repository, database and
runtime are separate and are not bundled here. Its scheduler is disabled.
`tools/test_copy_control.py` manages the three CRM services only.

NAS attachment access is opt-in through local configuration. Read and write
permissions are separate; the current local deployment authorizes writes to
the shared student and school directories. Database writes remain restricted
to local Staging data. No local NAS authorization or LAN configuration is
included in this source snapshot. External network access and browser CSP
restrictions remain in place.

The original production services were stopped before the current services
took over these ports. This snapshot still uses the backup-derived test
database and test accounts; it is not evidence of completed production-data
or account migration.

## Validation of the source build

The local integrated build passed 168 targeted feature/permission/contract
tests and 4 isolation tests. Authenticated HTTP checks covered login,
student pages, the actual Offer tab, application materials, contracts,
and the local content-extraction connection. The title-only follow-up was
verified by HTTP and service health checks.

Full browser business end-to-end flows, video extraction, and live online map
loading were not validated. These results describe the configured local build;
they do not establish that a fresh clone is configured or ready for production.

## Current deployment verification

The four service health checks passed on 2026-10-30. Five read-only CRM gateway
operations passed. Trend run history has a known validation incompatibility
with three older failed records whose platform outcomes are incomplete.
No real platform search was executed during this deployment.
