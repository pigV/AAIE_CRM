-- migration_mode: sqlite_fk_off_rebuild

CREATE TABLE migration_007_completion_empty_guard (
    ok INTEGER NOT NULL
);

CREATE TRIGGER migration_007_completion_empty_guard_reject
BEFORE INSERT ON migration_007_completion_empty_guard
WHEN NEW.ok <> 1
BEGIN
    SELECT RAISE(
        ABORT,
        'migration 007 requires an empty case_completion_confirmations table'
    );
END;

INSERT INTO migration_007_completion_empty_guard(ok)
SELECT CASE
    WHEN NOT EXISTS (
        SELECT 1 FROM case_completion_confirmations
    )
    THEN 1
    ELSE 0
END;

DROP TRIGGER migration_007_completion_empty_guard_reject;
DROP TABLE migration_007_completion_empty_guard;

ALTER TABLE transfer_cases
    ADD COLUMN current_school_enrolment_id INTEGER
        REFERENCES student_school_enrolments(id) ON DELETE RESTRICT;

ALTER TABLE transfer_cases
    ADD COLUMN upcoming_school_enrolment_id INTEGER
        REFERENCES student_school_enrolments(id) ON DELETE RESTRICT;

ALTER TABLE transfer_cases
    ADD COLUMN relationship_status TEXT NOT NULL DEFAULT 'needs_repair'
        CHECK (
            relationship_status IN (
                'needs_repair',
                'current_ready',
                'ready',
                'transitioned',
                'reverted'
            )
        );

CREATE TABLE case_completion_confirmations_rebuild_007 (
    id INTEGER PRIMARY KEY,
    case_id INTEGER NOT NULL
        REFERENCES transfer_cases(case_id) ON DELETE RESTRICT,
    action TEXT NOT NULL
        CHECK (action IN ('complete', 'undo_completion')),
    reverses_confirmation_id INTEGER
        REFERENCES case_completion_confirmations_rebuild_007(id)
        ON DELETE RESTRICT,
    actor_user_id INTEGER NOT NULL
        REFERENCES users(id) ON DELETE RESTRICT,
    confirmed_at TEXT NOT NULL,
    reason TEXT,
    effective_date TEXT NOT NULL
        CHECK (
            length(effective_date) = 10
            AND date(effective_date) IS NOT NULL
            AND date(effective_date) = effective_date
        ),
    idempotency_key TEXT NOT NULL
        CHECK (
            length(trim(idempotency_key)) BETWEEN 1 AND 200
        ),
    workflow_state_before TEXT NOT NULL
        CHECK (
            workflow_state_before IN (
                'draft',
                'preparing',
                'school_search',
                'target_confirmed',
                'application_submitted',
                'waiting_offer',
                'offer_received',
                'waiting_payment',
                'payment_satisfied',
                'waiting_completion_document',
                'ready_to_complete',
                'paused',
                'completed',
                'cancelled',
                'failed',
                'archived'
            )
        ),
    workflow_state_after TEXT NOT NULL
        CHECK (
            workflow_state_after IN (
                'draft',
                'preparing',
                'school_search',
                'target_confirmed',
                'application_submitted',
                'waiting_offer',
                'offer_received',
                'waiting_payment',
                'payment_satisfied',
                'waiting_completion_document',
                'ready_to_complete',
                'paused',
                'completed',
                'cancelled',
                'failed',
                'archived'
            )
        ),
    case_status_before TEXT NOT NULL
        CHECK (
            case_status_before IN (
                'draft',
                'preparing',
                'in_progress',
                'waiting',
                'paused',
                'completed',
                'cancelled',
                'failed',
                'archived'
            )
        ),
    case_status_after TEXT NOT NULL
        CHECK (
            case_status_after IN (
                'draft',
                'preparing',
                'in_progress',
                'waiting',
                'paused',
                'completed',
                'cancelled',
                'failed',
                'archived'
            )
        ),
    active_slot_before INTEGER NOT NULL
        CHECK (active_slot_before IN (0, 1)),
    active_slot_after INTEGER NOT NULL
        CHECK (active_slot_after IN (0, 1)),
    terminal_result_before TEXT
        CHECK (
            terminal_result_before IS NULL
            OR terminal_result_before IN ('completed', 'cancelled', 'failed')
        ),
    terminal_result_after TEXT
        CHECK (
            terminal_result_after IS NULL
            OR terminal_result_after IN ('completed', 'cancelled', 'failed')
        ),
    current_enrolment_before_json TEXT NOT NULL
        CHECK (json_valid(current_enrolment_before_json)),
    current_enrolment_after_json TEXT NOT NULL
        CHECK (json_valid(current_enrolment_after_json)),
    upcoming_enrolment_before_json TEXT NOT NULL
        CHECK (json_valid(upcoming_enrolment_before_json)),
    upcoming_enrolment_after_json TEXT NOT NULL
        CHECK (json_valid(upcoming_enrolment_after_json)),
    readiness_snapshot_json TEXT NOT NULL
        CHECK (json_valid(readiness_snapshot_json)),
    school_state_before_json TEXT NOT NULL
        CHECK (json_valid(school_state_before_json)),
    school_state_after_json TEXT NOT NULL
        CHECK (json_valid(school_state_after_json)),
    metadata_json TEXT NOT NULL
        CHECK (json_valid(metadata_json)),
    created_at TEXT NOT NULL,
    CHECK (
        (
            action = 'complete'
            AND reverses_confirmation_id IS NULL
            AND workflow_state_before = 'ready_to_complete'
            AND workflow_state_after = 'completed'
            AND case_status_before IN (
                'draft',
                'preparing',
                'in_progress',
                'waiting'
            )
            AND case_status_after = 'completed'
            AND active_slot_before = 1
            AND active_slot_after = 0
            AND terminal_result_before IS NULL
            AND terminal_result_after = 'completed'
        )
        OR (
            action = 'undo_completion'
            AND reverses_confirmation_id IS NOT NULL
            AND length(trim(ifnull(reason, ''))) > 0
            AND workflow_state_before = 'completed'
            AND workflow_state_after = 'ready_to_complete'
            AND case_status_before = 'completed'
            AND case_status_after IN (
                'draft',
                'preparing',
                'in_progress',
                'waiting'
            )
            AND active_slot_before = 0
            AND active_slot_after = 1
            AND terminal_result_before = 'completed'
            AND terminal_result_after IS NULL
        )
    )
);

DROP TABLE case_completion_confirmations;

ALTER TABLE case_completion_confirmations_rebuild_007
    RENAME TO case_completion_confirmations;

CREATE UNIQUE INDEX uq_case_completion_idempotency
    ON case_completion_confirmations(case_id, idempotency_key);

CREATE UNIQUE INDEX uq_case_completion_one_undo_per_completion
    ON case_completion_confirmations(reverses_confirmation_id)
    WHERE reverses_confirmation_id IS NOT NULL;

CREATE INDEX ix_case_completion_confirmations_case
    ON case_completion_confirmations(case_id, confirmed_at, id);

CREATE INDEX ix_case_completion_confirmations_confirmed_at
    ON case_completion_confirmations(confirmed_at, id);

CREATE INDEX ix_transfer_cases_current_enrolment
    ON transfer_cases(current_school_enrolment_id);

CREATE INDEX ix_transfer_cases_upcoming_enrolment
    ON transfer_cases(upcoming_school_enrolment_id);

CREATE INDEX ix_transfer_cases_relationship_status
    ON transfer_cases(relationship_status, case_id);

CREATE TRIGGER case_completion_confirmations_validate_reverse
BEFORE INSERT ON case_completion_confirmations
WHEN NEW.action = 'undo_completion'
BEGIN
    SELECT CASE
        WHEN NOT EXISTS (
            SELECT 1
            FROM case_completion_confirmations AS completed
            WHERE completed.id = NEW.reverses_confirmation_id
              AND completed.case_id = NEW.case_id
              AND completed.action = 'complete'
        )
        THEN RAISE(
            ABORT,
            'undo completion must reverse a complete confirmation in the same case'
        )
    END;

    SELECT CASE
        WHEN EXISTS (
            SELECT 1
            FROM case_completion_confirmations AS completed
            WHERE completed.id = NEW.reverses_confirmation_id
              AND (
                  NEW.effective_date IS NOT completed.effective_date
                  OR NEW.workflow_state_before
                        IS NOT completed.workflow_state_after
                  OR NEW.workflow_state_after
                        IS NOT completed.workflow_state_before
                  OR NEW.case_status_before IS NOT completed.case_status_after
                  OR NEW.case_status_after IS NOT completed.case_status_before
                  OR NEW.active_slot_before IS NOT completed.active_slot_after
                  OR NEW.active_slot_after IS NOT completed.active_slot_before
                  OR NEW.terminal_result_before
                        IS NOT completed.terminal_result_after
                  OR NEW.terminal_result_after
                        IS NOT completed.terminal_result_before
                  OR NEW.current_enrolment_before_json
                        IS NOT completed.current_enrolment_after_json
                  OR NEW.current_enrolment_after_json
                        IS NOT completed.current_enrolment_before_json
                  OR NEW.upcoming_enrolment_before_json
                        IS NOT completed.upcoming_enrolment_after_json
                  OR NEW.upcoming_enrolment_after_json
                        IS NOT completed.upcoming_enrolment_before_json
                  OR NEW.school_state_before_json
                        IS NOT completed.school_state_after_json
                  OR NEW.school_state_after_json
                        IS NOT completed.school_state_before_json
              )
        )
        THEN RAISE(
            ABORT,
            'undo completion must restore the reversed completion state'
        )
    END;
END;

CREATE TRIGGER case_completion_confirmations_no_update
BEFORE UPDATE ON case_completion_confirmations
BEGIN
    SELECT RAISE(ABORT, 'completion confirmations are append-only');
END;

CREATE TRIGGER case_completion_confirmations_no_delete
BEFORE DELETE ON case_completion_confirmations
BEGIN
    SELECT RAISE(ABORT, 'completion confirmations are append-only');
END;

CREATE TRIGGER transfer_cases_relationship_validate_insert
BEFORE INSERT ON transfer_cases
BEGIN
    SELECT CASE
        WHEN NOT (
            (
                NEW.relationship_status = 'needs_repair'
                AND NEW.current_school_enrolment_id IS NULL
                AND NEW.upcoming_school_enrolment_id IS NULL
            )
            OR (
                NEW.relationship_status IN (
                    'current_ready',
                    'transitioned',
                    'reverted'
                )
                AND NEW.current_school_enrolment_id IS NOT NULL
                AND NEW.upcoming_school_enrolment_id IS NULL
            )
            OR (
                NEW.relationship_status = 'ready'
                AND NEW.current_school_enrolment_id IS NOT NULL
                AND NEW.upcoming_school_enrolment_id IS NOT NULL
            )
        )
        THEN RAISE(ABORT, 'transfer relationship status and pointers conflict')
    END;

    SELECT CASE
        WHEN NEW.current_school_enrolment_id IS NOT NULL
         AND NOT EXISTS (
            SELECT 1
            FROM student_school_enrolments AS enrolment
            JOIN student_cases AS parent_case
              ON parent_case.id = NEW.case_id
            WHERE enrolment.id = NEW.current_school_enrolment_id
              AND enrolment.student_id = parent_case.student_id
              AND enrolment.enrolment_role = 'current'
              AND enrolment.status = 'active'
        )
        THEN RAISE(
            ABORT,
            'current enrolment must be active current for the case student'
        )
    END;

    SELECT CASE
        WHEN NEW.upcoming_school_enrolment_id IS NOT NULL
         AND NOT EXISTS (
            SELECT 1
            FROM student_school_enrolments AS enrolment
            JOIN student_cases AS parent_case
              ON parent_case.id = NEW.case_id
            WHERE enrolment.id = NEW.upcoming_school_enrolment_id
              AND enrolment.student_id = parent_case.student_id
              AND enrolment.enrolment_role = 'upcoming'
              AND enrolment.status = 'active'
              AND enrolment.source_case_id = NEW.case_id
        )
        THEN RAISE(
            ABORT,
            'upcoming enrolment must be active upcoming for the same case student'
        )
    END;

    SELECT CASE
        WHEN NEW.current_school_enrolment_id
             IS NEW.upcoming_school_enrolment_id
         AND NEW.current_school_enrolment_id IS NOT NULL
        THEN RAISE(ABORT, 'current and upcoming enrolments must differ')
    END;
END;

CREATE TRIGGER transfer_cases_relationship_validate_update
BEFORE UPDATE OF
    current_school_enrolment_id,
    upcoming_school_enrolment_id,
    relationship_status
ON transfer_cases
BEGIN
    SELECT CASE
        WHEN NOT (
            (
                NEW.relationship_status = 'needs_repair'
                AND NEW.current_school_enrolment_id IS NULL
                AND NEW.upcoming_school_enrolment_id IS NULL
            )
            OR (
                NEW.relationship_status IN (
                    'current_ready',
                    'transitioned',
                    'reverted'
                )
                AND NEW.current_school_enrolment_id IS NOT NULL
                AND NEW.upcoming_school_enrolment_id IS NULL
            )
            OR (
                NEW.relationship_status = 'ready'
                AND NEW.current_school_enrolment_id IS NOT NULL
                AND NEW.upcoming_school_enrolment_id IS NOT NULL
            )
        )
        THEN RAISE(ABORT, 'transfer relationship status and pointers conflict')
    END;

    SELECT CASE
        WHEN NEW.current_school_enrolment_id IS NOT NULL
         AND NOT EXISTS (
            SELECT 1
            FROM student_school_enrolments AS enrolment
            JOIN student_cases AS parent_case
              ON parent_case.id = NEW.case_id
            WHERE enrolment.id = NEW.current_school_enrolment_id
              AND enrolment.student_id = parent_case.student_id
              AND enrolment.enrolment_role = 'current'
              AND enrolment.status = 'active'
        )
        THEN RAISE(
            ABORT,
            'current enrolment must be active current for the case student'
        )
    END;

    SELECT CASE
        WHEN NEW.upcoming_school_enrolment_id IS NOT NULL
         AND NOT EXISTS (
            SELECT 1
            FROM student_school_enrolments AS enrolment
            JOIN student_cases AS parent_case
              ON parent_case.id = NEW.case_id
            WHERE enrolment.id = NEW.upcoming_school_enrolment_id
              AND enrolment.student_id = parent_case.student_id
              AND enrolment.enrolment_role = 'upcoming'
              AND enrolment.status = 'active'
              AND enrolment.source_case_id = NEW.case_id
        )
        THEN RAISE(
            ABORT,
            'upcoming enrolment must be active upcoming for the same case student'
        )
    END;

    SELECT CASE
        WHEN NEW.current_school_enrolment_id
             IS NEW.upcoming_school_enrolment_id
         AND NEW.current_school_enrolment_id IS NOT NULL
        THEN RAISE(ABORT, 'current and upcoming enrolments must differ')
    END;
END;

CREATE TRIGGER student_school_enrolments_transfer_reference_guard
BEFORE UPDATE OF
    student_id,
    enrolment_role,
    status,
    source_case_id
ON student_school_enrolments
BEGIN
    SELECT CASE
        WHEN EXISTS (
            SELECT 1
            FROM transfer_cases AS transfer
            JOIN student_cases AS parent_case
              ON parent_case.id = transfer.case_id
            WHERE transfer.current_school_enrolment_id = OLD.id
              AND (
                  NEW.student_id <> parent_case.student_id
                  OR NEW.enrolment_role <> 'current'
                  OR NEW.status <> 'active'
              )
        )
        THEN RAISE(
            ABORT,
            'referenced current enrolment cannot invalidate transfer relationship'
        )
    END;

    SELECT CASE
        WHEN EXISTS (
            SELECT 1
            FROM transfer_cases AS transfer
            JOIN student_cases AS parent_case
              ON parent_case.id = transfer.case_id
            WHERE transfer.upcoming_school_enrolment_id = OLD.id
              AND (
                  NEW.student_id <> parent_case.student_id
                  OR NEW.enrolment_role <> 'upcoming'
                  OR NEW.status <> 'active'
                  OR NEW.source_case_id IS NOT transfer.case_id
              )
        )
        THEN RAISE(
            ABORT,
            'referenced upcoming enrolment cannot invalidate transfer relationship'
        )
    END;
END;
