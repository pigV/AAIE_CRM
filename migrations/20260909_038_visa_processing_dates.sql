CREATE TABLE visa_processing_date_extractions(id INTEGER PRIMARY KEY,visa_case_id INTEGER NOT NULL REFERENCES visa_cases(id),document_id INTEGER NOT NULL REFERENCES visa_case_documents(id),proposed_json TEXT NOT NULL,actor_user_id INTEGER NOT NULL REFERENCES users(id),occurred_at TEXT NOT NULL);
CREATE TABLE visa_processing_date_revisions(id INTEGER PRIMARY KEY,visa_case_id INTEGER NOT NULL REFERENCES visa_cases(id),field TEXT NOT NULL CHECK(field IN ('acknowledge_sent_on','health_exam_date','grant_sent_on','medical_completed_date')),value TEXT NOT NULL,document_id INTEGER REFERENCES visa_case_documents(id),before_json TEXT NOT NULL,actor_user_id INTEGER NOT NULL REFERENCES users(id),occurred_at TEXT NOT NULL);
CREATE INDEX idx_processing_extractions_case ON visa_processing_date_extractions(visa_case_id,document_id,id);
CREATE INDEX idx_processing_revisions_case ON visa_processing_date_revisions(visa_case_id,field,id);
CREATE TRIGGER processing_extractions_no_update BEFORE UPDATE ON visa_processing_date_extractions BEGIN SELECT RAISE(ABORT,'Processing evidence immutable'); END;
CREATE TRIGGER processing_extractions_no_delete BEFORE DELETE ON visa_processing_date_extractions BEGIN SELECT RAISE(ABORT,'Processing evidence immutable'); END;
CREATE TRIGGER processing_revisions_no_update BEFORE UPDATE ON visa_processing_date_revisions BEGIN SELECT RAISE(ABORT,'Processing audit immutable'); END;
CREATE TRIGGER processing_revisions_no_delete BEFORE DELETE ON visa_processing_date_revisions BEGIN SELECT RAISE(ABORT,'Processing audit immutable'); END;
