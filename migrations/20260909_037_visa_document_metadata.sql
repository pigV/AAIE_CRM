CREATE TABLE visa_document_keyword_rules (
 id INTEGER PRIMARY KEY, keyword TEXT NOT NULL, normalized_keyword TEXT NOT NULL UNIQUE CHECK(length(normalized_keyword)>0),
 category TEXT NOT NULL CHECK(category IN ('other','deposit','salary','financial_asset','grant_letter','refusal_letter','s56_request','s56_response','lodgement_receipt','immi_acknowledgment','bridging_visa','health_examination','hap_id','withdrawal_confirmation')),
 priority INTEGER NOT NULL DEFAULT 100 CHECK(priority BETWEEN 0 AND 10000), active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
 created_by INTEGER REFERENCES users(id), created_at TEXT NOT NULL, updated_by INTEGER REFERENCES users(id), updated_at TEXT NOT NULL
);
INSERT INTO visa_document_keyword_rules(id,keyword,normalized_keyword,category,priority,active,created_at,updated_at) VALUES
 (1,'Bank Account Transaction Details_Deposit','bank account transaction details deposit','deposit',100,1,'2026-09-08','2026-09-08'),
 (2,'Bank Account Transaction Details_Salary','bank account transaction details salary','salary',100,1,'2026-09-08','2026-09-08'),
 (3,'Certificate of Assets_Fund','certificate of assets fund','financial_asset',100,1,'2026-09-08','2026-09-08'),
 (4,'Health Exam Referraletter','health exam referraletter','health_examination',100,1,'2026-09-08','2026-09-08'),
 (5,'IMMI Grant Notification','immi grant notification','grant_letter',100,1,'2026-09-08','2026-09-08');
CREATE TABLE visa_document_metadata (
 document_id INTEGER PRIMARY KEY REFERENCES visa_case_documents(id) ON DELETE RESTRICT,
 display_name TEXT, auto_category TEXT CHECK(auto_category IS NULL OR auto_category IN ('other','deposit','salary','financial_asset','grant_letter','refusal_letter','s56_request','s56_response','lodgement_receipt','immi_acknowledgment','bridging_visa','health_examination','hap_id','withdrawal_confirmation')),
 manual_category TEXT CHECK(manual_category IS NULL OR manual_category IN ('other','deposit','salary','financial_asset','grant_letter','refusal_letter','s56_request','s56_response','lodgement_receipt','immi_acknowledgment','bridging_visa','health_examination','hap_id','withdrawal_confirmation')),
 classification_evidence_json TEXT NOT NULL DEFAULT '{}',
 updated_by INTEGER NOT NULL REFERENCES users(id), updated_at TEXT NOT NULL
);
