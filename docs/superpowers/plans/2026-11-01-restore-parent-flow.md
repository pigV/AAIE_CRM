# Restore existing public identity flow

Approved by user: revert the added OAuth-only rejection, preserve original identity confirmation, draft save and submission behavior, clean only test wording. Test, restart production, and append the approved scope rule to adjustment.md.

- [x] Replace incorrect regression assertions with old-flow behavior tests; observe failure.
- [x] Restore four original login handlers from the pre-change snapshot exactly. Restore the original form fields and action, using neutral identity copy without test labels or unavailable warning. Keep real OAuth branches intact.
- [x] Test route success, same-identity resume, different-identity denial, expiry, draft save, submit protection and OAuth routes in isolated native sandbox; run existing neighboring regressions.
- [x] Verify patch against original, restart through existing controller after waiting for port release, verify current public link without binding it to a synthetic identity.
- [x] Append approved adjustment.md rule. No schema, credentials, or existing binding changes.
