-- migration_mode: sqlite_fk_off_rebuild
-- Visa-only release from verified Production 20260904_035.
-- 20260907_035 precedes unshipped Revenue 20260907_036; _037 object suffix retains provenance.
-- Reject unknown legacy columns instead of silently discarding data.
CREATE TEMP TABLE visa_037_guard(ok INTEGER CHECK(ok=1));
INSERT INTO visa_037_guard SELECT CASE WHEN sql='CREATE TABLE visa_cases (
  id INTEGER PRIMARY KEY, student_id TEXT NOT NULL REFERENCES students(student_id),
  application_id TEXT, visa_case_name TEXT NOT NULL, visa_type TEXT, applicant_name TEXT,
  relationship_to_student TEXT, application_type TEXT, visa_status TEXT NOT NULL DEFAULT ''准备中'',
  submitted_date TEXT, grant_date TEXT, refusal_date TEXT, visa_start_date TEXT, visa_expiry_date TEXT,
  trn TEXT, hap_id TEXT, grant_number TEXT, visa_conditions TEXT,
  coe_expiry_date TEXT, oshc_expiry_date TEXT, caaw_expiry_date TEXT, s56_deadline TEXT,
  responsible_consultant_id INTEGER REFERENCES users(id), responsible_consultant_name TEXT,
  school_name TEXT, state TEXT, internal_notes TEXT, risk_level TEXT,
  final_result TEXT NOT NULL DEFAULT ''Pending'', refusal_reason_category TEXT,
  is_included_in_analytics INTEGER NOT NULL DEFAULT 1, file_completion INTEGER NOT NULL DEFAULT 0,
  legacy_visa_id INTEGER REFERENCES visas(id), created_by INTEGER REFERENCES users(id),
  created_at TEXT NOT NULL, updated_by INTEGER REFERENCES users(id), updated_at TEXT NOT NULL
, case_type TEXT DEFAULT ''Student Visa'', status TEXT DEFAULT ''Draft'', visa_subclass TEXT, lodgement_date TEXT, expiry_date TEXT, s56_due_date TEXT, decision_status TEXT DEFAULT ''Pending'', notes TEXT, archived_at TEXT, responsible_consultant TEXT, main_applicant TEXT, guardian_applicant TEXT, applicant_count INTEGER DEFAULT 1, case_owner TEXT, target_lodgement_date TEXT, actual_lodgement_date TEXT, result_date TEXT, result_status TEXT DEFAULT ''Pending'', lodged_by_user_id INTEGER REFERENCES users(id), lodged_by_name TEXT, lodgement_recorded_at TEXT, application_cycle_id INTEGER REFERENCES application_cycles(id) ON DELETE RESTRICT)' THEN 1 ELSE 0 END FROM sqlite_master WHERE type='table' AND name='visa_cases';
INSERT INTO visa_037_guard SELECT CASE WHEN count(*)=59 THEN 1 ELSE 0 END FROM pragma_table_info('visa_cases');
INSERT INTO visa_037_guard SELECT CASE WHEN count(*)=6 THEN 1 ELSE 0 END FROM sqlite_master WHERE type='index' AND tbl_name='visa_cases';
INSERT INTO visa_037_guard SELECT CASE WHEN EXISTS(SELECT 1 FROM sqlite_master WHERE type='index' AND name='idx_visa_cases_application' AND sql='CREATE INDEX idx_visa_cases_application ON visa_cases(application_id)') THEN 1 ELSE 0 END;
INSERT INTO visa_037_guard SELECT CASE WHEN EXISTS(SELECT 1 FROM sqlite_master WHERE type='index' AND name='idx_visa_cases_cycle' AND sql='CREATE INDEX idx_visa_cases_cycle
ON visa_cases(application_cycle_id)') THEN 1 ELSE 0 END;
INSERT INTO visa_037_guard SELECT CASE WHEN EXISTS(SELECT 1 FROM sqlite_master WHERE type='index' AND name='idx_visa_cases_expiry' AND sql='CREATE INDEX idx_visa_cases_expiry ON visa_cases(visa_expiry_date, coe_expiry_date, oshc_expiry_date, caaw_expiry_date, s56_deadline)') THEN 1 ELSE 0 END;
INSERT INTO visa_037_guard SELECT CASE WHEN EXISTS(SELECT 1 FROM sqlite_master WHERE type='index' AND name='idx_visa_cases_legacy' AND sql='CREATE INDEX idx_visa_cases_legacy ON visa_cases(legacy_visa_id)') THEN 1 ELSE 0 END;
INSERT INTO visa_037_guard SELECT CASE WHEN EXISTS(SELECT 1 FROM sqlite_master WHERE type='index' AND name='idx_visa_cases_status' AND sql='CREATE INDEX idx_visa_cases_status ON visa_cases(visa_status, final_result, risk_level)') THEN 1 ELSE 0 END;
INSERT INTO visa_037_guard SELECT CASE WHEN EXISTS(SELECT 1 FROM sqlite_master WHERE type='index' AND name='idx_visa_cases_student' AND sql='CREATE INDEX idx_visa_cases_student ON visa_cases(student_id, application_id, updated_at DESC)') THEN 1 ELSE 0 END;
INSERT INTO visa_037_guard SELECT CASE WHEN count(*)=0 THEN 1 ELSE 0 END FROM sqlite_master WHERE type='trigger' AND tbl_name='visa_cases';
CREATE TABLE visa_applicants(id INTEGER PRIMARY KEY, name TEXT NOT NULL CHECK(length(trim(name))>0), email TEXT NOT NULL DEFAULT '', created_by INTEGER NOT NULL REFERENCES users(id), created_at TEXT NOT NULL);
CREATE TABLE visa_applicant_student_links(id INTEGER PRIMARY KEY, applicant_id INTEGER NOT NULL UNIQUE REFERENCES visa_applicants(id), student_id TEXT NOT NULL REFERENCES students(student_id), linked_by INTEGER NOT NULL REFERENCES users(id), linked_at TEXT NOT NULL);
CREATE TABLE visa_cases_rebuild_037 (
  id INTEGER PRIMARY KEY, student_id TEXT REFERENCES students(student_id),
  application_id TEXT, visa_case_name TEXT NOT NULL, visa_type TEXT, applicant_name TEXT,
  relationship_to_student TEXT, application_type TEXT, visa_status TEXT NOT NULL DEFAULT '准备中',
  submitted_date TEXT, grant_date TEXT, refusal_date TEXT, visa_start_date TEXT, visa_expiry_date TEXT,
  trn TEXT, hap_id TEXT, grant_number TEXT, visa_conditions TEXT,
  coe_expiry_date TEXT, oshc_expiry_date TEXT, caaw_expiry_date TEXT, s56_deadline TEXT,
  responsible_consultant_id INTEGER REFERENCES users(id), responsible_consultant_name TEXT,
  school_name TEXT, state TEXT, internal_notes TEXT, risk_level TEXT,
  final_result TEXT NOT NULL DEFAULT 'Pending', refusal_reason_category TEXT,
  is_included_in_analytics INTEGER NOT NULL DEFAULT 1, file_completion INTEGER NOT NULL DEFAULT 0,
  legacy_visa_id INTEGER REFERENCES visas(id), created_by INTEGER REFERENCES users(id),
  created_at TEXT NOT NULL, updated_by INTEGER REFERENCES users(id), updated_at TEXT NOT NULL
, case_type TEXT DEFAULT 'Student Visa', status TEXT DEFAULT 'Draft', visa_subclass TEXT, lodgement_date TEXT, expiry_date TEXT, s56_due_date TEXT, decision_status TEXT DEFAULT 'Pending', notes TEXT, archived_at TEXT, responsible_consultant TEXT, main_applicant TEXT, guardian_applicant TEXT, applicant_count INTEGER DEFAULT 1, case_owner TEXT, target_lodgement_date TEXT, actual_lodgement_date TEXT, result_date TEXT, result_status TEXT DEFAULT 'Pending', lodged_by_user_id INTEGER REFERENCES users(id), lodged_by_name TEXT, lodgement_recorded_at TEXT, application_cycle_id INTEGER REFERENCES application_cycles(id) ON DELETE RESTRICT, applicant_id INTEGER REFERENCES visa_applicants(id), CHECK(student_id IS NOT NULL OR applicant_id IS NOT NULL));
INSERT INTO visa_cases_rebuild_037(id,student_id,application_id,visa_case_name,visa_type,applicant_name,relationship_to_student,application_type,visa_status,submitted_date,grant_date,refusal_date,visa_start_date,visa_expiry_date,trn,hap_id,grant_number,visa_conditions,coe_expiry_date,oshc_expiry_date,caaw_expiry_date,s56_deadline,responsible_consultant_id,responsible_consultant_name,school_name,state,internal_notes,risk_level,final_result,refusal_reason_category,is_included_in_analytics,file_completion,legacy_visa_id,created_by,created_at,updated_by,updated_at,case_type,status,visa_subclass,lodgement_date,expiry_date,s56_due_date,decision_status,notes,archived_at,responsible_consultant,main_applicant,guardian_applicant,applicant_count,case_owner,target_lodgement_date,actual_lodgement_date,result_date,result_status,lodged_by_user_id,lodged_by_name,lodgement_recorded_at,application_cycle_id) SELECT id,student_id,application_id,visa_case_name,visa_type,applicant_name,relationship_to_student,application_type,visa_status,submitted_date,grant_date,refusal_date,visa_start_date,visa_expiry_date,trn,hap_id,grant_number,visa_conditions,coe_expiry_date,oshc_expiry_date,caaw_expiry_date,s56_deadline,responsible_consultant_id,responsible_consultant_name,school_name,state,internal_notes,risk_level,final_result,refusal_reason_category,is_included_in_analytics,file_completion,legacy_visa_id,created_by,created_at,updated_by,updated_at,case_type,status,visa_subclass,lodgement_date,expiry_date,s56_due_date,decision_status,notes,archived_at,responsible_consultant,main_applicant,guardian_applicant,applicant_count,case_owner,target_lodgement_date,actual_lodgement_date,result_date,result_status,lodged_by_user_id,lodged_by_name,lodgement_recorded_at,application_cycle_id FROM visa_cases;
INSERT INTO visa_037_guard SELECT CASE WHEN (SELECT count(*) FROM visa_cases)=(SELECT count(*) FROM visa_cases_rebuild_037) THEN 1 ELSE 0 END;
DROP TRIGGER visa_decision_events_ownership_insert;
DROP TABLE visa_cases;
ALTER TABLE visa_cases_rebuild_037 RENAME TO visa_cases;
CREATE TRIGGER visa_decision_events_ownership_insert
BEFORE INSERT ON visa_decision_events
WHEN NOT EXISTS (
  SELECT 1 FROM application_cycles
  WHERE id=NEW.application_cycle_id AND student_id=NEW.student_id
)
OR NOT EXISTS (
  SELECT 1 FROM visa_cases
  WHERE id=NEW.visa_case_id
    AND student_id=NEW.student_id
    AND application_cycle_id=NEW.application_cycle_id
)
OR (
  NEW.student_document_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM student_documents
    WHERE id=NEW.student_document_id
      AND student_id=NEW.student_id
      AND is_active=1
      AND (
        (NEW.decision_type='grant' AND document_type='VisaGrantLetter')
        OR
        (NEW.decision_type='refusal' AND document_type='VisaRefusalLetter')
      )
  )
)
OR (
  NEW.visa_record_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM visas
    WHERE id=NEW.visa_record_id
      AND student_id=NEW.student_id
      AND (
        (NEW.decision_type='grant' AND lower(trim(visa_status)) IN ('granted','active'))
        OR
        (NEW.decision_type='refusal' AND lower(trim(visa_status))='refused')
      )
  )
)
BEGIN
  SELECT RAISE(ABORT, 'visa decision ownership or evidence mismatch');
END;
CREATE INDEX idx_visa_cases_application ON visa_cases(application_id);
CREATE INDEX idx_visa_cases_cycle
ON visa_cases(application_cycle_id);
CREATE INDEX idx_visa_cases_expiry ON visa_cases(visa_expiry_date, coe_expiry_date, oshc_expiry_date, caaw_expiry_date, s56_deadline);
CREATE INDEX idx_visa_cases_legacy ON visa_cases(legacy_visa_id);
CREATE INDEX idx_visa_cases_status ON visa_cases(visa_status, final_result, risk_level);
CREATE INDEX idx_visa_cases_student ON visa_cases(student_id, application_id, updated_at DESC);
CREATE INDEX idx_visa_cases_applicant ON visa_cases(applicant_id);
CREATE TABLE visa_form_submission_originals(assignment_id INTEGER PRIMARY KEY REFERENCES visa_form_assignments(id), respondent_id INTEGER NOT NULL REFERENCES visa_information_collection_respondents(id), visa_case_id INTEGER NOT NULL REFERENCES visa_cases(id), original_json TEXT NOT NULL, captured_at TEXT NOT NULL, source TEXT NOT NULL);
CREATE TABLE visa_form_change_audit(id INTEGER PRIMARY KEY, assignment_id INTEGER NOT NULL REFERENCES visa_form_assignments(id), visa_case_id INTEGER NOT NULL REFERENCES visa_cases(id), actor_id INTEGER REFERENCES users(id), action TEXT NOT NULL, before_json TEXT NOT NULL, after_json TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE visa_case_documents(id INTEGER PRIMARY KEY, visa_case_id INTEGER NOT NULL REFERENCES visa_cases(id), student_document_id INTEGER REFERENCES student_documents(id), file_path TEXT NOT NULL, original_name TEXT NOT NULL, version INTEGER NOT NULL, sha256 TEXT NOT NULL, created_by INTEGER NOT NULL REFERENCES users(id), created_at TEXT NOT NULL, archived_at TEXT, archived_by INTEGER REFERENCES users(id), UNIQUE(visa_case_id,student_document_id));
CREATE TABLE visa_immi_submissions(id INTEGER PRIMARY KEY, visa_case_id INTEGER NOT NULL REFERENCES visa_cases(id), document_id INTEGER NOT NULL REFERENCES visa_case_documents(id), file_path TEXT NOT NULL, version INTEGER NOT NULL, sha256 TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('submitted','superseded')), submitted_by INTEGER NOT NULL REFERENCES users(id), submitted_at TEXT NOT NULL, supersedes_id INTEGER REFERENCES visa_immi_submissions(id), UNIQUE(visa_case_id,document_id));
CREATE TABLE visa_letter_extractions(id INTEGER PRIMARY KEY,visa_case_id INTEGER NOT NULL REFERENCES visa_cases(id),document_id INTEGER NOT NULL REFERENCES visa_case_documents(id),proposed_json TEXT NOT NULL,extraction_method TEXT NOT NULL,created_by INTEGER NOT NULL REFERENCES users(id),created_at TEXT NOT NULL);
CREATE TABLE visa_letter_reviews(id INTEGER PRIMARY KEY,extraction_id INTEGER NOT NULL REFERENCES visa_letter_extractions(id),visa_case_id INTEGER NOT NULL REFERENCES visa_cases(id),confirmed_json TEXT NOT NULL,confirmed_by INTEGER NOT NULL REFERENCES users(id),confirmed_at TEXT NOT NULL);
CREATE TABLE visa_case_confirmed_decisions(id INTEGER PRIMARY KEY,visa_case_id INTEGER NOT NULL UNIQUE REFERENCES visa_cases(id),extraction_id INTEGER NOT NULL REFERENCES visa_letter_extractions(id),decision_type TEXT NOT NULL CHECK(decision_type IN ('grant','refusal')),decision_date TEXT NOT NULL,confirmed_json TEXT NOT NULL,confirmed_by INTEGER NOT NULL REFERENCES users(id),confirmed_at TEXT NOT NULL,canonical_event_id INTEGER REFERENCES visa_decision_events(id));
CREATE TABLE visa_document_audit(id INTEGER PRIMARY KEY,visa_case_id INTEGER NOT NULL REFERENCES visa_cases(id),document_id INTEGER REFERENCES visa_case_documents(id),action TEXT NOT NULL,before_json TEXT NOT NULL,after_json TEXT NOT NULL,actor_user_id INTEGER NOT NULL REFERENCES users(id),occurred_at TEXT NOT NULL);
ALTER TABLE visa_evidence_profiles ADD COLUMN property_owner TEXT NOT NULL DEFAULT '';
ALTER TABLE visa_evidence_profiles ADD COLUMN company_owner TEXT NOT NULL DEFAULT '';
ALTER TABLE visa_evidence_profiles ADD COLUMN investment_assets_owner TEXT NOT NULL DEFAULT '';
ALTER TABLE visa_evidence_profiles ADD COLUMN home_property_owner TEXT NOT NULL DEFAULT '';
ALTER TABLE visa_evidence_profiles ADD COLUMN home_company_owner TEXT NOT NULL DEFAULT '';
ALTER TABLE visa_evidence_profiles ADD COLUMN home_job_owner TEXT NOT NULL DEFAULT '';
ALTER TABLE visa_evidence_profiles ADD COLUMN funding_sources_json TEXT NOT NULL DEFAULT '[]';
CREATE TRIGGER visa_applicant_student_links_immutable_update_037 BEFORE UPDATE ON visa_applicant_student_links BEGIN SELECT RAISE(ABORT,'Visa audit/original is immutable'); END;
CREATE TRIGGER visa_applicant_student_links_immutable_delete_037 BEFORE DELETE ON visa_applicant_student_links BEGIN SELECT RAISE(ABORT,'Visa audit/original is immutable'); END;
CREATE TRIGGER visa_form_submission_originals_immutable_update_037 BEFORE UPDATE ON visa_form_submission_originals BEGIN SELECT RAISE(ABORT,'Visa audit/original is immutable'); END;
CREATE TRIGGER visa_form_submission_originals_immutable_delete_037 BEFORE DELETE ON visa_form_submission_originals BEGIN SELECT RAISE(ABORT,'Visa audit/original is immutable'); END;
CREATE TRIGGER visa_form_change_audit_immutable_update_037 BEFORE UPDATE ON visa_form_change_audit BEGIN SELECT RAISE(ABORT,'Visa audit/original is immutable'); END;
CREATE TRIGGER visa_form_change_audit_immutable_delete_037 BEFORE DELETE ON visa_form_change_audit BEGIN SELECT RAISE(ABORT,'Visa audit/original is immutable'); END;
CREATE TRIGGER visa_letter_extractions_immutable_update_037 BEFORE UPDATE ON visa_letter_extractions BEGIN SELECT RAISE(ABORT,'Visa audit/original is immutable'); END;
CREATE TRIGGER visa_letter_extractions_immutable_delete_037 BEFORE DELETE ON visa_letter_extractions BEGIN SELECT RAISE(ABORT,'Visa audit/original is immutable'); END;
CREATE TRIGGER visa_letter_reviews_immutable_update_037 BEFORE UPDATE ON visa_letter_reviews BEGIN SELECT RAISE(ABORT,'Visa audit/original is immutable'); END;
CREATE TRIGGER visa_letter_reviews_immutable_delete_037 BEFORE DELETE ON visa_letter_reviews BEGIN SELECT RAISE(ABORT,'Visa audit/original is immutable'); END;
CREATE TRIGGER visa_case_confirmed_decisions_immutable_update_037 BEFORE UPDATE ON visa_case_confirmed_decisions BEGIN SELECT RAISE(ABORT,'Visa audit/original is immutable'); END;
CREATE TRIGGER visa_case_confirmed_decisions_immutable_delete_037 BEFORE DELETE ON visa_case_confirmed_decisions BEGIN SELECT RAISE(ABORT,'Visa audit/original is immutable'); END;
CREATE TRIGGER visa_document_audit_immutable_update_037 BEFORE UPDATE ON visa_document_audit BEGIN SELECT RAISE(ABORT,'Visa audit/original is immutable'); END;
CREATE TRIGGER visa_document_audit_immutable_delete_037 BEFORE DELETE ON visa_document_audit BEGIN SELECT RAISE(ABORT,'Visa audit/original is immutable'); END;
CREATE TRIGGER visa_immi_pinned_037 BEFORE UPDATE OF visa_case_id,document_id,file_path,version,sha256,submitted_by,submitted_at,supersedes_id ON visa_immi_submissions BEGIN SELECT RAISE(ABORT,'Submitted version is immutable'); END;
CREATE TRIGGER visa_immi_no_delete_037 BEFORE DELETE ON visa_immi_submissions BEGIN SELECT RAISE(ABORT,'Submitted history cannot be deleted'); END;
DROP TABLE visa_037_guard;
