# Visa case three-page Figma styling

Approved final design: info-fresh.jpg, materials-fresh.jpg, result-fresh.jpg. Sea-salt teal, mint and subtle lavender; no orange/yellow; white canvas and unchanged functions/structure. User explicitly authorized MAIN integration, GitHub push and deployment after acceptance.

1. Record original desktop DOM control contracts and geometry in STAGING_DATA/preview/implementation-baseline.json. Run existing material/result rendering and JS suites as baseline.
2. Extend the existing presentation-only visa_overview_ui state tone lookup for translated collection/material labels. Preserve existing overview mappings. Add tests for terminal outcomes, aliases, unknown values, HTML escaping and unchanged stage/action targets. New tests must fail before implementation.
3. Add assets/visa-case-figma.css based on approved review overlay; preserve dimensions, spacing, display, fonts, text and control ownership. Scope content styles to the three tab panels. Add presentation-only tone attributes to existing status elements in app.py, visa_workspace_candidate.py and visa_result_workspace_ui.py. Use the overview color formulas and shared theme variables. No DB logic, routes, JS behavior or schema changes.
4. Restart only task server 19086 with its existing OS-confined staging launcher. Verify synthetic DB/storage identity. Test collection edit/save/reload, material filtering/status/save/reload, result accordion/status/save/reload, upload/readback and navigation with disposable synthetic fixtures. Compare control contracts and geometry. Verify terminal outcomes and collection/material state colors.
5. Run focused Python and JS suites. Request a fresh final review. Save screenshots, evidence and remaining limitations in STAGING_DATA. After final verification, integrate only scoped files, preserve unrelated MAIN edits, push exact candidate and perform guarded code-only deployment with backup/health/write-gate checks.

Review focus: no accidental styling of evidence/AI content; status colors follow values rather than positions; Pending and other waiting states use subtle lavender and terminal outcomes match the main Visa overview; disabled controls stay readable; HTML stays escaped; no changed POST targets or business transitions; layout geometry unchanged.

Final release includes assets/visa-state-colors.css to synchronize same-name overview colors. No schema/data changes. Preview-only environment badge recoloring is excluded from application code.
