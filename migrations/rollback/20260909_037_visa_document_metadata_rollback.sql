-- Isolated empty-state rollback only. Preserve any postmigration business/config edits.
CREATE TEMP TABLE visa_metadata_rollback_guard(ok INTEGER CHECK(ok=1));
INSERT INTO visa_metadata_rollback_guard SELECT CASE WHEN
(SELECT count(*) FROM visa_document_metadata)=0 AND
(SELECT count(*) FROM visa_document_keyword_rules)=5 AND
NOT EXISTS(SELECT id,keyword,normalized_keyword,category FROM visa_document_keyword_rules EXCEPT SELECT column1,column2,column3,column4 FROM (VALUES
 (1,'Bank Account Transaction Details_Deposit','bank account transaction details deposit','deposit'),
 (2,'Bank Account Transaction Details_Salary','bank account transaction details salary','salary'),
 (3,'Certificate of Assets_Fund','certificate of assets fund','financial_asset'),
 (4,'Health Exam Referraletter','health exam referraletter','health_examination'),
 (5,'IMMI Grant Notification','immi grant notification','grant_letter'))) AND
NOT EXISTS(SELECT 1 FROM visa_document_keyword_rules WHERE created_by IS NOT NULL OR updated_by IS NOT NULL OR priority<>100 OR active<>1 OR created_at<>'2026-09-08' OR updated_at<>'2026-09-08') AND
NOT EXISTS(SELECT 1 FROM audit_logs WHERE module_name='visa_document_keyword_rules')
THEN 1 ELSE 0 END;
DROP TABLE visa_document_metadata;
DROP TABLE visa_document_keyword_rules;
DROP TABLE visa_metadata_rollback_guard;
