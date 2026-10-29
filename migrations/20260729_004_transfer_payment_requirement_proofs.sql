CREATE TABLE case_payment_requirement_proofs (
    id INTEGER PRIMARY KEY,
    case_id INTEGER NOT NULL
        REFERENCES transfer_cases(case_id) ON DELETE RESTRICT,
    transfer_application_id INTEGER NOT NULL
        REFERENCES transfer_school_applications(id) ON DELETE RESTRICT,
    payment_requirement_id INTEGER NOT NULL
        REFERENCES case_payment_requirements(id) ON DELETE RESTRICT,
    case_record_link_id INTEGER NOT NULL
        REFERENCES case_record_links(id) ON DELETE RESTRICT,
    validity_status TEXT NOT NULL
        CHECK (validity_status IN ('current', 'superseded', 'historical')),
    effective_from TEXT NOT NULL,
    superseded_by INTEGER,
    created_by INTEGER NOT NULL
        REFERENCES users(id) ON DELETE RESTRICT,
    created_at TEXT NOT NULL,
    CHECK (
        (validity_status = 'superseded' AND superseded_by IS NOT NULL)
        OR
        (validity_status <> 'superseded' AND superseded_by IS NULL)
    ),
    CHECK (superseded_by IS NULL OR superseded_by <> id),
    UNIQUE(payment_requirement_id, id),
    FOREIGN KEY(payment_requirement_id, superseded_by)
        REFERENCES case_payment_requirement_proofs(
            payment_requirement_id,
            id
        )
        ON DELETE RESTRICT
        DEFERRABLE INITIALLY DEFERRED
);

CREATE INDEX ix_payment_requirement_proofs_requirement
    ON case_payment_requirement_proofs(
        payment_requirement_id,
        validity_status,
        created_at
    );

CREATE INDEX ix_payment_requirement_proofs_case_application
    ON case_payment_requirement_proofs(
        case_id,
        transfer_application_id,
        validity_status
    );

CREATE INDEX ix_payment_requirement_proofs_link
    ON case_payment_requirement_proofs(case_record_link_id);

CREATE INDEX ix_payment_requirement_proofs_status
    ON case_payment_requirement_proofs(validity_status, created_at);

CREATE UNIQUE INDEX uq_payment_requirement_proofs_requirement_link
    ON case_payment_requirement_proofs(
        payment_requirement_id,
        case_record_link_id
    );

CREATE UNIQUE INDEX uq_payment_requirement_proofs_link
    ON case_payment_requirement_proofs(case_record_link_id);

CREATE UNIQUE INDEX uq_payment_requirement_proofs_current_requirement
    ON case_payment_requirement_proofs(payment_requirement_id)
    WHERE validity_status = 'current';

CREATE UNIQUE INDEX uq_payment_requirement_proofs_current_link
    ON case_payment_requirement_proofs(case_record_link_id)
    WHERE validity_status = 'current';

CREATE TRIGGER case_payment_requirement_proofs_validate_insert
BEFORE INSERT ON case_payment_requirement_proofs
BEGIN
    SELECT CASE
        WHEN NOT EXISTS (
            SELECT 1
            FROM case_payment_requirements AS requirement
            WHERE requirement.id = NEW.payment_requirement_id
              AND requirement.case_id = NEW.case_id
              AND requirement.transfer_application_id =
                  NEW.transfer_application_id
        )
        THEN RAISE(
            ABORT,
            'payment proof requirement belongs to another case or application'
        )
        WHEN NOT EXISTS (
            SELECT 1
            FROM case_record_links AS link
            WHERE link.id = NEW.case_record_link_id
              AND link.case_id = NEW.case_id
              AND link.transfer_application_id =
                  NEW.transfer_application_id
              AND link.transfer_application_id IS NOT NULL
              AND link.link_role = 'payment_proof'
              AND link.record_type IN ('payment', 'student_document')
              AND (
                  (
                      NEW.validity_status = 'current'
                      AND link.validity_status = 'current'
                  )
                  OR (
                      NEW.validity_status IN ('superseded', 'historical')
                      AND link.validity_status IN (
                          'current',
                          'superseded',
                          'historical'
                      )
                  )
              )
        )
        THEN RAISE(
            ABORT,
            'payment proof link has an invalid case, application, role, type, or status'
        )
        WHEN EXISTS (
            SELECT 1
            FROM case_record_links AS link
            JOIN student_cases AS parent ON parent.id = NEW.case_id
            LEFT JOIN payments AS payment
              ON link.record_type = 'payment'
             AND payment.id = link.record_id
            LEFT JOIN student_documents AS document
              ON link.record_type = 'student_document'
             AND document.id = link.record_id
            WHERE link.id = NEW.case_record_link_id
              AND (
                  (
                      link.record_type = 'payment'
                      AND (
                          payment.id IS NULL
                          OR payment.student_id <> parent.student_id
                      )
                  )
                  OR (
                      link.record_type = 'student_document'
                      AND (
                          document.id IS NULL
                          OR document.student_id <> parent.student_id
                          OR link.document_id <> document.id
                      )
                  )
              )
        )
        THEN RAISE(ABORT, 'payment proof belongs to another student')
        WHEN NEW.validity_status <> 'current'
         AND EXISTS (
            SELECT 1
            FROM case_payment_requirement_proofs AS predecessor
            WHERE predecessor.superseded_by = NEW.id
         )
        THEN RAISE(
            ABORT,
            'payment proof supersede target must be current when linked'
        )
    END;
END;

CREATE TRIGGER case_payment_requirement_proofs_validate_update
BEFORE UPDATE ON case_payment_requirement_proofs
BEGIN
    SELECT CASE
        WHEN NEW.case_id IS NOT OLD.case_id
          OR NEW.transfer_application_id IS NOT OLD.transfer_application_id
          OR NEW.payment_requirement_id IS NOT OLD.payment_requirement_id
          OR NEW.case_record_link_id IS NOT OLD.case_record_link_id
          OR NEW.created_by IS NOT OLD.created_by
          OR NEW.created_at IS NOT OLD.created_at
        THEN RAISE(ABORT, 'payment proof relation identity is immutable')
        WHEN OLD.validity_status IN ('superseded', 'historical')
         AND NEW.validity_status IS NOT OLD.validity_status
        THEN RAISE(ABORT, 'payment proof history cannot become current')
        WHEN OLD.superseded_by IS NOT NULL
         AND NEW.superseded_by IS NOT OLD.superseded_by
        THEN RAISE(ABORT, 'payment proof superseded link cannot be changed')
        WHEN NEW.validity_status = 'superseded'
         AND NEW.superseded_by IS NOT NULL
         AND EXISTS (
            SELECT 1
            FROM case_payment_requirement_proofs AS replacement
            WHERE replacement.id = NEW.superseded_by
              AND (
                  replacement.payment_requirement_id <>
                      NEW.payment_requirement_id
                  OR replacement.validity_status <> 'current'
              )
         )
        THEN RAISE(
            ABORT,
            'payment proof supersede target must be current and in the same requirement'
        )
        WHEN NOT EXISTS (
            SELECT 1
            FROM case_payment_requirements AS requirement
            WHERE requirement.id = NEW.payment_requirement_id
              AND requirement.case_id = NEW.case_id
              AND requirement.transfer_application_id =
                  NEW.transfer_application_id
        )
        THEN RAISE(
            ABORT,
            'payment proof requirement belongs to another case or application'
        )
        WHEN NOT EXISTS (
            SELECT 1
            FROM case_record_links AS link
            WHERE link.id = NEW.case_record_link_id
              AND link.case_id = NEW.case_id
              AND link.transfer_application_id =
                  NEW.transfer_application_id
              AND link.transfer_application_id IS NOT NULL
              AND link.link_role = 'payment_proof'
              AND link.record_type IN ('payment', 'student_document')
              AND (
                  (
                      NEW.validity_status = 'current'
                      AND link.validity_status = 'current'
                  )
                  OR (
                      NEW.validity_status IN ('superseded', 'historical')
                      AND link.validity_status IN (
                          'current',
                          'superseded',
                          'historical'
                      )
                  )
              )
        )
        THEN RAISE(
            ABORT,
            'payment proof link has an invalid case, application, role, type, or status'
        )
    END;
END;

CREATE TRIGGER case_record_links_payment_proof_relation_guard
BEFORE UPDATE OF link_role, validity_status ON case_record_links
WHEN EXISTS (
    SELECT 1
    FROM case_payment_requirement_proofs AS proof
    WHERE proof.case_record_link_id = OLD.id
      AND (
          NEW.link_role <> 'payment_proof'
          OR NEW.record_type NOT IN ('payment', 'student_document')
          OR NEW.transfer_application_id IS NULL
          OR (
              proof.validity_status = 'current'
              AND NEW.validity_status <> 'current'
          )
          OR (
              proof.validity_status IN ('superseded', 'historical')
              AND NEW.validity_status NOT IN (
                  'current',
                  'superseded',
                  'historical'
              )
          )
      )
)
BEGIN
    SELECT RAISE(
        ABORT,
        'payment proof record link conflicts with its requirement relation'
    );
END;

CREATE TRIGGER case_payment_requirement_proofs_no_delete
BEFORE DELETE ON case_payment_requirement_proofs
BEGIN
    SELECT RAISE(ABORT, 'payment proof relations are retained permanently');
END;
