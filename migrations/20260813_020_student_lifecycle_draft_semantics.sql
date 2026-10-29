-- migration_mode: sqlite_fk_off_rebuild

-- Fail closed before any schema mutation if an existing Draft is already bound
-- to a formal active Cycle. Such a row needs immutable reconciliation first.
CREATE TABLE student_lifecycle_reconciliation_guard_018 (
  blocker_count INTEGER NOT NULL CHECK(blocker_count = 0)
);

INSERT INTO student_lifecycle_reconciliation_guard_018(blocker_count)
SELECT count(*)
FROM student_cases AS cases
JOIN application_cycles AS cycles
  ON cycles.source_student_case_id = cases.id
WHERE cases.status = 'draft'
  AND cycles.status = 'active';

DROP TABLE student_lifecycle_reconciliation_guard_018;

DROP TRIGGER student_cases_no_delete;
DROP TRIGGER student_cases_archive_from_terminal_only;
DROP INDEX uq_student_cases_one_active_per_student;
DROP INDEX ix_student_cases_student_type_sequence;
DROP INDEX ix_student_cases_student_status;

CREATE TABLE student_cases_rebuild_018 (
  id INTEGER PRIMARY KEY,
  student_id TEXT NOT NULL REFERENCES students(student_id) ON DELETE RESTRICT,
  case_type TEXT NOT NULL CHECK(case_type IN ('transfer','progression')),
  sequence_no INTEGER NOT NULL CHECK(sequence_no > 0),
  status TEXT NOT NULL DEFAULT 'draft' CHECK(status IN (
    'draft','preparing','in_progress','waiting','paused',
    'completed','cancelled','failed','archived'
  )),
  terminal_result TEXT CHECK(
    terminal_result IS NULL OR terminal_result IN ('completed','cancelled','failed')
  ),
  active_slot INTEGER NOT NULL DEFAULT 0 CHECK(active_slot IN (0,1)),
  started_at TEXT,
  paused_at TEXT,
  completed_at TEXT,
  cancelled_at TEXT,
  failed_at TEXT,
  archived_at TEXT,
  created_by INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
  created_at TEXT NOT NULL,
  updated_by INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
  updated_at TEXT NOT NULL,
  UNIQUE(student_id,case_type,sequence_no),
  CHECK(
    (status='draft' AND active_slot=0 AND terminal_result IS NULL)
    OR (status IN ('preparing','in_progress','waiting','paused')
        AND active_slot=1 AND terminal_result IS NULL)
    OR (status IN ('completed','cancelled','failed')
        AND active_slot=0 AND terminal_result=status)
    OR (status='archived' AND active_slot=0
        AND terminal_result IN ('completed','cancelled','failed'))
  )
);

INSERT INTO student_cases_rebuild_018(
  id,student_id,case_type,sequence_no,status,terminal_result,active_slot,
  started_at,paused_at,completed_at,cancelled_at,failed_at,archived_at,
  created_by,created_at,updated_by,updated_at
)
SELECT
  id,student_id,case_type,sequence_no,status,terminal_result,
  CASE WHEN status='draft' THEN 0 ELSE active_slot END,
  CASE WHEN status='draft' THEN NULL ELSE started_at END,
  paused_at,completed_at,cancelled_at,failed_at,archived_at,
  created_by,created_at,updated_by,updated_at
FROM student_cases;

ALTER TABLE student_cases RENAME TO student_cases_legacy_018;
ALTER TABLE student_cases_rebuild_018 RENAME TO student_cases;
DROP TABLE student_cases_legacy_018;

CREATE UNIQUE INDEX uq_student_cases_one_active_per_student
  ON student_cases(student_id) WHERE active_slot=1;
CREATE INDEX ix_student_cases_student_type_sequence
  ON student_cases(student_id,case_type,sequence_no DESC);
CREATE INDEX ix_student_cases_student_status
  ON student_cases(student_id,status);

CREATE TRIGGER student_cases_no_delete
BEFORE DELETE ON student_cases
BEGIN
  SELECT RAISE(ABORT,'student cases are retained permanently');
END;

CREATE TRIGGER student_cases_archive_from_terminal_only
BEFORE UPDATE OF status ON student_cases
WHEN NEW.status='archived' AND OLD.status NOT IN ('completed','cancelled','failed')
BEGIN
  SELECT RAISE(ABORT,'active student case cannot be archived');
END;

CREATE TABLE student_lifecycle_idempotency_receipts (
  id INTEGER PRIMARY KEY,
  student_id TEXT NOT NULL REFERENCES students(student_id) ON DELETE RESTRICT,
  operation TEXT NOT NULL CHECK(length(trim(operation)) > 0),
  idempotency_key TEXT NOT NULL CHECK(length(trim(idempotency_key)) > 0),
  payload_sha256 TEXT NOT NULL CHECK(length(payload_sha256) = 64),
  result_case_id INTEGER REFERENCES student_cases(id) ON DELETE RESTRICT,
  result_cycle_id INTEGER REFERENCES application_cycles(id) ON DELETE RESTRICT,
  result_json TEXT NOT NULL CHECK(json_valid(result_json)),
  actor_user_id INTEGER REFERENCES users(id) ON DELETE RESTRICT,
  created_at TEXT NOT NULL,
  UNIQUE(student_id,operation,idempotency_key)
);

CREATE INDEX ix_student_lifecycle_receipts_results
  ON student_lifecycle_idempotency_receipts(result_case_id,result_cycle_id);
