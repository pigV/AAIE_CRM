SELECT CASE WHEN EXISTS(SELECT 1 FROM sqlite_master WHERE type='table' AND name='visa_applicants') AND
EXISTS(SELECT 1 FROM sqlite_master WHERE type='table' AND name='visa_applicant_student_links') AND
EXISTS(SELECT 1 FROM sqlite_master WHERE type='table' AND name='visa_form_submission_originals') AND
EXISTS(SELECT 1 FROM sqlite_master WHERE type='table' AND name='visa_form_change_audit') AND
EXISTS(SELECT 1 FROM sqlite_master WHERE type='table' AND name='visa_case_documents') AND
EXISTS(SELECT 1 FROM sqlite_master WHERE type='table' AND name='visa_immi_submissions') AND
EXISTS(SELECT 1 FROM sqlite_master WHERE type='table' AND name='visa_letter_extractions') AND
EXISTS(SELECT 1 FROM sqlite_master WHERE type='table' AND name='visa_letter_reviews') AND
EXISTS(SELECT 1 FROM sqlite_master WHERE type='table' AND name='visa_case_confirmed_decisions') AND
EXISTS(SELECT 1 FROM sqlite_master WHERE type='table' AND name='visa_document_audit') AND
EXISTS(SELECT 1 FROM pragma_table_info('visa_cases') WHERE name='student_id' AND "notnull"=0) AND
EXISTS(SELECT 1 FROM pragma_table_info('visa_cases') WHERE name='applicant_id') AND
EXISTS(SELECT 1 FROM sqlite_master WHERE name='visa_immi_pinned_037') AND
EXISTS(SELECT 1 FROM pragma_table_info('visa_evidence_profiles') WHERE name='property_owner') AND
EXISTS(SELECT 1 FROM pragma_table_info('visa_evidence_profiles') WHERE name='company_owner') AND
EXISTS(SELECT 1 FROM pragma_table_info('visa_evidence_profiles') WHERE name='investment_assets_owner') AND
EXISTS(SELECT 1 FROM pragma_table_info('visa_evidence_profiles') WHERE name='home_property_owner') AND
EXISTS(SELECT 1 FROM pragma_table_info('visa_evidence_profiles') WHERE name='home_company_owner') AND
EXISTS(SELECT 1 FROM pragma_table_info('visa_evidence_profiles') WHERE name='home_job_owner') AND
EXISTS(SELECT 1 FROM pragma_table_info('visa_evidence_profiles') WHERE name='funding_sources_json') AND
EXISTS(SELECT 1 FROM sqlite_master WHERE type='trigger' AND name='visa_applicant_student_links_immutable_update_037') AND
EXISTS(SELECT 1 FROM sqlite_master WHERE type='trigger' AND name='visa_applicant_student_links_immutable_delete_037') AND
EXISTS(SELECT 1 FROM sqlite_master WHERE type='trigger' AND name='visa_form_submission_originals_immutable_update_037') AND
EXISTS(SELECT 1 FROM sqlite_master WHERE type='trigger' AND name='visa_form_submission_originals_immutable_delete_037') AND
EXISTS(SELECT 1 FROM sqlite_master WHERE type='trigger' AND name='visa_form_change_audit_immutable_update_037') AND
EXISTS(SELECT 1 FROM sqlite_master WHERE type='trigger' AND name='visa_form_change_audit_immutable_delete_037') AND
EXISTS(SELECT 1 FROM sqlite_master WHERE type='trigger' AND name='visa_letter_extractions_immutable_update_037') AND
EXISTS(SELECT 1 FROM sqlite_master WHERE type='trigger' AND name='visa_letter_extractions_immutable_delete_037') AND
EXISTS(SELECT 1 FROM sqlite_master WHERE type='trigger' AND name='visa_letter_reviews_immutable_update_037') AND
EXISTS(SELECT 1 FROM sqlite_master WHERE type='trigger' AND name='visa_letter_reviews_immutable_delete_037') AND
EXISTS(SELECT 1 FROM sqlite_master WHERE type='trigger' AND name='visa_case_confirmed_decisions_immutable_update_037') AND
EXISTS(SELECT 1 FROM sqlite_master WHERE type='trigger' AND name='visa_case_confirmed_decisions_immutable_delete_037') AND
EXISTS(SELECT 1 FROM sqlite_master WHERE type='trigger' AND name='visa_document_audit_immutable_update_037') AND
EXISTS(SELECT 1 FROM sqlite_master WHERE type='trigger' AND name='visa_document_audit_immutable_delete_037') AND
EXISTS(SELECT 1 FROM sqlite_master WHERE type='trigger' AND name='visa_immi_no_delete_037') AND
EXISTS(SELECT 1 FROM sqlite_master WHERE type='trigger' AND name='visa_decision_events_ownership_insert') THEN 1 ELSE 0 END AS visa_management_authorities_ready;
