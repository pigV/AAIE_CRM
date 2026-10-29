CREATE TABLE transfer_guardianship_arrangements (
    id INTEGER PRIMARY KEY,
    case_id INTEGER NOT NULL
        REFERENCES transfer_cases(case_id) ON DELETE RESTRICT,
    version_no INTEGER NOT NULL CHECK (version_no > 0),
    arrangement_mode TEXT NOT NULL
        CHECK (
            arrangement_mode IN (
                'parent_guardian',
                'nominated_guardian',
                'school_arranged',
                'caaw',
                'welfare_provider',
                'visa_guardian',
                'other',
                'pending_confirmation'
            )
        ),
    guardian_person_name TEXT,
    guardian_organisation_name TEXT,
    relationship_to_student TEXT,
    provider_name TEXT,
    start_date TEXT,
    end_date TEXT,
    source_type TEXT NOT NULL
        CHECK (
            source_type IN (
                'carried_forward',
                'staff_entered',
                'linked_existing_record',
                'updated_arrangement'
            )
        ),
    arrangement_status TEXT NOT NULL
        CHECK (
            arrangement_status IN (
                'pending',
                'carried_forward',
                'updated',
                'confirmed',
                'not_applicable'
            )
        ),
    is_current INTEGER NOT NULL DEFAULT 1 CHECK (is_current IN (0, 1)),
    superseded_by INTEGER,
    notes TEXT,
    created_by INTEGER NOT NULL
        REFERENCES users(id) ON DELETE RESTRICT,
    created_at TEXT NOT NULL,
    updated_by INTEGER
        REFERENCES users(id) ON DELETE RESTRICT,
    updated_at TEXT NOT NULL,
    UNIQUE(case_id, id),
    CHECK (
        (is_current = 1 AND superseded_by IS NULL)
        OR (is_current = 0 AND superseded_by IS NOT NULL)
    ),
    CHECK (superseded_by IS NULL OR superseded_by <> id),
    CHECK (
        start_date IS NULL
        OR (
            length(start_date) = 10
            AND start_date GLOB
                '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'
            AND date(start_date) IS NOT NULL
        )
    ),
    CHECK (
        end_date IS NULL
        OR (
            length(end_date) = 10
            AND end_date GLOB
                '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'
            AND date(end_date) IS NOT NULL
        )
    ),
    CHECK (start_date IS NULL OR end_date IS NULL OR end_date >= start_date),
    CHECK (
        arrangement_status <> 'not_applicable'
        OR length(trim(ifnull(notes, ''))) > 0
    ),
    CHECK (
        arrangement_status <> 'confirmed'
        OR (
            arrangement_mode IN ('parent_guardian', 'nominated_guardian')
            AND length(trim(ifnull(guardian_person_name, ''))) > 0
        )
        OR (
            arrangement_mode IN ('school_arranged', 'welfare_provider')
            AND (
                length(trim(ifnull(guardian_organisation_name, ''))) > 0
                OR length(trim(ifnull(provider_name, ''))) > 0
            )
        )
        OR (
            arrangement_mode = 'caaw'
            AND start_date IS NOT NULL
            AND end_date IS NOT NULL
        )
        OR (
            arrangement_mode IN ('visa_guardian', 'other')
            AND (
                length(trim(ifnull(guardian_person_name, ''))) > 0
                OR length(trim(ifnull(guardian_organisation_name, ''))) > 0
                OR length(trim(ifnull(provider_name, ''))) > 0
            )
        )
    ),
    FOREIGN KEY(case_id, superseded_by)
        REFERENCES transfer_guardianship_arrangements(case_id, id)
        ON DELETE RESTRICT
        DEFERRABLE INITIALLY DEFERRED
);

CREATE UNIQUE INDEX uq_transfer_guardianship_version
    ON transfer_guardianship_arrangements(case_id, version_no);

CREATE UNIQUE INDEX uq_transfer_guardianship_current
    ON transfer_guardianship_arrangements(case_id)
    WHERE is_current = 1;

CREATE INDEX ix_transfer_guardianship_case_history
    ON transfer_guardianship_arrangements(
        case_id,
        is_current DESC,
        version_no DESC,
        id DESC
    );

CREATE TRIGGER transfer_guardianship_arrangements_validate_insert
BEFORE INSERT ON transfer_guardianship_arrangements
BEGIN
    SELECT CASE
        WHEN NEW.superseded_by IS NOT NULL
         AND EXISTS (
            SELECT 1
            FROM transfer_guardianship_arrangements AS replacement
            WHERE replacement.id = NEW.superseded_by
              AND (
                  replacement.case_id <> NEW.case_id
                  OR replacement.is_current <> 1
                  OR replacement.version_no <= NEW.version_no
              )
         )
        THEN RAISE(
            ABORT,
            'guardianship replacement must be a newer current version in the same case'
        )
        WHEN NEW.is_current <> 1
         AND NEW.superseded_by IS NULL
        THEN RAISE(ABORT, 'guardianship history requires a replacement')
        WHEN EXISTS (
            SELECT 1
            FROM transfer_guardianship_arrangements AS predecessor
            WHERE predecessor.superseded_by = NEW.id
              AND (
                  NEW.is_current <> 1
                  OR predecessor.case_id <> NEW.case_id
                  OR predecessor.version_no >= NEW.version_no
              )
         )
        THEN RAISE(
            ABORT,
            'guardianship replacement must be current and newer than its predecessor'
        )
    END;
END;

CREATE TRIGGER transfer_guardianship_arrangements_validate_update
BEFORE UPDATE ON transfer_guardianship_arrangements
BEGIN
    SELECT CASE
        WHEN NEW.id IS NOT OLD.id
          OR NEW.case_id IS NOT OLD.case_id
          OR NEW.version_no IS NOT OLD.version_no
          OR NEW.arrangement_mode IS NOT OLD.arrangement_mode
          OR NEW.guardian_person_name IS NOT OLD.guardian_person_name
          OR NEW.guardian_organisation_name
                IS NOT OLD.guardian_organisation_name
          OR NEW.relationship_to_student IS NOT OLD.relationship_to_student
          OR NEW.provider_name IS NOT OLD.provider_name
          OR NEW.start_date IS NOT OLD.start_date
          OR NEW.end_date IS NOT OLD.end_date
          OR NEW.source_type IS NOT OLD.source_type
          OR NEW.arrangement_status IS NOT OLD.arrangement_status
          OR NEW.notes IS NOT OLD.notes
          OR NEW.created_by IS NOT OLD.created_by
          OR NEW.created_at IS NOT OLD.created_at
        THEN RAISE(ABORT, 'guardianship arrangement identity is immutable')
        WHEN OLD.is_current = 0 AND NEW.is_current <> 0
        THEN RAISE(ABORT, 'guardianship history cannot become current')
        WHEN OLD.superseded_by IS NOT NULL
         AND NEW.superseded_by IS NOT OLD.superseded_by
        THEN RAISE(ABORT, 'guardianship superseded link is immutable')
        WHEN OLD.is_current = 1
         AND NEW.is_current = 0
         AND (
             NEW.superseded_by IS NULL
             OR NEW.updated_by IS NULL
             OR NEW.updated_at IS OLD.updated_at
         )
        THEN RAISE(
            ABORT,
            'guardianship supersession requires replacement and update audit'
        )
        WHEN NEW.superseded_by IS NOT NULL
         AND EXISTS (
            SELECT 1
            FROM transfer_guardianship_arrangements AS replacement
            WHERE replacement.id = NEW.superseded_by
              AND (
                  replacement.case_id <> NEW.case_id
                  OR replacement.is_current <> 1
                  OR replacement.version_no <= NEW.version_no
              )
         )
        THEN RAISE(
            ABORT,
            'guardianship replacement must be a newer current version in the same case'
        )
    END;
END;

CREATE TRIGGER transfer_guardianship_arrangements_no_delete
BEFORE DELETE ON transfer_guardianship_arrangements
BEGIN
    SELECT RAISE(
        ABORT,
        'guardianship arrangements are retained permanently'
    );
END;

CREATE TABLE transfer_accommodation_arrangements (
    id INTEGER PRIMARY KEY,
    case_id INTEGER NOT NULL
        REFERENCES transfer_cases(case_id) ON DELETE RESTRICT,
    version_no INTEGER NOT NULL CHECK (version_no > 0),
    accommodation_type TEXT NOT NULL
        CHECK (
            accommodation_type IN (
                'homestay',
                'parent_or_guardian',
                'relative',
                'school_boarding',
                'private_rental',
                'provider_arranged',
                'other',
                'pending_confirmation'
            )
        ),
    provider_name TEXT,
    host_name TEXT,
    address_line1 TEXT,
    address_line2 TEXT,
    suburb TEXT,
    state_code TEXT,
    postcode TEXT,
    country_code TEXT,
    start_date TEXT,
    end_date TEXT,
    source_type TEXT NOT NULL
        CHECK (
            source_type IN (
                'carried_forward',
                'staff_entered',
                'linked_existing_record',
                'updated_arrangement'
            )
        ),
    arrangement_status TEXT NOT NULL
        CHECK (
            arrangement_status IN (
                'pending',
                'carried_forward',
                'updated',
                'confirmed',
                'not_applicable'
            )
        ),
    is_current INTEGER NOT NULL DEFAULT 1 CHECK (is_current IN (0, 1)),
    superseded_by INTEGER,
    notes TEXT,
    created_by INTEGER NOT NULL
        REFERENCES users(id) ON DELETE RESTRICT,
    created_at TEXT NOT NULL,
    updated_by INTEGER
        REFERENCES users(id) ON DELETE RESTRICT,
    updated_at TEXT NOT NULL,
    UNIQUE(case_id, id),
    CHECK (
        (is_current = 1 AND superseded_by IS NULL)
        OR (is_current = 0 AND superseded_by IS NOT NULL)
    ),
    CHECK (superseded_by IS NULL OR superseded_by <> id),
    CHECK (
        state_code IS NULL
        OR (
            length(state_code) BETWEEN 2 AND 3
            AND state_code = upper(state_code)
            AND state_code NOT GLOB '*[^A-Z]*'
        )
    ),
    CHECK (
        postcode IS NULL
        OR (
            length(trim(postcode)) BETWEEN 3 AND 10
            AND postcode NOT GLOB '*[^A-Za-z0-9 -]*'
        )
    ),
    CHECK (
        country_code IS NULL
        OR (
            length(country_code) = 2
            AND country_code = upper(country_code)
            AND country_code NOT GLOB '*[^A-Z]*'
        )
    ),
    CHECK (
        start_date IS NULL
        OR (
            length(start_date) = 10
            AND start_date GLOB
                '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'
            AND date(start_date) IS NOT NULL
        )
    ),
    CHECK (
        end_date IS NULL
        OR (
            length(end_date) = 10
            AND end_date GLOB
                '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'
            AND date(end_date) IS NOT NULL
        )
    ),
    CHECK (start_date IS NULL OR end_date IS NULL OR end_date >= start_date),
    CHECK (
        arrangement_status <> 'not_applicable'
        OR length(trim(ifnull(notes, ''))) > 0
    ),
    CHECK (
        arrangement_status <> 'confirmed'
        OR (
            accommodation_type IN ('homestay', 'provider_arranged')
            AND (
                length(trim(ifnull(provider_name, ''))) > 0
                OR length(trim(ifnull(host_name, ''))) > 0
            )
        )
        OR (
            accommodation_type IN ('parent_or_guardian', 'relative')
            AND (
                length(trim(ifnull(address_line1, ''))) > 0
                OR length(trim(ifnull(host_name, ''))) > 0
                OR length(trim(ifnull(notes, ''))) > 0
            )
        )
        OR (
            accommodation_type = 'school_boarding'
            AND (
                length(trim(ifnull(provider_name, ''))) > 0
                OR length(trim(ifnull(notes, ''))) > 0
            )
        )
        OR (
            accommodation_type = 'private_rental'
            AND length(trim(ifnull(address_line1, ''))) > 0
        )
        OR (
            accommodation_type = 'other'
            AND length(trim(ifnull(notes, ''))) > 0
        )
    ),
    FOREIGN KEY(case_id, superseded_by)
        REFERENCES transfer_accommodation_arrangements(case_id, id)
        ON DELETE RESTRICT
        DEFERRABLE INITIALLY DEFERRED
);

CREATE UNIQUE INDEX uq_transfer_accommodation_version
    ON transfer_accommodation_arrangements(case_id, version_no);

CREATE UNIQUE INDEX uq_transfer_accommodation_current
    ON transfer_accommodation_arrangements(case_id)
    WHERE is_current = 1;

CREATE INDEX ix_transfer_accommodation_case_history
    ON transfer_accommodation_arrangements(
        case_id,
        is_current DESC,
        version_no DESC,
        id DESC
    );

CREATE TRIGGER transfer_accommodation_arrangements_validate_insert
BEFORE INSERT ON transfer_accommodation_arrangements
BEGIN
    SELECT CASE
        WHEN NEW.superseded_by IS NOT NULL
         AND EXISTS (
            SELECT 1
            FROM transfer_accommodation_arrangements AS replacement
            WHERE replacement.id = NEW.superseded_by
              AND (
                  replacement.case_id <> NEW.case_id
                  OR replacement.is_current <> 1
                  OR replacement.version_no <= NEW.version_no
              )
         )
        THEN RAISE(
            ABORT,
            'accommodation replacement must be a newer current version in the same case'
        )
        WHEN EXISTS (
            SELECT 1
            FROM transfer_accommodation_arrangements AS predecessor
            WHERE predecessor.superseded_by = NEW.id
              AND (
                  NEW.is_current <> 1
                  OR predecessor.case_id <> NEW.case_id
                  OR predecessor.version_no >= NEW.version_no
              )
         )
        THEN RAISE(
            ABORT,
            'accommodation replacement must be current and newer than its predecessor'
        )
    END;
END;

CREATE TRIGGER transfer_accommodation_arrangements_validate_update
BEFORE UPDATE ON transfer_accommodation_arrangements
BEGIN
    SELECT CASE
        WHEN NEW.id IS NOT OLD.id
          OR NEW.case_id IS NOT OLD.case_id
          OR NEW.version_no IS NOT OLD.version_no
          OR NEW.accommodation_type IS NOT OLD.accommodation_type
          OR NEW.provider_name IS NOT OLD.provider_name
          OR NEW.host_name IS NOT OLD.host_name
          OR NEW.address_line1 IS NOT OLD.address_line1
          OR NEW.address_line2 IS NOT OLD.address_line2
          OR NEW.suburb IS NOT OLD.suburb
          OR NEW.state_code IS NOT OLD.state_code
          OR NEW.postcode IS NOT OLD.postcode
          OR NEW.country_code IS NOT OLD.country_code
          OR NEW.start_date IS NOT OLD.start_date
          OR NEW.end_date IS NOT OLD.end_date
          OR NEW.source_type IS NOT OLD.source_type
          OR NEW.arrangement_status IS NOT OLD.arrangement_status
          OR NEW.notes IS NOT OLD.notes
          OR NEW.created_by IS NOT OLD.created_by
          OR NEW.created_at IS NOT OLD.created_at
        THEN RAISE(ABORT, 'accommodation arrangement identity is immutable')
        WHEN OLD.is_current = 0 AND NEW.is_current <> 0
        THEN RAISE(ABORT, 'accommodation history cannot become current')
        WHEN OLD.superseded_by IS NOT NULL
         AND NEW.superseded_by IS NOT OLD.superseded_by
        THEN RAISE(ABORT, 'accommodation superseded link is immutable')
        WHEN OLD.is_current = 1
         AND NEW.is_current = 0
         AND (
             NEW.superseded_by IS NULL
             OR NEW.updated_by IS NULL
             OR NEW.updated_at IS OLD.updated_at
         )
        THEN RAISE(
            ABORT,
            'accommodation supersession requires replacement and update audit'
        )
        WHEN NEW.superseded_by IS NOT NULL
         AND EXISTS (
            SELECT 1
            FROM transfer_accommodation_arrangements AS replacement
            WHERE replacement.id = NEW.superseded_by
              AND (
                  replacement.case_id <> NEW.case_id
                  OR replacement.is_current <> 1
                  OR replacement.version_no <= NEW.version_no
              )
         )
        THEN RAISE(
            ABORT,
            'accommodation replacement must be a newer current version in the same case'
        )
    END;
END;

CREATE TRIGGER transfer_accommodation_arrangements_no_delete
BEFORE DELETE ON transfer_accommodation_arrangements
BEGIN
    SELECT RAISE(
        ABORT,
        'accommodation arrangements are retained permanently'
    );
END;

ALTER TABLE case_arrangement_confirmations
    ADD COLUMN guardianship_arrangement_id INTEGER
        REFERENCES transfer_guardianship_arrangements(id) ON DELETE RESTRICT;

ALTER TABLE case_arrangement_confirmations
    ADD COLUMN accommodation_arrangement_id INTEGER
        REFERENCES transfer_accommodation_arrangements(id) ON DELETE RESTRICT;

ALTER TABLE case_arrangement_confirmations
    ADD COLUMN rule_version TEXT;

ALTER TABLE case_arrangement_confirmations
    ADD COLUMN requirement_reason TEXT;

ALTER TABLE case_arrangement_confirmations
    ADD COLUMN updated_by INTEGER REFERENCES users(id) ON DELETE RESTRICT;

CREATE TABLE case_arrangement_evidence_links (
    id INTEGER PRIMARY KEY,
    case_id INTEGER NOT NULL
        REFERENCES transfer_cases(case_id) ON DELETE RESTRICT,
    arrangement_type TEXT NOT NULL
        CHECK (
            arrangement_type IN (
                'visa',
                'oshc',
                'guardianship',
                'accommodation'
            )
        ),
    arrangement_confirmation_id INTEGER
        REFERENCES case_arrangement_confirmations(id) ON DELETE RESTRICT,
    guardianship_arrangement_id INTEGER
        REFERENCES transfer_guardianship_arrangements(id) ON DELETE RESTRICT,
    accommodation_arrangement_id INTEGER
        REFERENCES transfer_accommodation_arrangements(id) ON DELETE RESTRICT,
    case_record_link_id INTEGER
        REFERENCES case_record_links(id) ON DELETE RESTRICT,
    document_id INTEGER
        REFERENCES student_documents(id) ON DELETE RESTRICT,
    evidence_role TEXT NOT NULL
        CHECK (
            evidence_role IN (
                'primary',
                'supporting',
                'identity',
                'authority',
                'coverage',
                'address',
                'current',
                'historical'
            )
        ),
    validity_status TEXT NOT NULL
        CHECK (
            validity_status IN (
                'current',
                'superseded',
                'historical',
                'rejected'
            )
        ),
    effective_from TEXT NOT NULL,
    superseded_by INTEGER
        REFERENCES case_arrangement_evidence_links(id) ON DELETE RESTRICT,
    created_by INTEGER NOT NULL
        REFERENCES users(id) ON DELETE RESTRICT,
    created_at TEXT NOT NULL,
    CHECK (
        (arrangement_confirmation_id IS NOT NULL)
        + (guardianship_arrangement_id IS NOT NULL)
        + (accommodation_arrangement_id IS NOT NULL)
        = 1
    ),
    CHECK (case_record_link_id IS NOT NULL OR document_id IS NOT NULL),
    CHECK (
        (validity_status = 'superseded' AND superseded_by IS NOT NULL)
        OR (validity_status <> 'superseded' AND superseded_by IS NULL)
    ),
    CHECK (superseded_by IS NULL OR superseded_by <> id)
);

CREATE INDEX ix_arrangement_evidence_case_type
    ON case_arrangement_evidence_links(
        case_id,
        arrangement_type,
        validity_status,
        evidence_role,
        created_at
    );

CREATE INDEX ix_arrangement_evidence_confirmation
    ON case_arrangement_evidence_links(arrangement_confirmation_id);

CREATE INDEX ix_arrangement_evidence_guardianship
    ON case_arrangement_evidence_links(guardianship_arrangement_id);

CREATE INDEX ix_arrangement_evidence_accommodation
    ON case_arrangement_evidence_links(accommodation_arrangement_id);

CREATE UNIQUE INDEX uq_arrangement_evidence_current_source
    ON case_arrangement_evidence_links(
        case_id,
        arrangement_type,
        ifnull(arrangement_confirmation_id, 0),
        ifnull(guardianship_arrangement_id, 0),
        ifnull(accommodation_arrangement_id, 0),
        ifnull(case_record_link_id, 0),
        ifnull(document_id, 0)
    )
    WHERE validity_status = 'current';

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
        THEN RAISE(
            ABORT,
            'evidence confirmation has an invalid case or arrangement type'
        )
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
        THEN RAISE(
            ABORT,
            'guardianship evidence has an invalid case or target'
        )
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
        THEN RAISE(
            ABORT,
            'accommodation evidence has an invalid case or target'
        )
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
        THEN RAISE(
            ABORT,
            'evidence record link has an invalid case or record type'
        )
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
              AND replacement.arrangement_confirmation_id
                    IS NEW.arrangement_confirmation_id
              AND replacement.guardianship_arrangement_id
                    IS NEW.guardianship_arrangement_id
              AND replacement.accommodation_arrangement_id
                    IS NEW.accommodation_arrangement_id
              AND replacement.validity_status = 'current'
         )
        THEN RAISE(
            ABORT,
            'evidence replacement must be current and have the same target'
        )
    END;
END;

CREATE TRIGGER case_arrangement_evidence_links_validate_update
BEFORE UPDATE ON case_arrangement_evidence_links
BEGIN
    SELECT CASE
        WHEN NEW.id IS NOT OLD.id
          OR NEW.case_id IS NOT OLD.case_id
          OR NEW.arrangement_type IS NOT OLD.arrangement_type
          OR NEW.arrangement_confirmation_id
                IS NOT OLD.arrangement_confirmation_id
          OR NEW.guardianship_arrangement_id
                IS NOT OLD.guardianship_arrangement_id
          OR NEW.accommodation_arrangement_id
                IS NOT OLD.accommodation_arrangement_id
          OR NEW.case_record_link_id IS NOT OLD.case_record_link_id
          OR NEW.document_id IS NOT OLD.document_id
          OR NEW.evidence_role IS NOT OLD.evidence_role
          OR NEW.effective_from IS NOT OLD.effective_from
          OR NEW.created_by IS NOT OLD.created_by
          OR NEW.created_at IS NOT OLD.created_at
        THEN RAISE(ABORT, 'arrangement evidence identity is immutable')
        WHEN OLD.validity_status IN ('superseded', 'historical', 'rejected')
         AND NEW.validity_status IS NOT OLD.validity_status
        THEN RAISE(ABORT, 'arrangement evidence history cannot become current')
        WHEN OLD.superseded_by IS NOT NULL
         AND NEW.superseded_by IS NOT OLD.superseded_by
        THEN RAISE(ABORT, 'arrangement evidence replacement is immutable')
        WHEN NEW.validity_status = 'superseded'
         AND NOT EXISTS (
            SELECT 1
            FROM case_arrangement_evidence_links AS replacement
            WHERE replacement.id = NEW.superseded_by
              AND replacement.case_id = NEW.case_id
              AND replacement.arrangement_type = NEW.arrangement_type
              AND replacement.arrangement_confirmation_id
                    IS NEW.arrangement_confirmation_id
              AND replacement.guardianship_arrangement_id
                    IS NEW.guardianship_arrangement_id
              AND replacement.accommodation_arrangement_id
                    IS NEW.accommodation_arrangement_id
              AND replacement.validity_status = 'current'
         )
        THEN RAISE(
            ABORT,
            'evidence replacement must be current and have the same target'
        )
    END;
END;

CREATE TRIGGER case_arrangement_evidence_links_no_delete
BEFORE DELETE ON case_arrangement_evidence_links
BEGIN
    SELECT RAISE(ABORT, 'arrangement evidence is retained permanently');
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
                  (NEW.arrangement_status = 'carried_forward'
                   AND link.validity_status = 'carried_forward')
                  OR (NEW.arrangement_status IN ('updated', 'confirmed')
                      AND link.validity_status IN ('current', 'carried_forward'))
                  OR NEW.arrangement_status = 'pending'
              )
         )
        THEN RAISE(
            ABORT,
            'visa or oshc confirmation record type or status does not match'
        )
        WHEN NEW.arrangement_type IN ('visa', 'oshc')
         AND NEW.arrangement_status IN (
             'carried_forward',
             'updated',
             'confirmed'
         )
         AND NEW.linked_record_id IS NULL
        THEN RAISE(ABORT, 'visa or oshc confirmation requires a record link')
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
        WHEN NEW.arrangement_type = 'guardianship'
         AND NEW.arrangement_status IN (
             'carried_forward',
             'updated',
             'confirmed'
         )
         AND NEW.guardianship_arrangement_id IS NULL
        THEN RAISE(
            ABORT,
            'guardianship confirmation requires a structured arrangement'
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
        WHEN NEW.arrangement_type = 'accommodation'
         AND NEW.arrangement_status IN (
             'carried_forward',
             'updated',
             'confirmed'
         )
         AND NEW.accommodation_arrangement_id IS NULL
        THEN RAISE(
            ABORT,
            'accommodation confirmation requires a structured arrangement'
        )
        WHEN NEW.arrangement_type = 'transcript_or_school_report'
         AND (
             NEW.guardianship_arrangement_id IS NOT NULL
             OR NEW.accommodation_arrangement_id IS NOT NULL
         )
        THEN RAISE(ABORT, 'material summary cannot use arrangement targets')
        WHEN NEW.arrangement_status = 'not_applicable'
         AND length(trim(ifnull(NEW.requirement_reason, ''))) = 0
        THEN RAISE(ABORT, 'not applicable confirmation requires a reason')
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
                 NEW.arrangement_status IN (
                     'carried_forward',
                     'updated',
                     'confirmed'
                 )
                 AND NEW.linked_record_id IS NULL
             )
             OR (
                 NEW.linked_record_id IS NOT NULL
                 AND NOT EXISTS (
                     SELECT 1
                     FROM case_record_links AS link
                     WHERE link.id = NEW.linked_record_id
                       AND link.case_id = NEW.case_id
                       AND link.record_type = NEW.arrangement_type
                       AND (
                           (NEW.arrangement_status = 'carried_forward'
                            AND link.validity_status = 'carried_forward')
                           OR (
                               NEW.arrangement_status IN ('updated', 'confirmed')
                               AND link.validity_status IN (
                                   'current',
                                   'carried_forward'
                               )
                           )
                           OR NEW.arrangement_status = 'pending'
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
                 NEW.arrangement_status IN (
                     'carried_forward',
                     'updated',
                     'confirmed'
                 )
                 AND NEW.guardianship_arrangement_id IS NULL
             )
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
                 NEW.arrangement_status IN (
                     'carried_forward',
                     'updated',
                     'confirmed'
                 )
                 AND NEW.accommodation_arrangement_id IS NULL
             )
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
        WHEN NEW.arrangement_status = 'not_applicable'
         AND length(trim(ifnull(NEW.requirement_reason, ''))) = 0
        THEN RAISE(ABORT, 'not applicable confirmation requires a reason')
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
    SELECT RAISE(
        ABORT,
        'record link conflicts with its arrangement confirmation'
    );
END;
