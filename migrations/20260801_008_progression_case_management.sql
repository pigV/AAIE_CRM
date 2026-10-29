CREATE TABLE progression_cases (
    case_id INTEGER PRIMARY KEY REFERENCES student_cases(id) ON DELETE RESTRICT,
    progression_type TEXT NOT NULL CHECK (progression_type IN (
        'foundation','foundation_bachelor_package','bachelor','diploma',
        'higher_education_pathway','other'
    )),
    workflow_stage TEXT NOT NULL DEFAULT 'planning' CHECK (workflow_stage IN (
        'planning','materials_collection','application_preparation',
        'application_submitted','offer_received','offer_accepted',
        'enrolment_preparation','completed'
    )),
    priority TEXT NOT NULL DEFAULT 'normal'
        CHECK (priority IN ('low','normal','high','urgent')),
    current_school_id INTEGER REFERENCES schools(id) ON DELETE RESTRICT,
    current_school_name_snapshot TEXT NOT NULL
        CHECK (length(trim(current_school_name_snapshot)) > 0),
    current_course_name TEXT NOT NULL
        CHECK (length(trim(current_course_name)) > 0),
    current_year_level TEXT NOT NULL
        CHECK (length(trim(current_year_level)) > 0),
    expected_graduation_date TEXT NOT NULL
        CHECK (date(expected_graduation_date) IS NOT NULL),
    target_education_stage TEXT NOT NULL
        CHECK (length(trim(target_education_stage)) > 0),
    target_course_type TEXT NOT NULL CHECK (target_course_type IN (
        'secondary','foundation','diploma','bachelor','postgraduate','other'
    )),
    target_institution_id INTEGER REFERENCES schools(id) ON DELETE RESTRICT,
    target_institution_name_snapshot TEXT NOT NULL
        CHECK (length(trim(target_institution_name_snapshot)) > 0),
    target_start_date TEXT NOT NULL CHECK (date(target_start_date) IS NOT NULL),
    notes TEXT,
    case_outcome TEXT CHECK (
        case_outcome IS NULL OR case_outcome IN (
            'completed','withdrawn','cancelled','superseded'
        )
    ),
    close_reason TEXT,
    closed_by INTEGER REFERENCES users(id) ON DELETE RESTRICT,
    closed_at TEXT,
    created_by INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    created_at TEXT NOT NULL,
    updated_by INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    updated_at TEXT NOT NULL,
    CHECK (
        (case_outcome IS NULL AND close_reason IS NULL
         AND closed_by IS NULL AND closed_at IS NULL)
        OR
        (case_outcome IS NOT NULL AND length(trim(close_reason)) > 0
         AND closed_by IS NOT NULL AND closed_at IS NOT NULL)
    )
);

CREATE INDEX ix_progression_cases_stage_priority
    ON progression_cases(workflow_stage, priority);
CREATE INDEX ix_progression_cases_target_start
    ON progression_cases(target_start_date);

CREATE TRIGGER progression_cases_parent_type_insert
BEFORE INSERT ON progression_cases
WHEN NOT EXISTS (
    SELECT 1 FROM student_cases c
    WHERE c.id = NEW.case_id AND c.case_type = 'progression'
)
BEGIN
    SELECT RAISE(ABORT, 'progression case requires progression parent');
END;

CREATE TRIGGER progression_cases_parent_type_update
BEFORE UPDATE OF case_id ON progression_cases
WHEN NOT EXISTS (
    SELECT 1 FROM student_cases c
    WHERE c.id = NEW.case_id AND c.case_type = 'progression'
)
BEGIN
    SELECT RAISE(ABORT, 'progression case requires progression parent');
END;

CREATE TRIGGER progression_cases_no_delete
BEFORE DELETE ON progression_cases
BEGIN
    SELECT RAISE(ABORT, 'progression cases are retained permanently');
END;

CREATE TABLE progression_case_stages (
    id INTEGER PRIMARY KEY,
    case_id INTEGER NOT NULL REFERENCES progression_cases(case_id) ON DELETE RESTRICT,
    sequence_no INTEGER NOT NULL CHECK (sequence_no > 0),
    course_type TEXT NOT NULL CHECK (course_type IN (
        'secondary','foundation','diploma','bachelor','postgraduate','other'
    )),
    course_name TEXT NOT NULL CHECK (length(trim(course_name)) > 0),
    institution_id INTEGER REFERENCES schools(id) ON DELETE RESTRICT,
    institution_name_snapshot TEXT NOT NULL
        CHECK (length(trim(institution_name_snapshot)) > 0),
    expected_start_date TEXT CHECK (
        expected_start_date IS NULL OR date(expected_start_date) IS NOT NULL
    ),
    expected_end_date TEXT CHECK (
        expected_end_date IS NULL OR date(expected_end_date) IS NOT NULL
    ),
    status TEXT NOT NULL DEFAULT 'planned'
        CHECK (status IN ('planned','applied','offered','accepted','withdrawn','completed')),
    is_packaged_with_next INTEGER NOT NULL DEFAULT 0
        CHECK (is_packaged_with_next IN (0,1)),
    notes TEXT,
    created_by INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    created_at TEXT NOT NULL,
    updated_by INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    updated_at TEXT NOT NULL,
    UNIQUE(case_id, sequence_no),
    CHECK (
        expected_end_date IS NULL OR expected_start_date IS NULL
        OR date(expected_end_date) >= date(expected_start_date)
    )
);

CREATE INDEX ix_progression_case_stages_case_status
    ON progression_case_stages(case_id, status, sequence_no);

CREATE TRIGGER progression_case_stages_no_delete
BEFORE DELETE ON progression_case_stages
BEGIN
    SELECT RAISE(ABORT, 'progression path stages are retained permanently');
END;

CREATE TABLE progression_case_materials (
    id INTEGER PRIMARY KEY,
    case_id INTEGER NOT NULL REFERENCES progression_cases(case_id) ON DELETE RESTRICT,
    material_key TEXT NOT NULL CHECK (length(trim(material_key)) > 0),
    material_name TEXT NOT NULL CHECK (length(trim(material_name)) > 0),
    status TEXT NOT NULL DEFAULT 'missing'
        CHECK (status IN ('missing','requested','received','verified','not_applicable','historical')),
    is_required INTEGER NOT NULL DEFAULT 1 CHECK (is_required IN (0,1)),
    student_document_id INTEGER REFERENCES student_documents(id) ON DELETE RESTRICT,
    upload_metadata_json TEXT NOT NULL DEFAULT '{}'
        CHECK (json_valid(upload_metadata_json)),
    notes TEXT,
    created_by INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    created_at TEXT NOT NULL,
    updated_by INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    updated_at TEXT NOT NULL,
    UNIQUE(case_id, material_key)
);

CREATE INDEX ix_progression_case_materials_case_status
    ON progression_case_materials(case_id, status, is_required);

CREATE TRIGGER progression_case_materials_document_student_insert
BEFORE INSERT ON progression_case_materials
WHEN NEW.student_document_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM student_documents d
    JOIN progression_cases p ON p.case_id = NEW.case_id
    JOIN student_cases c ON c.id = p.case_id
    WHERE d.id = NEW.student_document_id AND d.student_id = c.student_id
)
BEGIN
    SELECT RAISE(ABORT, 'progression material document student mismatch');
END;

CREATE TRIGGER progression_case_materials_document_student_update
BEFORE UPDATE OF student_document_id,case_id ON progression_case_materials
WHEN NEW.student_document_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM student_documents d
    JOIN progression_cases p ON p.case_id = NEW.case_id
    JOIN student_cases c ON c.id = p.case_id
    WHERE d.id = NEW.student_document_id AND d.student_id = c.student_id
)
BEGIN
    SELECT RAISE(ABORT, 'progression material document student mismatch');
END;

CREATE TRIGGER progression_case_materials_no_delete
BEFORE DELETE ON progression_case_materials
BEGIN
    SELECT RAISE(ABORT, 'progression materials are retained permanently');
END;

CREATE TABLE progression_case_offers (
    id INTEGER PRIMARY KEY,
    case_id INTEGER NOT NULL REFERENCES progression_cases(case_id) ON DELETE RESTRICT,
    stage_id INTEGER REFERENCES progression_case_stages(id) ON DELETE RESTRICT,
    existing_offer_id INTEGER REFERENCES offers(id) ON DELETE RESTRICT,
    offer_type TEXT NOT NULL CHECK (offer_type IN ('conditional','unconditional')),
    offer_status TEXT NOT NULL DEFAULT 'received'
        CHECK (offer_status IN ('received','accepted','declined','superseded','withdrawn')),
    institution_id INTEGER REFERENCES schools(id) ON DELETE RESTRICT,
    institution_name_snapshot TEXT NOT NULL
        CHECK (length(trim(institution_name_snapshot)) > 0),
    course_name TEXT NOT NULL CHECK (length(trim(course_name)) > 0),
    offer_date TEXT NOT NULL CHECK (date(offer_date) IS NOT NULL),
    expiry_date TEXT CHECK (expiry_date IS NULL OR date(expiry_date) IS NOT NULL),
    accepted_at TEXT,
    conditions_text TEXT,
    student_document_id INTEGER REFERENCES student_documents(id) ON DELETE RESTRICT,
    notes TEXT,
    created_by INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    created_at TEXT NOT NULL,
    updated_by INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    updated_at TEXT NOT NULL,
    CHECK (
        (offer_status = 'accepted' AND accepted_at IS NOT NULL)
        OR (offer_status <> 'accepted' AND accepted_at IS NULL)
    ),
    CHECK (
        offer_type = 'unconditional'
        OR length(trim(COALESCE(conditions_text,''))) > 0
    )
);

CREATE INDEX ix_progression_case_offers_case_status
    ON progression_case_offers(case_id, offer_status, offer_date);
CREATE UNIQUE INDEX uq_progression_case_offers_one_accepted
    ON progression_case_offers(case_id) WHERE offer_status = 'accepted';

CREATE TRIGGER progression_case_offers_scope_insert
BEFORE INSERT ON progression_case_offers
WHEN (NEW.stage_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM progression_case_stages s
        WHERE s.id = NEW.stage_id AND s.case_id = NEW.case_id
    ))
 OR (NEW.existing_offer_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM offers o
        JOIN progression_cases p ON p.case_id = NEW.case_id
        JOIN student_cases c ON c.id = p.case_id
        WHERE o.id = NEW.existing_offer_id AND o.student_id = c.student_id
    ))
 OR (NEW.student_document_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM student_documents d
        JOIN progression_cases p ON p.case_id = NEW.case_id
        JOIN student_cases c ON c.id = p.case_id
        WHERE d.id = NEW.student_document_id AND d.student_id = c.student_id
    ))
BEGIN
    SELECT RAISE(ABORT, 'progression offer relationship mismatch');
END;

CREATE TRIGGER progression_case_offers_scope_update
BEFORE UPDATE OF case_id,stage_id,existing_offer_id,student_document_id
ON progression_case_offers
WHEN (NEW.stage_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM progression_case_stages s
        WHERE s.id = NEW.stage_id AND s.case_id = NEW.case_id
    ))
 OR (NEW.existing_offer_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM offers o
        JOIN progression_cases p ON p.case_id = NEW.case_id
        JOIN student_cases c ON c.id = p.case_id
        WHERE o.id = NEW.existing_offer_id AND o.student_id = c.student_id
    ))
 OR (NEW.student_document_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM student_documents d
        JOIN progression_cases p ON p.case_id = NEW.case_id
        JOIN student_cases c ON c.id = p.case_id
        WHERE d.id = NEW.student_document_id AND d.student_id = c.student_id
    ))
BEGIN
    SELECT RAISE(ABORT, 'progression offer relationship mismatch');
END;

CREATE TRIGGER progression_case_offers_no_delete
BEFORE DELETE ON progression_case_offers
BEGIN
    SELECT RAISE(ABORT, 'progression offers are retained permanently');
END;

CREATE TABLE progression_case_events (
    id INTEGER PRIMARY KEY,
    case_id INTEGER NOT NULL REFERENCES progression_cases(case_id) ON DELETE RESTRICT,
    event_type TEXT NOT NULL CHECK (length(trim(event_type)) > 0),
    entity_type TEXT,
    entity_id TEXT,
    old_value_json TEXT CHECK (old_value_json IS NULL OR json_valid(old_value_json)),
    new_value_json TEXT CHECK (new_value_json IS NULL OR json_valid(new_value_json)),
    metadata_json TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(metadata_json)),
    actor_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    request_id TEXT,
    correlation_id TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX ix_progression_case_events_case_created
    ON progression_case_events(case_id, created_at, id);

CREATE TRIGGER progression_case_events_no_update
BEFORE UPDATE ON progression_case_events
BEGIN
    SELECT RAISE(ABORT, 'progression case events are append-only');
END;

CREATE TRIGGER progression_case_events_no_delete
BEFORE DELETE ON progression_case_events
BEGIN
    SELECT RAISE(ABORT, 'progression case events are append-only');
END;
