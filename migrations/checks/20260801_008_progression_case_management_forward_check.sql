SELECT count(*) = 5 FROM sqlite_master
WHERE type='table' AND name IN (
  'progression_cases','progression_case_stages','progression_case_materials',
  'progression_case_offers','progression_case_events'
);

SELECT count(*) = 18 FROM sqlite_master
WHERE type='table' AND name IN (
  'student_case_sequences','student_cases','student_case_assignments',
  'case_events','student_school_enrolments','transfer_cases',
  'transfer_school_applications','case_record_links','case_material_items',
  'case_payment_requirements','case_arrangement_confirmations',
  'case_completion_confirmations','student_source_tags',
  'external_transfer_case_requests','case_payment_requirement_proofs',
  'transfer_guardianship_arrangements','transfer_accommodation_arrangements',
  'case_arrangement_evidence_links'
);

SELECT count(*) = 2 FROM sqlite_master
WHERE type='trigger' AND name IN (
  'case_completion_confirmations_no_update',
  'case_completion_confirmations_no_delete'
);

SELECT count(*) = 7 FROM sqlite_master
WHERE type='index' AND name IN (
  'ix_progression_cases_stage_priority','ix_progression_cases_target_start',
  'ix_progression_case_stages_case_status',
  'ix_progression_case_materials_case_status',
  'ix_progression_case_offers_case_status',
  'uq_progression_case_offers_one_accepted',
  'ix_progression_case_events_case_created'
);

SELECT count(*) = 12 FROM sqlite_master
WHERE type='trigger' AND name IN (
  'progression_cases_parent_type_insert','progression_cases_parent_type_update',
  'progression_cases_no_delete','progression_case_stages_no_delete',
  'progression_case_materials_document_student_insert',
  'progression_case_materials_document_student_update',
  'progression_case_materials_no_delete','progression_case_offers_scope_insert',
  'progression_case_offers_scope_update',
  'progression_case_offers_no_delete','progression_case_events_no_update',
  'progression_case_events_no_delete'
);

SELECT count(*) = 23 FROM pragma_table_info('progression_cases');
SELECT count(*) = 16 FROM pragma_table_info('progression_case_stages');
SELECT count(*) = 13 FROM pragma_table_info('progression_case_materials');
SELECT count(*) = 19 FROM pragma_table_info('progression_case_offers');
SELECT count(*) = 12 FROM pragma_table_info('progression_case_events');
