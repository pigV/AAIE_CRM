# Visa feedback repair

Approved scope: batch review table matching the supplied screenshot; document edits and deletion return to their originating tab and material position.

1. Use task branch fix/visa-feedback-20261126 at baseline 164a2fc. Never change production or its dirty files.
2. Add failing rendered-form and real handler tests for the table, preserved selections, document return context, and safe redirects.
3. Restore the seven-column review table in app.py using the existing table styles. Add scoped responsive styling only if required. Preserve upload confirmation, skipped pending files, CSRF and classification behavior.
4. Add originating-tab fields to document forms inside material/result renderers. Validate a fixed local return tab and anchor in the document POST handler. Retain existing behavior when no context is supplied.
5. Preserve material search, filters, expanded file/edit controls and scroll through the existing session state.
6. Run confined Visa regressions and browser checks with real handlers, synthetic data, private storage, and ports 19086/19087. Save evidence under this checkout and leave it for acceptance. No production deployment, migration, restart, commit or push.
