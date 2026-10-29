ALTER TABLE progression_case_offers
    ADD COLUMN target_application_id INTEGER
        REFERENCES progression_target_applications(id) ON DELETE RESTRICT;

CREATE TABLE progression_target_applications (
    id INTEGER PRIMARY KEY,
    case_id INTEGER NOT NULL
        REFERENCES progression_cases(case_id) ON DELETE RESTRICT,
    institution_id INTEGER REFERENCES schools(id) ON DELETE RESTRICT,
    institution_name_snapshot TEXT NOT NULL
        CHECK (length(trim(institution_name_snapshot)) > 0),
    course_type TEXT NOT NULL CHECK (course_type IN (
        'secondary','foundation','diploma','bachelor','postgraduate','other'
    )),
    course_name TEXT NOT NULL CHECK (length(trim(course_name)) > 0),
    expected_start_date TEXT
        CHECK (expected_start_date IS NULL OR date(expected_start_date) IS NOT NULL),
    application_status TEXT NOT NULL DEFAULT 'planning' CHECK (
        application_status IN (
            'planning','preparing','submitted','offer_received',
            'offer_accepted','withdrawn','not_proceeding'
        )
    ),
    is_final INTEGER NOT NULL DEFAULT 0 CHECK (is_final IN (0,1)),
    final_selected_at TEXT,
    notes TEXT,
    created_by INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    created_at TEXT NOT NULL,
    updated_by INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    updated_at TEXT NOT NULL,
    CHECK (
        (is_final = 1 AND final_selected_at IS NOT NULL)
        OR (is_final = 0 AND final_selected_at IS NULL)
    )
);

CREATE INDEX ix_progression_target_applications_case_status
    ON progression_target_applications(case_id, application_status, id);
CREATE UNIQUE INDEX uq_progression_target_applications_one_final
    ON progression_target_applications(case_id) WHERE is_final = 1;

CREATE TRIGGER progression_target_applications_no_delete
BEFORE DELETE ON progression_target_applications
BEGIN
    SELECT RAISE(ABORT, 'progression target applications are retained permanently');
END;

CREATE TABLE progression_document_links (
    id INTEGER PRIMARY KEY,
    case_id INTEGER NOT NULL
        REFERENCES progression_cases(case_id) ON DELETE RESTRICT,
    target_application_id INTEGER
        REFERENCES progression_target_applications(id) ON DELETE RESTRICT,
    document_role TEXT NOT NULL CHECK (document_role IN (
        'application_material','offer','payment_receipt','coe',
        'latest_transcript','oshc','visa'
    )),
    student_document_id INTEGER
        REFERENCES student_documents(id) ON DELETE RESTRICT,
    status TEXT NOT NULL DEFAULT 'received' CHECK (status IN (
        'pending','received','verified','confirmed','not_applicable','historical'
    )),
    is_current INTEGER NOT NULL DEFAULT 1 CHECK (is_current IN (0,1)),
    not_applicable_reason TEXT,
    notes TEXT,
    created_by INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    created_at TEXT NOT NULL,
    updated_by INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    updated_at TEXT NOT NULL,
    CHECK (
        (status = 'not_applicable'
         AND student_document_id IS NULL
         AND length(trim(COALESCE(not_applicable_reason,''))) > 0)
        OR
        (status <> 'not_applicable'
         AND student_document_id IS NOT NULL
         AND not_applicable_reason IS NULL)
    ),
    CHECK (
        (document_role IN ('offer','payment_receipt','coe')
         AND target_application_id IS NOT NULL)
        OR
        (document_role NOT IN ('offer','payment_receipt','coe')
         AND target_application_id IS NULL)
    )
);

CREATE INDEX ix_progression_document_links_case_role
    ON progression_document_links(case_id, document_role, status, id);
CREATE INDEX ix_progression_document_links_target_role
    ON progression_document_links(target_application_id, document_role, status, id);
CREATE UNIQUE INDEX uq_progression_document_links_current
    ON progression_document_links(
        case_id, COALESCE(target_application_id, 0), document_role
    ) WHERE is_current = 1;

CREATE TRIGGER progression_document_links_scope_insert
BEFORE INSERT ON progression_document_links
WHEN (NEW.target_application_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM progression_target_applications a
        WHERE a.id = NEW.target_application_id AND a.case_id = NEW.case_id
    ))
 OR (NEW.student_document_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM student_documents d
        JOIN student_cases c ON c.id = NEW.case_id
        WHERE d.id = NEW.student_document_id AND d.student_id = c.student_id
    ))
BEGIN
    SELECT RAISE(ABORT, 'progression document relationship mismatch');
END;

CREATE TRIGGER progression_document_links_scope_update
BEFORE UPDATE OF case_id,target_application_id,student_document_id
ON progression_document_links
WHEN (NEW.target_application_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM progression_target_applications a
        WHERE a.id = NEW.target_application_id AND a.case_id = NEW.case_id
    ))
 OR (NEW.student_document_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM student_documents d
        JOIN student_cases c ON c.id = NEW.case_id
        WHERE d.id = NEW.student_document_id AND d.student_id = c.student_id
    ))
BEGIN
    SELECT RAISE(ABORT, 'progression document relationship mismatch');
END;

CREATE TRIGGER progression_document_links_no_delete
BEFORE DELETE ON progression_document_links
BEGIN
    SELECT RAISE(ABORT, 'progression document links are retained permanently');
END;

CREATE TABLE progression_case_arrangements (
    id INTEGER PRIMARY KEY,
    case_id INTEGER NOT NULL
        REFERENCES progression_cases(case_id) ON DELETE RESTRICT,
    arrangement_type TEXT NOT NULL CHECK (arrangement_type IN ('oshc','visa')),
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN (
        'pending','confirmed','not_applicable','no_update_required'
    )),
    reason TEXT,
    notes TEXT,
    created_by INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    created_at TEXT NOT NULL,
    updated_by INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    updated_at TEXT NOT NULL,
    UNIQUE(case_id, arrangement_type),
    CHECK (
        status NOT IN ('not_applicable','no_update_required')
        OR length(trim(COALESCE(reason,''))) > 0
    )
);

CREATE INDEX ix_progression_case_arrangements_case_status
    ON progression_case_arrangements(case_id, arrangement_type, status);

CREATE TRIGGER progression_case_arrangements_no_delete
BEFORE DELETE ON progression_case_arrangements
BEGIN
    SELECT RAISE(ABORT, 'progression arrangements are retained permanently');
END;

CREATE TRIGGER progression_case_offers_target_scope_insert
BEFORE INSERT ON progression_case_offers
WHEN NEW.target_application_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM progression_target_applications a
    WHERE a.id = NEW.target_application_id AND a.case_id = NEW.case_id
)
BEGIN
    SELECT RAISE(ABORT, 'progression offer target application mismatch');
END;

CREATE TRIGGER progression_case_offers_target_scope_update
BEFORE UPDATE OF case_id,target_application_id ON progression_case_offers
WHEN NEW.target_application_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM progression_target_applications a
    WHERE a.id = NEW.target_application_id AND a.case_id = NEW.case_id
)
BEGIN
    SELECT RAISE(ABORT, 'progression offer target application mismatch');
END;
