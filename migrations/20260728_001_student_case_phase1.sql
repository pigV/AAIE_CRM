CREATE TABLE student_case_sequences (
    student_id TEXT NOT NULL
        REFERENCES students(student_id) ON DELETE RESTRICT,
    case_type TEXT NOT NULL
        CHECK (case_type IN ('transfer', 'progression')),
    next_sequence_no INTEGER NOT NULL DEFAULT 1
        CHECK (next_sequence_no >= 1),
    updated_at TEXT NOT NULL,
    PRIMARY KEY (student_id, case_type)
);

CREATE TRIGGER student_case_sequences_no_delete
BEFORE DELETE ON student_case_sequences
BEGIN
    SELECT RAISE(
        ABORT,
        'student case sequences are retained permanently'
    );
END;

CREATE TABLE student_cases (
    id INTEGER PRIMARY KEY,
    student_id TEXT NOT NULL
        REFERENCES students(student_id) ON DELETE RESTRICT,
    case_type TEXT NOT NULL
        CHECK (case_type IN ('transfer', 'progression')),
    sequence_no INTEGER NOT NULL
        CHECK (sequence_no > 0),
    status TEXT NOT NULL DEFAULT 'draft'
        CHECK (
            status IN (
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
    terminal_result TEXT
        CHECK (
            terminal_result IS NULL
            OR terminal_result IN ('completed', 'cancelled', 'failed')
        ),
    active_slot INTEGER NOT NULL DEFAULT 1
        CHECK (active_slot IN (0, 1)),
    started_at TEXT,
    paused_at TEXT,
    completed_at TEXT,
    cancelled_at TEXT,
    failed_at TEXT,
    archived_at TEXT,
    created_by INTEGER NOT NULL
        REFERENCES users(id) ON DELETE RESTRICT,
    created_at TEXT NOT NULL,
    updated_by INTEGER NOT NULL
        REFERENCES users(id) ON DELETE RESTRICT,
    updated_at TEXT NOT NULL,
    UNIQUE (student_id, case_type, sequence_no),
    CHECK (
        (
            status IN (
                'draft',
                'preparing',
                'in_progress',
                'waiting',
                'paused'
            )
            AND active_slot = 1
            AND terminal_result IS NULL
        )
        OR (
            status IN ('completed', 'cancelled', 'failed')
            AND active_slot = 0
            AND terminal_result = status
        )
        OR (
            status = 'archived'
            AND active_slot = 0
            AND terminal_result IN ('completed', 'cancelled', 'failed')
        )
    )
);

CREATE UNIQUE INDEX uq_student_cases_one_active_per_student
    ON student_cases(student_id)
    WHERE active_slot = 1;

CREATE INDEX ix_student_cases_student_type_sequence
    ON student_cases(student_id, case_type, sequence_no DESC);

CREATE INDEX ix_student_cases_student_status
    ON student_cases(student_id, status);

CREATE TRIGGER student_cases_no_delete
BEFORE DELETE ON student_cases
BEGIN
    SELECT RAISE(ABORT, 'student cases are retained permanently');
END;

CREATE TRIGGER student_cases_archive_from_terminal_only
BEFORE UPDATE OF status ON student_cases
WHEN NEW.status = 'archived'
 AND OLD.status NOT IN ('completed', 'cancelled', 'failed')
BEGIN
    SELECT RAISE(ABORT, 'active student case cannot be archived');
END;

CREATE TABLE student_case_assignments (
    id INTEGER PRIMARY KEY,
    case_id INTEGER NOT NULL
        REFERENCES student_cases(id) ON DELETE RESTRICT,
    user_id INTEGER NOT NULL
        REFERENCES users(id) ON DELETE RESTRICT,
    role_type TEXT NOT NULL
        CHECK (role_type IN ('owner', 'collaborator', 'watcher')),
    effective_from TEXT NOT NULL,
    effective_to TEXT,
    created_by INTEGER NOT NULL
        REFERENCES users(id) ON DELETE RESTRICT,
    created_at TEXT NOT NULL,
    ended_by INTEGER
        REFERENCES users(id) ON DELETE RESTRICT,
    CHECK (effective_to IS NULL OR effective_to >= effective_from),
    CHECK (
        (effective_to IS NULL AND ended_by IS NULL)
        OR (effective_to IS NOT NULL AND ended_by IS NOT NULL)
    )
);

CREATE UNIQUE INDEX uq_student_case_assignments_current_role_user
    ON student_case_assignments(case_id, role_type, user_id)
    WHERE effective_to IS NULL;

CREATE UNIQUE INDEX uq_student_case_assignments_one_current_owner
    ON student_case_assignments(case_id)
    WHERE role_type = 'owner' AND effective_to IS NULL;

CREATE INDEX ix_student_case_assignments_current
    ON student_case_assignments(
        case_id,
        effective_to,
        role_type,
        user_id
    );

CREATE TRIGGER student_case_assignments_no_delete
BEFORE DELETE ON student_case_assignments
BEGIN
    SELECT RAISE(
        ABORT,
        'student case assignments are retained permanently'
    );
END;

CREATE TRIGGER student_case_assignments_identity_immutable
BEFORE UPDATE ON student_case_assignments
WHEN NEW.id IS NOT OLD.id
  OR NEW.case_id IS NOT OLD.case_id
  OR NEW.user_id IS NOT OLD.user_id
  OR NEW.role_type IS NOT OLD.role_type
  OR NEW.effective_from IS NOT OLD.effective_from
  OR NEW.created_by IS NOT OLD.created_by
  OR NEW.created_at IS NOT OLD.created_at
  OR OLD.effective_to IS NOT NULL
  OR NEW.effective_to IS NULL
  OR NEW.ended_by IS NULL
BEGIN
    SELECT RAISE(
        ABORT,
        'assignment update may only end a current assignment once'
    );
END;

CREATE TABLE case_events (
    id INTEGER PRIMARY KEY,
    case_id INTEGER NOT NULL
        REFERENCES student_cases(id) ON DELETE RESTRICT,
    event_type TEXT NOT NULL
        CHECK (length(trim(event_type)) > 0),
    entity_type TEXT,
    entity_id TEXT,
    old_value_json TEXT
        CHECK (old_value_json IS NULL OR json_valid(old_value_json)),
    new_value_json TEXT
        CHECK (new_value_json IS NULL OR json_valid(new_value_json)),
    metadata_json TEXT DEFAULT '{}'
        CHECK (metadata_json IS NULL OR json_valid(metadata_json)),
    actor_type TEXT NOT NULL
        CHECK (actor_type IN ('user', 'system')),
    actor_user_id INTEGER
        REFERENCES users(id) ON DELETE RESTRICT,
    request_id TEXT,
    correlation_id TEXT,
    correction_of_event_id INTEGER
        REFERENCES case_events(id) ON DELETE RESTRICT,
    created_at TEXT NOT NULL,
    CHECK (
        (actor_type = 'user' AND actor_user_id IS NOT NULL)
        OR (actor_type = 'system' AND actor_user_id IS NULL)
    )
);

CREATE INDEX ix_case_events_case_created
    ON case_events(case_id, created_at, id);

CREATE INDEX ix_case_events_correlation
    ON case_events(correlation_id)
    WHERE correlation_id IS NOT NULL;

CREATE TRIGGER case_events_no_update
BEFORE UPDATE ON case_events
BEGIN
    SELECT RAISE(ABORT, 'case events are append-only');
END;

CREATE TRIGGER case_events_no_delete
BEFORE DELETE ON case_events
BEGIN
    SELECT RAISE(ABORT, 'case events are append-only');
END;

CREATE TABLE student_school_enrolments (
    id INTEGER PRIMARY KEY,
    student_id TEXT NOT NULL
        REFERENCES students(student_id) ON DELETE RESTRICT,
    school_id INTEGER
        REFERENCES schools(id) ON DELETE SET NULL,
    school_name_snapshot TEXT NOT NULL
        CHECK (length(trim(school_name_snapshot)) > 0),
    institution_group_id INTEGER,
    city_key TEXT
        CHECK (
            city_key IS NULL
            OR (
                city_key = lower(city_key)
                AND city_key NOT GLOB '*[^a-z0-9_-]*'
            )
        ),
    campus_key TEXT,
    school_sector TEXT
        CHECK (
            school_sector IS NULL
            OR school_sector IN (
                'government',
                'independent',
                'catholic',
                'other'
            )
        ),
    enrolment_role TEXT NOT NULL
        CHECK (enrolment_role IN ('current', 'upcoming', 'historical')),
    course_name_snapshot TEXT,
    start_date TEXT,
    end_date TEXT,
    source_case_id INTEGER
        REFERENCES student_cases(id) ON DELETE RESTRICT,
    status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'ended', 'cancelled', 'reverted')),
    created_by INTEGER NOT NULL
        REFERENCES users(id) ON DELETE RESTRICT,
    created_at TEXT NOT NULL,
    updated_by INTEGER NOT NULL
        REFERENCES users(id) ON DELETE RESTRICT,
    updated_at TEXT NOT NULL,
    CHECK (
        end_date IS NULL
        OR start_date IS NULL
        OR end_date >= start_date
    )
);

CREATE UNIQUE INDEX uq_student_school_enrolments_one_current
    ON student_school_enrolments(student_id)
    WHERE enrolment_role = 'current';

CREATE UNIQUE INDEX uq_student_school_enrolments_one_upcoming
    ON student_school_enrolments(student_id)
    WHERE enrolment_role = 'upcoming';

CREATE INDEX ix_student_school_enrolments_student_history
    ON student_school_enrolments(
        student_id,
        enrolment_role,
        status,
        start_date
    );

CREATE TRIGGER student_school_enrolments_no_delete
BEFORE DELETE ON student_school_enrolments
BEGIN
    SELECT RAISE(
        ABORT,
        'student school enrolments are retained permanently'
    );
END;
