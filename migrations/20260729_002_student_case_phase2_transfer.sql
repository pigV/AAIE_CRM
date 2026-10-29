CREATE TABLE transfer_cases (
    case_id INTEGER PRIMARY KEY
        REFERENCES student_cases(id) ON DELETE RESTRICT,
    transfer_origin TEXT NOT NULL DEFAULT 'internal'
        CHECK (transfer_origin IN ('internal', 'external')),
    source_contract_id INTEGER
        REFERENCES contracts(id) ON DELETE RESTRICT,
    workflow_state TEXT NOT NULL DEFAULT 'draft'
        CHECK (
            workflow_state IN (
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
    current_school_name_snapshot TEXT NOT NULL
        CHECK (length(trim(current_school_name_snapshot)) > 0),
    current_school_id INTEGER
        REFERENCES schools(id) ON DELETE SET NULL,
    current_city_key TEXT
        CHECK (
            current_city_key IS NULL
            OR (
                current_city_key = lower(current_city_key)
                AND current_city_key NOT GLOB '*[^a-z0-9_-]*'
            )
        ),
    current_school_sector TEXT
        CHECK (
            current_school_sector IS NULL
            OR current_school_sector IN ('public', 'private', 'unknown')
        ),
    target_city_key TEXT
        CHECK (
            target_city_key IS NULL
            OR (
                target_city_key = lower(target_city_key)
                AND target_city_key NOT GLOB '*[^a-z0-9_-]*'
            )
        ),
    target_school_sector TEXT
        CHECK (
            target_school_sector IS NULL
            OR target_school_sector IN ('public', 'private', 'unknown')
        ),
    rule_status TEXT NOT NULL DEFAULT 'pending_confirmation'
        CHECK (rule_status IN ('pending_confirmation', 'determined')),
    same_city_result TEXT
        CHECK (
            same_city_result IS NULL
            OR same_city_result IN ('same_city', 'cross_city', 'unknown')
        ),
    sector_transition TEXT
        CHECK (
            sector_transition IS NULL
            OR sector_transition IN (
                'public_to_public',
                'public_to_private',
                'private_to_public',
                'private_to_private',
                'unknown'
            )
        ),
    required_completion_document TEXT
        CHECK (
            required_completion_document IS NULL
            OR required_completion_document IN (
                'confirmation_letter',
                'coe',
                'pending_confirmation'
            )
        ),
    tuition_requirement TEXT
        CHECK (
            tuition_requirement IS NULL
            OR tuition_requirement IN (
                'not_applicable',
                'deposit_or_first_payment_required',
                'pending_confirmation'
            )
        ),
    final_application_id INTEGER,
    rule_calculated_at TEXT,
    rule_calculated_by INTEGER
        REFERENCES users(id) ON DELETE RESTRICT,
    rule_version TEXT NOT NULL
        CHECK (length(trim(rule_version)) > 0),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK (
        (transfer_origin = 'internal' AND source_contract_id IS NULL)
        OR (transfer_origin = 'external' AND source_contract_id IS NOT NULL)
    ),
    CHECK (
        (rule_status = 'pending_confirmation')
        OR (
            rule_status = 'determined'
            AND same_city_result IS NOT NULL
            AND sector_transition IS NOT NULL
            AND required_completion_document IS NOT NULL
            AND tuition_requirement IS NOT NULL
            AND rule_calculated_at IS NOT NULL
            AND rule_calculated_by IS NOT NULL
        )
    ),
    FOREIGN KEY(final_application_id, case_id)
        REFERENCES transfer_school_applications(id, case_id)
        ON DELETE RESTRICT
        DEFERRABLE INITIALLY DEFERRED
);

CREATE UNIQUE INDEX uq_transfer_cases_external_contract
    ON transfer_cases(source_contract_id)
    WHERE source_contract_id IS NOT NULL;

CREATE INDEX ix_transfer_cases_workflow
    ON transfer_cases(workflow_state, updated_at, case_id);

CREATE TRIGGER transfer_cases_require_transfer_parent_insert
BEFORE INSERT ON transfer_cases
WHEN NOT EXISTS (
    SELECT 1
    FROM student_cases
    WHERE id = NEW.case_id
      AND case_type = 'transfer'
)
BEGIN
    SELECT RAISE(ABORT, 'transfer case requires transfer student case');
END;

CREATE TRIGGER transfer_cases_require_transfer_parent_update
BEFORE UPDATE OF case_id ON transfer_cases
BEGIN
    SELECT RAISE(ABORT, 'transfer case identity is immutable');
END;

CREATE TRIGGER transfer_cases_final_application_insert_guard
BEFORE INSERT ON transfer_cases
WHEN NEW.final_application_id IS NOT NULL
 AND NOT EXISTS (
    SELECT 1
    FROM transfer_school_applications
    WHERE id = NEW.final_application_id
      AND case_id = NEW.case_id
      AND is_final = 1
)
BEGIN
    SELECT RAISE(ABORT, 'final application must be final and belong to case');
END;

CREATE TRIGGER transfer_cases_final_application_update_guard
BEFORE UPDATE OF final_application_id ON transfer_cases
WHEN (
    NEW.final_application_id IS NULL
    AND EXISTS (
        SELECT 1
        FROM transfer_school_applications
        WHERE case_id = NEW.case_id
          AND is_final = 1
    )
)
OR (
    NEW.final_application_id IS NOT NULL
    AND NOT EXISTS (
        SELECT 1
        FROM transfer_school_applications
        WHERE id = NEW.final_application_id
          AND case_id = NEW.case_id
          AND is_final = 1
    )
)
BEGIN
    SELECT RAISE(ABORT, 'final application must be final and belong to case');
END;

CREATE TRIGGER transfer_cases_no_delete
BEFORE DELETE ON transfer_cases
BEGIN
    SELECT RAISE(ABORT, 'transfer cases are retained permanently');
END;

CREATE TABLE transfer_school_applications (
    id INTEGER PRIMARY KEY,
    case_id INTEGER NOT NULL
        REFERENCES transfer_cases(case_id) ON DELETE RESTRICT,
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
            OR school_sector IN ('public', 'private', 'unknown')
        ),
    expected_start_date TEXT,
    preference_order INTEGER
        CHECK (preference_order IS NULL OR preference_order > 0),
    application_status TEXT NOT NULL DEFAULT 'preparing'
        CHECK (
            application_status IN (
                'draft',
                'preparing',
                'submitted',
                'waiting_offer',
                'offer_received',
                'waiting_payment',
                'payment_satisfied',
                'waiting_completion_document',
                'ready',
                'accepted',
                'rejected',
                'withdrawn',
                'cancelled'
            )
        ),
    notes TEXT,
    is_final INTEGER NOT NULL DEFAULT 0 CHECK (is_final IN (0, 1)),
    created_by INTEGER NOT NULL
        REFERENCES users(id) ON DELETE RESTRICT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(id, case_id)
);

CREATE UNIQUE INDEX uq_transfer_school_applications_one_final
    ON transfer_school_applications(case_id)
    WHERE is_final = 1;

CREATE UNIQUE INDEX uq_transfer_school_applications_active_preference
    ON transfer_school_applications(case_id, preference_order)
    WHERE preference_order IS NOT NULL
      AND application_status NOT IN ('rejected', 'withdrawn', 'cancelled');

CREATE INDEX ix_transfer_school_applications_case_list
    ON transfer_school_applications(
        case_id,
        is_final DESC,
        preference_order,
        application_status,
        id
    );

CREATE TRIGGER transfer_school_applications_identity_immutable
BEFORE UPDATE OF id, case_id ON transfer_school_applications
BEGIN
    SELECT RAISE(ABORT, 'transfer application identity is immutable');
END;

CREATE TRIGGER transfer_school_applications_sync_final_insert
AFTER INSERT ON transfer_school_applications
WHEN NEW.is_final = 1
BEGIN
    UPDATE transfer_cases
    SET final_application_id = NEW.id,
        target_city_key = NEW.city_key,
        target_school_sector = NEW.school_sector,
        updated_at = NEW.updated_at
    WHERE case_id = NEW.case_id;
END;

CREATE TRIGGER transfer_school_applications_sync_final_update
AFTER UPDATE OF is_final, city_key, school_sector ON transfer_school_applications
BEGIN
    UPDATE transfer_cases
    SET final_application_id = CASE
            WHEN NEW.is_final = 1 THEN NEW.id
            WHEN final_application_id = OLD.id THEN NULL
            ELSE final_application_id
        END,
        target_city_key = CASE
            WHEN NEW.is_final = 1 THEN NEW.city_key
            WHEN final_application_id = OLD.id THEN NULL
            ELSE target_city_key
        END,
        target_school_sector = CASE
            WHEN NEW.is_final = 1 THEN NEW.school_sector
            WHEN final_application_id = OLD.id THEN NULL
            ELSE target_school_sector
        END,
        updated_at = NEW.updated_at
    WHERE case_id = NEW.case_id
      AND (NEW.is_final = 1 OR final_application_id = OLD.id);
END;

CREATE TRIGGER transfer_school_applications_no_delete
BEFORE DELETE ON transfer_school_applications
BEGIN
    SELECT RAISE(ABORT, 'transfer applications are retained permanently');
END;

CREATE TABLE case_record_links (
    id INTEGER PRIMARY KEY,
    case_id INTEGER NOT NULL
        REFERENCES transfer_cases(case_id) ON DELETE RESTRICT,
    transfer_application_id INTEGER
        REFERENCES transfer_school_applications(id) ON DELETE RESTRICT,
    record_type TEXT NOT NULL
        CHECK (
            record_type IN (
                'offer',
                'coe',
                'confirmation_letter',
                'visa',
                'oshc',
                'payment',
                'student_document'
            )
        ),
    record_id INTEGER NOT NULL CHECK (record_id > 0),
    document_id INTEGER
        REFERENCES student_documents(id) ON DELETE RESTRICT,
    link_role TEXT NOT NULL
        CHECK (
            link_role IN (
                'evidence',
                'current',
                'historical',
                'carried_forward',
                'updated',
                'completion_document',
                'payment_proof'
            )
        ),
    validity_status TEXT NOT NULL DEFAULT 'pending_confirmation'
        CHECK (
            validity_status IN (
                'pending_confirmation',
                'current',
                'superseded',
                'historical',
                'rejected',
                'carried_forward'
            )
        ),
    effective_from TEXT NOT NULL,
    superseded_by INTEGER
        REFERENCES case_record_links(id) ON DELETE RESTRICT,
    created_by INTEGER NOT NULL
        REFERENCES users(id) ON DELETE RESTRICT,
    created_at TEXT NOT NULL,
    CHECK (
        (
            record_type IN (
                'offer',
                'coe',
                'confirmation_letter',
                'payment'
            )
            AND transfer_application_id IS NOT NULL
        )
        OR (
            record_type IN ('visa', 'oshc')
            AND transfer_application_id IS NULL
        )
        OR record_type = 'student_document'
    ),
    CHECK (
        record_type <> 'student_document'
        OR document_id = record_id
    ),
    CHECK (
        validity_status <> 'superseded'
        OR superseded_by IS NOT NULL
    )
);

CREATE UNIQUE INDEX uq_case_record_links_same_record_context
    ON case_record_links(
        case_id,
        ifnull(transfer_application_id, 0),
        record_type,
        record_id
    );

CREATE UNIQUE INDEX uq_case_record_links_current_application_record
    ON case_record_links(transfer_application_id, record_type)
    WHERE transfer_application_id IS NOT NULL
      AND record_type IN ('offer', 'coe', 'confirmation_letter')
      AND validity_status = 'current';

CREATE UNIQUE INDEX uq_case_record_links_active_case_record
    ON case_record_links(case_id, record_type)
    WHERE transfer_application_id IS NULL
      AND record_type IN ('visa', 'oshc')
      AND validity_status IN ('current', 'carried_forward');

CREATE INDEX ix_case_record_links_case_status
    ON case_record_links(
        case_id,
        transfer_application_id,
        validity_status,
        record_type,
        created_at
    );

CREATE TRIGGER case_record_links_validate_insert
BEFORE INSERT ON case_record_links
BEGIN
    SELECT CASE
        WHEN NEW.transfer_application_id IS NOT NULL
         AND NOT EXISTS (
            SELECT 1
            FROM transfer_school_applications
            WHERE id = NEW.transfer_application_id
              AND case_id = NEW.case_id
         )
        THEN RAISE(ABORT, 'record link application belongs to another case')
        WHEN NEW.record_type = 'offer'
         AND NOT EXISTS (
            SELECT 1
            FROM offers AS record
            JOIN student_cases AS parent
              ON parent.id = NEW.case_id
            WHERE record.id = NEW.record_id
              AND record.student_id = parent.student_id
         )
        THEN RAISE(ABORT, 'offer record is missing or belongs to another student')
        WHEN NEW.record_type = 'payment'
         AND NOT EXISTS (
            SELECT 1
            FROM payments AS record
            JOIN student_cases AS parent
              ON parent.id = NEW.case_id
            WHERE record.id = NEW.record_id
              AND record.student_id = parent.student_id
         )
        THEN RAISE(ABORT, 'payment record is missing or belongs to another student')
        WHEN NEW.record_type = 'coe'
         AND NOT EXISTS (
            SELECT 1
            FROM coe_oshc AS record
            JOIN student_cases AS parent
              ON parent.id = NEW.case_id
            WHERE record.id = NEW.record_id
              AND record.student_id = parent.student_id
              AND record.document_kind IN ('COE', 'COE_CAAW')
         )
        THEN RAISE(ABORT, 'coe record is missing, mistyped, or belongs to another student')
        WHEN NEW.record_type = 'confirmation_letter'
         AND NOT EXISTS (
            SELECT 1
            FROM coe_oshc AS record
            JOIN student_cases AS parent
              ON parent.id = NEW.case_id
            WHERE record.id = NEW.record_id
              AND record.student_id = parent.student_id
              AND record.document_kind = 'LetterOfConfirmation'
         )
        THEN RAISE(ABORT, 'confirmation letter is missing, mistyped, or belongs to another student')
        WHEN NEW.record_type = 'visa'
         AND NOT EXISTS (
            SELECT 1
            FROM visas AS record
            JOIN student_cases AS parent
              ON parent.id = NEW.case_id
            WHERE record.id = NEW.record_id
              AND record.student_id = parent.student_id
         )
        THEN RAISE(ABORT, 'visa record is missing or belongs to another student')
        WHEN NEW.record_type = 'oshc'
         AND NOT EXISTS (
            SELECT 1
            FROM coe_oshc AS record
            JOIN student_cases AS parent
              ON parent.id = NEW.case_id
            WHERE record.id = NEW.record_id
              AND record.student_id = parent.student_id
              AND record.document_kind = 'OSHC'
         )
        THEN RAISE(ABORT, 'oshc record is missing, mistyped, or belongs to another student')
        WHEN NEW.record_type = 'student_document'
         AND NOT EXISTS (
            SELECT 1
            FROM student_documents AS record
            JOIN student_cases AS parent
              ON parent.id = NEW.case_id
            WHERE record.id = NEW.record_id
              AND record.student_id = parent.student_id
         )
        THEN RAISE(ABORT, 'student document is missing or belongs to another student')
        WHEN NEW.document_id IS NOT NULL
         AND NOT EXISTS (
            SELECT 1
            FROM student_documents AS document
            JOIN student_cases AS parent
              ON parent.id = NEW.case_id
            WHERE document.id = NEW.document_id
              AND document.student_id = parent.student_id
         )
        THEN RAISE(ABORT, 'linked document belongs to another student')
    END;
END;

CREATE TRIGGER case_record_links_validate_update
BEFORE UPDATE ON case_record_links
BEGIN
    SELECT CASE
        WHEN NEW.case_id IS NOT OLD.case_id
          OR NEW.transfer_application_id IS NOT OLD.transfer_application_id
          OR NEW.record_type IS NOT OLD.record_type
          OR NEW.record_id IS NOT OLD.record_id
          OR NEW.document_id IS NOT OLD.document_id
          OR NEW.effective_from IS NOT OLD.effective_from
          OR NEW.created_by IS NOT OLD.created_by
          OR NEW.created_at IS NOT OLD.created_at
        THEN RAISE(ABORT, 'record link identity is immutable')
        WHEN NEW.transfer_application_id IS NOT NULL
         AND NOT EXISTS (
            SELECT 1
            FROM transfer_school_applications
            WHERE id = NEW.transfer_application_id
              AND case_id = NEW.case_id
         )
        THEN RAISE(ABORT, 'record link application belongs to another case')
        WHEN NEW.record_type = 'offer'
         AND NOT EXISTS (
            SELECT 1 FROM offers AS record
            JOIN student_cases AS parent ON parent.id = NEW.case_id
            WHERE record.id = NEW.record_id
              AND record.student_id = parent.student_id
         )
        THEN RAISE(ABORT, 'offer record is missing or belongs to another student')
        WHEN NEW.record_type = 'payment'
         AND NOT EXISTS (
            SELECT 1 FROM payments AS record
            JOIN student_cases AS parent ON parent.id = NEW.case_id
            WHERE record.id = NEW.record_id
              AND record.student_id = parent.student_id
         )
        THEN RAISE(ABORT, 'payment record is missing or belongs to another student')
        WHEN NEW.record_type = 'coe'
         AND NOT EXISTS (
            SELECT 1 FROM coe_oshc AS record
            JOIN student_cases AS parent ON parent.id = NEW.case_id
            WHERE record.id = NEW.record_id
              AND record.student_id = parent.student_id
              AND record.document_kind IN ('COE', 'COE_CAAW')
         )
        THEN RAISE(ABORT, 'coe record is missing, mistyped, or belongs to another student')
        WHEN NEW.record_type = 'confirmation_letter'
         AND NOT EXISTS (
            SELECT 1 FROM coe_oshc AS record
            JOIN student_cases AS parent ON parent.id = NEW.case_id
            WHERE record.id = NEW.record_id
              AND record.student_id = parent.student_id
              AND record.document_kind = 'LetterOfConfirmation'
         )
        THEN RAISE(ABORT, 'confirmation letter is missing, mistyped, or belongs to another student')
        WHEN NEW.record_type = 'visa'
         AND NOT EXISTS (
            SELECT 1 FROM visas AS record
            JOIN student_cases AS parent ON parent.id = NEW.case_id
            WHERE record.id = NEW.record_id
              AND record.student_id = parent.student_id
         )
        THEN RAISE(ABORT, 'visa record is missing or belongs to another student')
        WHEN NEW.record_type = 'oshc'
         AND NOT EXISTS (
            SELECT 1 FROM coe_oshc AS record
            JOIN student_cases AS parent ON parent.id = NEW.case_id
            WHERE record.id = NEW.record_id
              AND record.student_id = parent.student_id
              AND record.document_kind = 'OSHC'
         )
        THEN RAISE(ABORT, 'oshc record is missing, mistyped, or belongs to another student')
        WHEN NEW.record_type = 'student_document'
         AND NOT EXISTS (
            SELECT 1 FROM student_documents AS record
            JOIN student_cases AS parent ON parent.id = NEW.case_id
            WHERE record.id = NEW.record_id
              AND record.student_id = parent.student_id
         )
        THEN RAISE(ABORT, 'student document is missing or belongs to another student')
        WHEN NEW.document_id IS NOT NULL
         AND NOT EXISTS (
            SELECT 1 FROM student_documents AS document
            JOIN student_cases AS parent ON parent.id = NEW.case_id
            WHERE document.id = NEW.document_id
              AND document.student_id = parent.student_id
         )
        THEN RAISE(ABORT, 'linked document belongs to another student')
    END;
END;

CREATE TRIGGER case_record_links_no_delete
BEFORE DELETE ON case_record_links
BEGIN
    SELECT RAISE(ABORT, 'case record links are retained permanently');
END;

CREATE TABLE case_material_items (
    id INTEGER PRIMARY KEY,
    case_id INTEGER NOT NULL
        REFERENCES transfer_cases(case_id) ON DELETE RESTRICT,
    material_key TEXT NOT NULL
        CHECK (length(trim(material_key)) > 0),
    material_label_snapshot TEXT NOT NULL
        CHECK (length(trim(material_label_snapshot)) > 0),
    material_status TEXT NOT NULL
        CHECK (
            material_status IN (
                'available',
                'needs_update',
                'missing',
                'not_applicable',
                'confirmed'
            )
        ),
    document_id INTEGER
        REFERENCES student_documents(id) ON DELETE RESTRICT,
    source_type TEXT NOT NULL
        CHECK (
            source_type IN (
                'system_generated',
                'carried_forward',
                'staff_added'
            )
        ),
    is_required INTEGER NOT NULL DEFAULT 1 CHECK (is_required IN (0, 1)),
    confirmed_by INTEGER
        REFERENCES users(id) ON DELETE RESTRICT,
    confirmed_at TEXT,
    created_by INTEGER NOT NULL
        REFERENCES users(id) ON DELETE RESTRICT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(case_id, material_key),
    CHECK (
        material_status <> 'confirmed'
        OR (confirmed_by IS NOT NULL AND confirmed_at IS NOT NULL)
    )
);

CREATE INDEX ix_case_material_items_case_status
    ON case_material_items(case_id, is_required, material_status, material_key);

CREATE TRIGGER case_material_items_no_delete
BEFORE DELETE ON case_material_items
BEGIN
    SELECT RAISE(ABORT, 'case material items are retained permanently');
END;

CREATE TABLE case_payment_requirements (
    id INTEGER PRIMARY KEY,
    case_id INTEGER NOT NULL
        REFERENCES transfer_cases(case_id) ON DELETE RESTRICT,
    transfer_application_id INTEGER NOT NULL
        REFERENCES transfer_school_applications(id) ON DELETE RESTRICT,
    requirement_type TEXT NOT NULL
        CHECK (requirement_type IN ('deposit', 'first_payment', 'other')),
    source_offer_id INTEGER
        REFERENCES offers(id) ON DELETE RESTRICT,
    extracted_amount REAL
        CHECK (extracted_amount IS NULL OR extracted_amount >= 0),
    confirmed_amount REAL
        CHECK (confirmed_amount IS NULL OR confirmed_amount >= 0),
    currency TEXT
        CHECK (
            currency IS NULL
            OR (
                length(currency) = 3
                AND currency = upper(currency)
                AND currency NOT GLOB '*[^A-Z]*'
            )
        ),
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'paid', 'not_applicable', 'refunded')),
    modification_reason TEXT,
    confirmed_by INTEGER
        REFERENCES users(id) ON DELETE RESTRICT,
    confirmed_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(transfer_application_id, requirement_type),
    CHECK (
        confirmed_amount IS NULL
        OR (
            currency IS NOT NULL
            AND confirmed_by IS NOT NULL
            AND confirmed_at IS NOT NULL
        )
    ),
    CHECK (
        extracted_amount IS NULL
        OR confirmed_amount IS NULL
        OR extracted_amount = confirmed_amount
        OR length(trim(ifnull(modification_reason, ''))) > 0
    )
);

CREATE INDEX ix_case_payment_requirements_case_status
    ON case_payment_requirements(case_id, status, requirement_type);

CREATE TRIGGER case_payment_requirements_validate_insert
BEFORE INSERT ON case_payment_requirements
WHEN NOT EXISTS (
    SELECT 1
    FROM transfer_school_applications
    WHERE id = NEW.transfer_application_id
      AND case_id = NEW.case_id
)
OR (
    NEW.source_offer_id IS NOT NULL
    AND NOT EXISTS (
        SELECT 1
        FROM offers
        JOIN student_cases ON student_cases.id = NEW.case_id
        WHERE offers.id = NEW.source_offer_id
          AND offers.student_id = student_cases.student_id
    )
)
BEGIN
    SELECT RAISE(ABORT, 'payment requirement references another case or student');
END;

CREATE TRIGGER case_payment_requirements_validate_update
BEFORE UPDATE OF case_id, transfer_application_id, source_offer_id
    ON case_payment_requirements
WHEN NOT EXISTS (
    SELECT 1
    FROM transfer_school_applications
    WHERE id = NEW.transfer_application_id
      AND case_id = NEW.case_id
)
OR (
    NEW.source_offer_id IS NOT NULL
    AND NOT EXISTS (
        SELECT 1
        FROM offers
        JOIN student_cases ON student_cases.id = NEW.case_id
        WHERE offers.id = NEW.source_offer_id
          AND offers.student_id = student_cases.student_id
    )
)
BEGIN
    SELECT RAISE(ABORT, 'payment requirement references another case or student');
END;

CREATE TRIGGER case_payment_requirements_no_delete
BEFORE DELETE ON case_payment_requirements
BEGIN
    SELECT RAISE(ABORT, 'case payment requirements are retained permanently');
END;

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
    UNIQUE(case_id, arrangement_type),
    CHECK (
        arrangement_status <> 'confirmed'
        OR (confirmed_by IS NOT NULL AND confirmed_at IS NOT NULL)
    ),
    CHECK (
        arrangement_status NOT IN ('carried_forward', 'updated')
        OR linked_record_id IS NOT NULL
        OR document_id IS NOT NULL
    )
);

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
    SELECT RAISE(ABORT, 'arrangement evidence belongs to another case or student');
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
    SELECT RAISE(ABORT, 'arrangement evidence belongs to another case or student');
END;

CREATE TRIGGER case_arrangement_confirmations_no_delete
BEFORE DELETE ON case_arrangement_confirmations
BEGIN
    SELECT RAISE(ABORT, 'case arrangement confirmations are retained permanently');
END;

CREATE TABLE case_completion_confirmations (
    id INTEGER PRIMARY KEY,
    case_id INTEGER NOT NULL
        REFERENCES transfer_cases(case_id) ON DELETE RESTRICT,
    confirmation_type TEXT NOT NULL
        CHECK (confirmation_type IN ('complete', 'undo_complete')),
    reason TEXT,
    actor_user_id INTEGER NOT NULL
        REFERENCES users(id) ON DELETE RESTRICT,
    confirmed_at TEXT NOT NULL,
    previous_status TEXT NOT NULL
        CHECK (
            previous_status IN (
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
    new_status TEXT NOT NULL
        CHECK (
            new_status IN (
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
    metadata_json TEXT NOT NULL DEFAULT '{}'
        CHECK (json_valid(metadata_json)),
    created_at TEXT NOT NULL,
    CHECK (
        confirmation_type <> 'undo_complete'
        OR length(trim(ifnull(reason, ''))) > 0
    )
);

CREATE INDEX ix_case_completion_confirmations_case
    ON case_completion_confirmations(case_id, confirmed_at, id);

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

CREATE TABLE student_source_tags (
    id INTEGER PRIMARY KEY,
    student_id TEXT NOT NULL
        REFERENCES students(student_id) ON DELETE RESTRICT,
    source_tag TEXT NOT NULL
        CHECK (source_tag IN ('external_transfer')),
    effective_from TEXT NOT NULL,
    effective_to TEXT,
    source_case_id INTEGER
        REFERENCES student_cases(id) ON DELETE RESTRICT,
    created_by INTEGER NOT NULL
        REFERENCES users(id) ON DELETE RESTRICT,
    created_at TEXT NOT NULL,
    CHECK (effective_to IS NULL OR effective_to >= effective_from)
);

CREATE UNIQUE INDEX uq_student_source_tags_current
    ON student_source_tags(student_id, source_tag)
    WHERE effective_to IS NULL;

CREATE INDEX ix_student_source_tags_history
    ON student_source_tags(student_id, source_tag, effective_from, id);

CREATE TRIGGER student_source_tags_validate_case_insert
BEFORE INSERT ON student_source_tags
WHEN NEW.source_case_id IS NOT NULL
 AND NOT EXISTS (
    SELECT 1
    FROM student_cases
    WHERE id = NEW.source_case_id
      AND student_id = NEW.student_id
)
BEGIN
    SELECT RAISE(ABORT, 'source tag case belongs to another student');
END;

CREATE TRIGGER student_source_tags_history_update
BEFORE UPDATE ON student_source_tags
WHEN NEW.id IS NOT OLD.id
  OR NEW.student_id IS NOT OLD.student_id
  OR NEW.source_tag IS NOT OLD.source_tag
  OR NEW.effective_from IS NOT OLD.effective_from
  OR NEW.source_case_id IS NOT OLD.source_case_id
  OR NEW.created_by IS NOT OLD.created_by
  OR NEW.created_at IS NOT OLD.created_at
  OR OLD.effective_to IS NOT NULL
  OR NEW.effective_to IS NULL
BEGIN
    SELECT RAISE(ABORT, 'source tag update may only end one current tag');
END;

CREATE TRIGGER student_source_tags_no_delete
BEFORE DELETE ON student_source_tags
BEGIN
    SELECT RAISE(ABORT, 'student source tags are retained permanently');
END;

CREATE TABLE external_transfer_case_requests (
    id INTEGER PRIMARY KEY,
    student_id TEXT NOT NULL
        REFERENCES students(student_id) ON DELETE RESTRICT,
    contract_id INTEGER
        REFERENCES contracts(id) ON DELETE RESTRICT,
    idempotency_key TEXT NOT NULL
        CHECK (length(trim(idempotency_key)) > 0),
    request_status TEXT NOT NULL DEFAULT 'pending'
        CHECK (
            request_status IN (
                'pending',
                'processing',
                'completed',
                'failed',
                'cancelled'
            )
        ),
    case_id INTEGER
        REFERENCES student_cases(id) ON DELETE RESTRICT,
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    last_error_code TEXT,
    last_error_message TEXT,
    next_retry_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT,
    CHECK (
        request_status <> 'completed'
        OR (case_id IS NOT NULL AND completed_at IS NOT NULL)
    )
);

CREATE UNIQUE INDEX uq_external_transfer_requests_idempotency
    ON external_transfer_case_requests(idempotency_key);

CREATE UNIQUE INDEX uq_external_transfer_requests_contract_student
    ON external_transfer_case_requests(student_id, contract_id)
    WHERE contract_id IS NOT NULL;

CREATE INDEX ix_external_transfer_requests_status_retry
    ON external_transfer_case_requests(
        request_status,
        next_retry_at,
        id
    );

CREATE TRIGGER external_transfer_case_requests_validate_insert
BEFORE INSERT ON external_transfer_case_requests
WHEN (
    NEW.contract_id IS NOT NULL
    AND NOT EXISTS (
        SELECT 1
        FROM contracts
        WHERE id = NEW.contract_id
          AND student_id = NEW.student_id
    )
)
OR (
    NEW.case_id IS NOT NULL
    AND NOT EXISTS (
        SELECT 1
        FROM student_cases
        WHERE id = NEW.case_id
          AND student_id = NEW.student_id
          AND case_type = 'transfer'
    )
)
BEGIN
    SELECT RAISE(ABORT, 'external transfer request references another student');
END;

CREATE TRIGGER external_transfer_case_requests_validate_update
BEFORE UPDATE OF student_id, contract_id, case_id
    ON external_transfer_case_requests
WHEN (
    NEW.contract_id IS NOT NULL
    AND NOT EXISTS (
        SELECT 1
        FROM contracts
        WHERE id = NEW.contract_id
          AND student_id = NEW.student_id
    )
)
OR (
    NEW.case_id IS NOT NULL
    AND NOT EXISTS (
        SELECT 1
        FROM student_cases
        WHERE id = NEW.case_id
          AND student_id = NEW.student_id
          AND case_type = 'transfer'
    )
)
BEGIN
    SELECT RAISE(ABORT, 'external transfer request references another student');
END;

CREATE TRIGGER external_transfer_case_requests_no_delete
BEFORE DELETE ON external_transfer_case_requests
BEGIN
    SELECT RAISE(ABORT, 'external transfer requests are retained permanently');
END;
