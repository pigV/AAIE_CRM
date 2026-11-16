CREATE TABLE visa_only_material_uploads (
    id INTEGER PRIMARY KEY,
    visa_case_id INTEGER NOT NULL REFERENCES visa_cases(id),
    original_name TEXT NOT NULL,
    file_path TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    item_id INTEGER REFERENCES visa_checklist_items(id),
    document_id INTEGER UNIQUE REFERENCES visa_case_documents(id),
    UNIQUE (visa_case_id, sha256),
    CHECK ((item_id IS NULL) = (document_id IS NULL))
);

CREATE INDEX visa_only_material_uploads_item ON visa_only_material_uploads(item_id);

CREATE TRIGGER visa_only_material_uploads_pending_insert
BEFORE INSERT ON visa_only_material_uploads
WHEN NEW.item_id IS NOT NULL OR NOT EXISTS (
    SELECT 1 FROM visa_cases
    WHERE id = NEW.visa_case_id AND student_id IS NULL
      AND applicant_id IS NOT NULL AND archived_at IS NULL
)
BEGIN
    SELECT RAISE(ABORT, 'Visa-only materials must start pending in an independent case');
END;

CREATE TRIGGER visa_only_material_uploads_confirm_owned
BEFORE UPDATE ON visa_only_material_uploads
WHEN OLD.document_id IS NOT NULL OR NEW.id IS NOT OLD.id
  OR NEW.visa_case_id IS NOT OLD.visa_case_id OR NEW.file_path IS NOT OLD.file_path
  OR NEW.sha256 IS NOT OLD.sha256 OR NEW.original_name IS NOT OLD.original_name
  OR NOT EXISTS (
      SELECT 1 FROM visa_checklist_items i
      JOIN visa_cases c ON c.id = i.visa_case_id
      JOIN visa_case_documents d ON d.visa_case_id = c.id
      WHERE i.id = NEW.item_id AND d.id = NEW.document_id
        AND c.id = NEW.visa_case_id AND c.student_id IS NULL
        AND c.applicant_id IS NOT NULL AND c.archived_at IS NULL
        AND d.student_document_id IS NULL AND d.file_path = NEW.file_path
        AND d.sha256 = NEW.sha256 AND d.archived_at IS NULL
  )
BEGIN
    SELECT RAISE(ABORT, 'Visa-only material confirmation ownership mismatch');
END;
