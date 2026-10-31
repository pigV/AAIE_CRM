# Code and Data Separation Implementation Plan

> **For agentic workers:** Use superpowers:executing-plans in this session. Steps use checkbox syntax.

**Goal:** Run the existing CRM code from releases/main with an explicit persistent runtime root, without moving or initializing production data.
**Architecture:** Add validated runtime configuration and a managed runtime adapter reusing existing filesystem audit rules. A separate controller performs read-only admission and launches sandboxed processes. Legacy staging and production paths keep their existing behavior.
**Tech Stack:** Python standard library, sqlite3, existing cryptography dependency, macOS sandbox-exec.
**Spec:** /Volumes/Zhang/AAIE CodeX/CRM/SAFEBOX/code-data-separation/design-20261101-005852/DESIGN.md

## Global Constraints
- Code: /Volumes/Zhang/AAIE CodeX/CRM/releases/main.
- Production data: sibling runtime/production. Test data: sibling runtime/staging.
- No production restart, migration, credentials copy, schema modification, or Git commit/push.
- Do not remove AppleDouble metadata. Work in this already isolated, non-running source clone explicitly selected by the user.
- Tests write only dedicated synthetic directories under runtime/staging; no production data used in write tests.
- Configuration errors must never create databases or keys. Legacy safety guards remain intact.

## Review Focus
- Symlink/hardlink/traversal or overlapping roots must fail before opening a database (Task 1).
- A staging config cannot select production data or ports (Task 1).
- Managed startup must not call database initialization or generate replacement keys/templates (Task 2).
- Code assets and scripts must not be looked up under data directories (Task 2).
- Unrelated listener/PID, missing migrations, concurrent startup or stale configuration must refuse control (Task 3).

### Task 1: Pure runtime configuration and read-only preflight
**Files:** runtime_configuration.py; tests/test_runtime_configuration.py.
**Interfaces:** RuntimeConfiguration.load(path, source_root); preflight(config, check_ports=False) -> ready/errors/identity/pending.
- [x] Write tests for valid separated roots, wrong environment, ports, aliases, missing DB/key and nonmutating preflight.
- [x] Run red tests.
- [x] Implement strict versioned config, preflight and native sandbox profile/environment.
- [x] Run green tests and record evidence.

### Task 2: Managed runtime integration
**Files:** staging_environment.py; app.py; database.py; tests/test_separated_runtime_integration.py.
**Interfaces:** ManagedRuntime.runtime_configuration, root/db/storage/namespace; AAIE_CRM_RUNTIME_CONFIG.
- [x] Write tests for bootstrap refusal without native sandbox, asset/data routing and startup without initialization.
- [x] Implement adapter without loosening legacy Staging.load.
- [x] Route immutable assets/scripts/migrations to SOURCE_DIR, data to runtime root.
- [x] Preserve old behavior when no separated runtime is configured; refuse implicit fallback in this separated layout.
- [x] Run targeted regressions.

### Task 3: Explicit control and synthetic acceptance
**Files:** tools/runtime_control.py; tools/runtime_server.py; docs/runtime-layout.md; config/runtime.example.json.
**Interfaces:** preflight/start/status/stop --config; production start/stop requires explicit activation confirmation.
- [x] Write tests for missing resources/migrations, process identity and port conflicts.
- [x] Implement controller with per-instance lock, exact PID/birth verification and start cleanup.
- [x] Verify native sandbox and HTTP using a synthetic fixture only where full preflight is supportable.
- [x] Run relevant tests, independent final review and record unresolved deployment prerequisites.

## Execution ledger
User approved the design and implementation in this chat. Continue inline without another permission loop; no Git commit/push is authorized.
