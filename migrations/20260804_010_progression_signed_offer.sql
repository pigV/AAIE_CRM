-- migration_mode: sqlite_fk_off_rebuild

CREATE TABLE progression_document_links_rebuild_010 (
    id INTEGER PRIMARY KEY,
    case_id INTEGER NOT NULL REFERENCES progression_cases(case_id) ON DELETE RESTRICT,
    target_application_id INTEGER REFERENCES progression_target_applications(id) ON DELETE RESTRICT,
    document_role TEXT NOT NULL CHECK (document_role IN (
        'application_material','offer','signed_offer','payment_receipt','coe',
        'latest_transcript','oshc','visa'
    )),
    student_document_id INTEGER REFERENCES student_documents(id) ON DELETE RESTRICT,
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
        (status = 'not_applicable' AND student_document_id IS NULL
         AND length(trim(COALESCE(not_applicable_reason,''))) > 0)
        OR
        (status <> 'not_applicable' AND student_document_id IS NOT NULL
         AND not_applicable_reason IS NULL)
    ),
    CHECK (
        (document_role IN ('offer','signed_offer','payment_receipt','coe')
         AND target_application_id IS NOT NULL)
        OR
        (document_role NOT IN ('offer','signed_offer','payment_receipt','coe')
         AND target_application_id IS NULL)
    )
);

INSERT INTO progression_document_links_rebuild_010(
    id,case_id,target_application_id,document_role,student_document_id,status,
    is_current,not_applicable_reason,notes,created_by,created_at,updated_by,updated_at
)
SELECT id,case_id,target_application_id,document_role,student_document_id,status,
       is_current,not_applicable_reason,notes,created_by,created_at,updated_by,updated_at
FROM progression_document_links;

DROP TABLE progression_document_links;
ALTER TABLE progression_document_links_rebuild_010 RENAME TO progression_document_links;

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
