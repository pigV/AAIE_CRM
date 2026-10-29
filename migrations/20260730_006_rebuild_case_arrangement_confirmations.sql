-- migration_mode: sqlite_fk_off_rebuild

CREATE TABLE case_arrangement_confirmations_rebuild_006 (
    id INTEGER PRIMARY KEY,
    case_id INTEGER NOT NULL,
    arrangement_type TEXT NOT NULL,
    arrangement_status TEXT NOT NULL,
    linked_record_id INTEGER,
    document_id INTEGER,
    notes TEXT,
    confirmed_by INTEGER,
    confirmed_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    guardianship_arrangement_id INTEGER,
    accommodation_arrangement_id INTEGER,
    rule_version TEXT,
    requirement_reason TEXT,
    updated_by INTEGER
);

INSERT INTO case_arrangement_confirmations_rebuild_006(
    id,
    case_id,
    arrangement_type,
    arrangement_status,
    linked_record_id,
    document_id,
    notes,
    confirmed_by,
    confirmed_at,
    created_at,
    updated_at,
    guardianship_arrangement_id,
    accommodation_arrangement_id,
    rule_version,
    requirement_reason,
    updated_by
)
SELECT
    id,
    case_id,
    arrangement_type,
    arrangement_status,
    linked_record_id,
    document_id,
    notes,
    confirmed_by,
    confirmed_at,
    created_at,
    updated_at,
    guardianship_arrangement_id,
    accommodation_arrangement_id,
    rule_version,
    requirement_reason,
    updated_by
FROM case_arrangement_confirmations;

CREATE TABLE migration_006_confirmation_copy_guard (
    ok INTEGER NOT NULL CHECK (ok = 1)
);

INSERT INTO migration_006_confirmation_copy_guard(ok)
SELECT CASE
    WHEN (
        SELECT count(*) FROM case_arrangement_confirmations
    ) = (
        SELECT count(*) FROM case_arrangement_confirmations_rebuild_006
    )
    AND NOT EXISTS (
        SELECT 1
        FROM case_arrangement_confirmations AS old
        LEFT JOIN case_arrangement_confirmations_rebuild_006 AS rebuilt
          ON rebuilt.id = old.id
        WHERE rebuilt.id IS NULL
           OR rebuilt.case_id IS NOT old.case_id
           OR rebuilt.arrangement_type IS NOT old.arrangement_type
           OR rebuilt.arrangement_status IS NOT old.arrangement_status
           OR rebuilt.linked_record_id IS NOT old.linked_record_id
           OR rebuilt.document_id IS NOT old.document_id
           OR rebuilt.notes IS NOT old.notes
           OR rebuilt.confirmed_by IS NOT old.confirmed_by
           OR rebuilt.confirmed_at IS NOT old.confirmed_at
           OR rebuilt.created_at IS NOT old.created_at
           OR rebuilt.updated_at IS NOT old.updated_at
           OR rebuilt.guardianship_arrangement_id
                IS NOT old.guardianship_arrangement_id
           OR rebuilt.accommodation_arrangement_id
                IS NOT old.accommodation_arrangement_id
           OR rebuilt.rule_version IS NOT old.rule_version
           OR rebuilt.requirement_reason IS NOT old.requirement_reason
           OR rebuilt.updated_by IS NOT old.updated_by
    )
    THEN 1
    ELSE 0
END;

DROP TABLE migration_006_confirmation_copy_guard;

DROP TRIGGER case_arrangement_evidence_links_validate_insert;
DROP TRIGGER case_record_links_arrangement_confirmation_guard;
DROP TABLE case_arrangement_confirmations;

CREATE TABLE case_arrangement_confirmations (
    id INTEGER PRIMARY KEY,
    case_id INTEGER NOT NULL
        REFERENCES transfer_cases(case_id) ON DELETE RESTRICT,
    arrangement_type TEXT NOT NULL
        CHECK (
            arrangement_type IN (
                'visa',
                'oshc',
                'guardianship',
                'accommodation',
                'transcript_or_school_report'
            )
        ),
    arrangement_status TEXT NOT NULL DEFAULT 'pending'
        CHECK (
            arrangement_status IN (
                'pending',
                'carried_forward',
                'updated',
                'confirmed',
                'not_applicable'
            )
        ),
    linked_record_id INTEGER
        REFERENCES case_record_links(id) ON DELETE RESTRICT,
    document_id INTEGER
        REFERENCES student_documents(id) ON DELETE RESTRICT,
    notes TEXT,
    confirmed_by INTEGER
        REFERENCES users(id) ON DELETE RESTRICT,
    confirmed_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    guardianship_arrangement_id INTEGER
        REFERENCES transfer_guardianship_arrangements(id) ON DELETE RESTRICT,
    accommodation_arrangement_id INTEGER
        REFERENCES transfer_accommodation_arrangements(id) ON DELETE RESTRICT,
    rule_version TEXT,
    requirement_reason TEXT,
    updated_by INTEGER
        REFERENCES users(id) ON DELETE RESTRICT,
    UNIQUE(case_id, arrangement_type),
    CHECK (
        arrangement_status <> 'confirmed'
        OR (confirmed_by IS NOT NULL AND confirmed_at IS NOT NULL)
    ),
    CHECK (
        arrangement_status <> 'not_applicable'
        OR length(trim(ifnull(requirement_reason, ''))) > 0
    ),
    CHECK (
        (
            arrangement_type IN ('visa', 'oshc')
            AND guardianship_arrangement_id IS NULL
            AND accommodation_arrangement_id IS NULL
            AND (
                arrangement_status NOT IN (
                    'carried_forward',
                    'updated',
                    'confirmed'
                )
                OR linked_record_id IS NOT NULL
            )
        )
        OR (
            arrangement_type = 'guardianship'
            AND linked_record_id IS NULL
            AND accommodation_arrangement_id IS NULL
            AND (
                arrangement_status NOT IN (
                    'carried_forward',
                    'updated',
                    'confirmed'
                )
                OR guardianship_arrangement_id IS NOT NULL
            )
        )
        OR (
            arrangement_type = 'accommodation'
            AND linked_record_id IS NULL
            AND guardianship_arrangement_id IS NULL
            AND (
                arrangement_status NOT IN (
                    'carried_forward',
                    'updated',
                    'confirmed'
                )
                OR accommodation_arrangement_id IS NOT NULL
            )
        )
        OR (
            arrangement_type = 'transcript_or_school_report'
            AND guardianship_arrangement_id IS NULL
            AND accommodation_arrangement_id IS NULL
            AND (
                arrangement_status NOT IN ('carried_forward', 'updated')
                OR linked_record_id IS NOT NULL
                OR document_id IS NOT NULL
            )
        )
    )
);

INSERT INTO case_arrangement_confirmations(
    id,
    case_id,
    arrangement_type,
    arrangement_status,
    linked_record_id,
    document_id,
    notes,
    confirmed_by,
    confirmed_at,
    created_at,
    updated_at,
    guardianship_arrangement_id,
    accommodation_arrangement_id,
    rule_version,
    requirement_reason,
    updated_by
)
SELECT
    id,
    case_id,
    arrangement_type,
    arrangement_status,
    linked_record_id,
    document_id,
    notes,
    confirmed_by,
    confirmed_at,
    created_at,
    updated_at,
    guardianship_arrangement_id,
    accommodation_arrangement_id,
    rule_version,
    requirement_reason,
    updated_by
FROM case_arrangement_confirmations_rebuild_006;

CREATE TABLE migration_006_confirmation_final_guard (
    ok INTEGER NOT NULL CHECK (ok = 1)
);

INSERT INTO migration_006_confirmation_final_guard(ok)
SELECT CASE
    WHEN (
        SELECT count(*) FROM case_arrangement_confirmations_rebuild_006
    ) = (
        SELECT count(*) FROM case_arrangement_confirmations
    )
    AND NOT EXISTS (
        SELECT 1
        FROM case_arrangement_confirmations_rebuild_006 AS snapshot
        LEFT JOIN case_arrangement_confirmations AS rebuilt
          ON rebuilt.id = snapshot.id
        WHERE rebuilt.id IS NULL
           OR rebuilt.case_id IS NOT snapshot.case_id
           OR rebuilt.arrangement_type IS NOT snapshot.arrangement_type
           OR rebuilt.arrangement_status IS NOT snapshot.arrangement_status
           OR rebuilt.linked_record_id IS NOT snapshot.linked_record_id
           OR rebuilt.document_id IS NOT snapshot.document_id
           OR rebuilt.notes IS NOT snapshot.notes
           OR rebuilt.confirmed_by IS NOT snapshot.confirmed_by
           OR rebuilt.confirmed_at IS NOT snapshot.confirmed_at
           OR rebuilt.created_at IS NOT snapshot.created_at
           OR rebuilt.updated_at IS NOT snapshot.updated_at
           OR rebuilt.guardianship_arrangement_id
                IS NOT snapshot.guardianship_arrangement_id
           OR rebuilt.accommodation_arrangement_id
                IS NOT snapshot.accommodation_arrangement_id
           OR rebuilt.rule_version IS NOT snapshot.rule_version
           OR rebuilt.requirement_reason IS NOT snapshot.requirement_reason
           OR rebuilt.updated_by IS NOT snapshot.updated_by
    )
    THEN 1
    ELSE 0
END;

DROP TABLE migration_006_confirmation_final_guard;
DROP TABLE case_arrangement_confirmations_rebuild_006;

CREATE INDEX ix_case_arrangement_confirmations_case_status
    ON case_arrangement_confirmations(
        case_id,
        arrangement_status,
        arrangement_type
    );

CREATE TRIGGER case_arrangement_confirmations_validate_insert
BEFORE INSERT ON case_arrangement_confirmations
WHEN (
    NEW.linked_record_id IS NOT NULL
    AND NOT EXISTS (
        SELECT 1
        FROM case_record_links
        WHERE id = NEW.linked_record_id
          AND case_id = NEW.case_id
    )
)
OR (
    NEW.document_id IS NOT NULL
    AND NOT EXISTS (
        SELECT 1
        FROM student_documents
        JOIN student_cases ON student_cases.id = NEW.case_id
        WHERE student_documents.id = NEW.document_id
          AND student_documents.student_id = student_cases.student_id
    )
)
BEGIN
    SELECT RAISE(
        ABORT,
        'arrangement evidence belongs to another case or student'
    );
END;

CREATE TRIGGER case_arrangement_confirmations_validate_update
BEFORE UPDATE OF case_id, linked_record_id, document_id
    ON case_arrangement_confirmations
WHEN (
    NEW.linked_record_id IS NOT NULL
    AND NOT EXISTS (
        SELECT 1
        FROM case_record_links
        WHERE id = NEW.linked_record_id
          AND case_id = NEW.case_id
    )
)
OR (
    NEW.document_id IS NOT NULL
    AND NOT EXISTS (
        SELECT 1
        FROM student_documents
        JOIN student_cases ON student_cases.id = NEW.case_id
        WHERE student_documents.id = NEW.document_id
          AND student_documents.student_id = student_cases.student_id
    )
)
BEGIN
    SELECT RAISE(
        ABORT,
        'arrangement evidence belongs to another case or student'
    );
END;

CREATE TRIGGER case_arrangement_confirmations_no_delete
BEFORE DELETE ON case_arrangement_confirmations
BEGIN
    SELECT RAISE(
        ABORT,
        'case arrangement confirmations are retained permanently'
    );
END;

CREATE TRIGGER case_arrangement_confirmations_t7_validate_insert
BEFORE INSERT ON case_arrangement_confirmations
BEGIN
    SELECT CASE
        WHEN NEW.arrangement_type IN ('visa', 'oshc')
         AND (
             NEW.guardianship_arrangement_id IS NOT NULL
             OR NEW.accommodation_arrangement_id IS NOT NULL
         )
        THEN RAISE(
            ABORT,
            'visa and oshc confirmations only use case record links'
        )
        WHEN NEW.arrangement_type IN ('visa', 'oshc')
         AND NEW.linked_record_id IS NOT NULL
         AND NOT EXISTS (
            SELECT 1
            FROM case_record_links AS link
            WHERE link.id = NEW.linked_record_id
              AND link.case_id = NEW.case_id
              AND link.record_type = NEW.arrangement_type
              AND (
                  (
                      NEW.arrangement_status = 'carried_forward'
                      AND link.validity_status = 'carried_forward'
                  )
                  OR (
                      NEW.arrangement_status IN ('updated', 'confirmed')
                      AND link.validity_status IN (
                          'current',
                          'carried_forward'
                      )
                  )
                  OR NEW.arrangement_status IN (
                      'pending',
                      'not_applicable'
                  )
              )
         )
        THEN RAISE(
            ABORT,
            'visa or oshc confirmation record type or status does not match'
        )
        WHEN NEW.arrangement_type = 'guardianship'
         AND (
             NEW.linked_record_id IS NOT NULL
             OR NEW.accommodation_arrangement_id IS NOT NULL
         )
        THEN RAISE(ABORT, 'guardianship confirmation target type is invalid')
        WHEN NEW.arrangement_type = 'guardianship'
         AND NEW.guardianship_arrangement_id IS NOT NULL
         AND NOT EXISTS (
            SELECT 1
            FROM transfer_guardianship_arrangements AS arrangement
            WHERE arrangement.id = NEW.guardianship_arrangement_id
              AND arrangement.case_id = NEW.case_id
              AND arrangement.is_current = 1
              AND arrangement.arrangement_status = NEW.arrangement_status
         )
        THEN RAISE(
            ABORT,
            'guardianship confirmation requires the matching current version'
        )
        WHEN NEW.arrangement_type = 'accommodation'
         AND (
             NEW.linked_record_id IS NOT NULL
             OR NEW.guardianship_arrangement_id IS NOT NULL
         )
        THEN RAISE(ABORT, 'accommodation confirmation target type is invalid')
        WHEN NEW.arrangement_type = 'accommodation'
         AND NEW.accommodation_arrangement_id IS NOT NULL
         AND NOT EXISTS (
            SELECT 1
            FROM transfer_accommodation_arrangements AS arrangement
            WHERE arrangement.id = NEW.accommodation_arrangement_id
              AND arrangement.case_id = NEW.case_id
              AND arrangement.is_current = 1
              AND arrangement.arrangement_status = NEW.arrangement_status
         )
        THEN RAISE(
            ABORT,
            'accommodation confirmation requires the matching current version'
        )
        WHEN NEW.arrangement_type = 'transcript_or_school_report'
         AND (
             NEW.guardianship_arrangement_id IS NOT NULL
             OR NEW.accommodation_arrangement_id IS NOT NULL
         )
        THEN RAISE(ABORT, 'material summary cannot use arrangement targets')
    END;
END;

CREATE TRIGGER case_arrangement_confirmations_t7_validate_update
BEFORE UPDATE ON case_arrangement_confirmations
BEGIN
    SELECT CASE
        WHEN NEW.case_id IS NOT OLD.case_id
          OR NEW.arrangement_type IS NOT OLD.arrangement_type
        THEN RAISE(ABORT, 'arrangement confirmation identity is immutable')
        WHEN NEW.arrangement_type IN ('visa', 'oshc')
         AND (
             NEW.guardianship_arrangement_id IS NOT NULL
             OR NEW.accommodation_arrangement_id IS NOT NULL
             OR (
                 NEW.linked_record_id IS NOT NULL
                 AND NOT EXISTS (
                     SELECT 1
                     FROM case_record_links AS link
                     WHERE link.id = NEW.linked_record_id
                       AND link.case_id = NEW.case_id
                       AND link.record_type = NEW.arrangement_type
                       AND (
                           (
                               NEW.arrangement_status = 'carried_forward'
                               AND link.validity_status = 'carried_forward'
                           )
                           OR (
                               NEW.arrangement_status IN (
                                   'updated',
                                   'confirmed'
                               )
                               AND link.validity_status IN (
                                   'current',
                                   'carried_forward'
                               )
                           )
                           OR NEW.arrangement_status IN (
                               'pending',
                               'not_applicable'
                           )
                       )
                 )
             )
         )
        THEN RAISE(
            ABORT,
            'visa or oshc confirmation record type or status does not match'
        )
        WHEN NEW.arrangement_type = 'guardianship'
         AND (
             NEW.linked_record_id IS NOT NULL
             OR NEW.accommodation_arrangement_id IS NOT NULL
             OR (
                 NEW.guardianship_arrangement_id IS NOT NULL
                 AND NOT EXISTS (
                     SELECT 1
                     FROM transfer_guardianship_arrangements AS arrangement
                     WHERE arrangement.id = NEW.guardianship_arrangement_id
                       AND arrangement.case_id = NEW.case_id
                       AND arrangement.is_current = 1
                       AND arrangement.arrangement_status =
                           NEW.arrangement_status
                 )
             )
         )
        THEN RAISE(ABORT, 'guardianship confirmation target is invalid')
        WHEN NEW.arrangement_type = 'accommodation'
         AND (
             NEW.linked_record_id IS NOT NULL
             OR NEW.guardianship_arrangement_id IS NOT NULL
             OR (
                 NEW.accommodation_arrangement_id IS NOT NULL
                 AND NOT EXISTS (
                     SELECT 1
                     FROM transfer_accommodation_arrangements AS arrangement
                     WHERE arrangement.id = NEW.accommodation_arrangement_id
                       AND arrangement.case_id = NEW.case_id
                       AND arrangement.is_current = 1
                       AND arrangement.arrangement_status =
                           NEW.arrangement_status
                 )
             )
         )
        THEN RAISE(ABORT, 'accommodation confirmation target is invalid')
        WHEN NEW.arrangement_type = 'transcript_or_school_report'
         AND (
             NEW.guardianship_arrangement_id IS NOT NULL
             OR NEW.accommodation_arrangement_id IS NOT NULL
         )
        THEN RAISE(ABORT, 'material summary cannot use arrangement targets')
    END;
END;

CREATE TRIGGER case_arrangement_evidence_links_validate_insert
BEFORE INSERT ON case_arrangement_evidence_links
BEGIN
    SELECT CASE
        WHEN NEW.arrangement_confirmation_id IS NOT NULL
         AND NOT EXISTS (
            SELECT 1
            FROM case_arrangement_confirmations AS confirmation
            WHERE confirmation.id = NEW.arrangement_confirmation_id
              AND confirmation.case_id = NEW.case_id
              AND confirmation.arrangement_type = NEW.arrangement_type
              AND NEW.arrangement_type IN ('visa', 'oshc')
         )
        THEN RAISE(ABORT, 'evidence confirmation has an invalid case or arrangement type')
        WHEN NEW.guardianship_arrangement_id IS NOT NULL
         AND (
             NEW.arrangement_type <> 'guardianship'
             OR NOT EXISTS (
                 SELECT 1
                 FROM transfer_guardianship_arrangements AS arrangement
                 WHERE arrangement.id = NEW.guardianship_arrangement_id
                   AND arrangement.case_id = NEW.case_id
             )
         )
        THEN RAISE(ABORT, 'guardianship evidence has an invalid case or target')
        WHEN NEW.accommodation_arrangement_id IS NOT NULL
         AND (
             NEW.arrangement_type <> 'accommodation'
             OR NOT EXISTS (
                 SELECT 1
                 FROM transfer_accommodation_arrangements AS arrangement
                 WHERE arrangement.id = NEW.accommodation_arrangement_id
                   AND arrangement.case_id = NEW.case_id
             )
         )
        THEN RAISE(ABORT, 'accommodation evidence has an invalid case or target')
        WHEN NEW.case_record_link_id IS NOT NULL
         AND NOT EXISTS (
             SELECT 1
             FROM case_record_links AS link
             WHERE link.id = NEW.case_record_link_id
               AND link.case_id = NEW.case_id
               AND (
                   NEW.arrangement_type NOT IN ('visa', 'oshc')
                   OR link.record_type = NEW.arrangement_type
               )
         )
        THEN RAISE(ABORT, 'evidence record link has an invalid case or record type')
        WHEN NEW.document_id IS NOT NULL
         AND NOT EXISTS (
             SELECT 1
             FROM student_documents AS document
             JOIN student_cases AS parent ON parent.id = NEW.case_id
             WHERE document.id = NEW.document_id
               AND document.student_id = parent.student_id
         )
        THEN RAISE(ABORT, 'arrangement evidence belongs to another student')
        WHEN NEW.superseded_by IS NOT NULL
         AND NOT EXISTS (
             SELECT 1
             FROM case_arrangement_evidence_links AS replacement
             WHERE replacement.id = NEW.superseded_by
               AND replacement.case_id = NEW.case_id
               AND replacement.arrangement_type = NEW.arrangement_type
               AND replacement.arrangement_confirmation_id IS NEW.arrangement_confirmation_id
               AND replacement.guardianship_arrangement_id IS NEW.guardianship_arrangement_id
               AND replacement.accommodation_arrangement_id IS NEW.accommodation_arrangement_id
               AND replacement.validity_status = 'current'
         )
        THEN RAISE(ABORT, 'evidence replacement must be current and have the same target')
    END;
END;

CREATE TRIGGER case_record_links_arrangement_confirmation_guard
BEFORE UPDATE OF case_id, record_type, validity_status ON case_record_links
WHEN EXISTS (
    SELECT 1
    FROM case_arrangement_confirmations AS confirmation
    WHERE confirmation.linked_record_id = OLD.id
      AND (
          confirmation.case_id <> NEW.case_id
          OR confirmation.arrangement_type <> NEW.record_type
          OR (
              confirmation.arrangement_status = 'carried_forward'
              AND NEW.validity_status <> 'carried_forward'
          )
          OR (
              confirmation.arrangement_status IN ('updated', 'confirmed')
              AND NEW.validity_status NOT IN ('current', 'carried_forward')
          )
      )
)
BEGIN
    SELECT RAISE(ABORT, 'record link conflicts with its arrangement confirmation');
END;
