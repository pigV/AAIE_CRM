# Application document extraction repair

Approved scope: the four issues in the supplied Word and its eight screenshots,
plus a separately editable and persisted Offer English Language Centre field.
Work stays on 20261030_02. Existing uncommitted work is preserved.

- [x] Reproduce Victorian Offer section selection, empty Medibank passport,
  CAAW welfare boundaries, scanned PDF extraction and OCR diagnostics.
- [x] In app.py anchor Offer school/language values to Course Details; preserve
  empty language as unknown. Parse Medibank label/value layouts without requiring
  a particular marketing title or a value for every optional field.
- [x] Restore local Apple Vision OCR with a precompiled helper in STAGING_DATA;
  separate source paths from data paths, allow only that executable in the
  Staging launcher, retain outbound network and database restrictions. Report
  failures safely and avoid extracting the same upload repeatedly.
- [x] Add an explicit nullable offers.language_school_name migration with a
  forward check; test in a fixture before requesting approval to apply it to
  the identified Staging database. Add review, create/update and reload support.
- [x] Run extraction regressions, real screenshot/image-PDF OCR, migration and
  persistence checks, and launcher isolation checks. Review the final diff.
- [x] After migration approval, use the existing Staging controller to restart
  only its verified services and confirm both health identities.

Review focus: page-three Course Details; distinct language and main school;
blank language and passport; welfare dates versus course/signature dates;
text-PDF regression; blank/encrypted/failed OCR; transaction and rollback
behavior; manual field clearing; source/data/executable isolation.

No original PDFs are available. Image-PDF fixtures reconstructed from screenshots
prove this extraction path only, not acceptance of the unavailable originals.
Only the current branch Staging schema migration was explicitly approved and applied.
No Production deployment, Production schema migration, commit or push was authorized.
