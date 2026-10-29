-- Run only after a verified database backup and after removing any candidate-only data.
DROP TRIGGER IF EXISTS higher_education_plan_cycle_insert;
DROP TRIGGER IF EXISTS higher_education_offer_segment_scope_insert;
DROP TRIGGER IF EXISTS higher_education_segment_scope_coe_insert;
DROP TRIGGER IF EXISTS higher_education_temporary_share_commission_insert;
DROP TRIGGER IF EXISTS higher_education_segment_scope_commission_insert;
DROP TRIGGER IF EXISTS higher_education_segment_scope_share_insert;
DROP TRIGGER IF EXISTS higher_education_package_segment_application_insert;
DROP TRIGGER IF EXISTS higher_education_provider_relationship_types_update;
DROP TRIGGER IF EXISTS higher_education_provider_relationship_types_insert;
DROP TABLE IF EXISTS higher_education_audit_log;
DROP TABLE IF EXISTS higher_education_coe_links;
DROP TABLE IF EXISTS higher_education_commission_lines;
DROP TABLE IF EXISTS higher_education_temporary_commission_shares;
DROP TABLE IF EXISTS higher_education_offers;
DROP TABLE IF EXISTS higher_education_package_segments;
DROP TABLE IF EXISTS higher_education_applications;
DROP TABLE IF EXISTS higher_education_plan_items;
DROP TABLE IF EXISTS higher_education_plans;
DROP TABLE IF EXISTS higher_education_provider_course_names;
DROP TABLE IF EXISTS higher_education_course_names;
DROP TABLE IF EXISTS higher_education_provider_relationships;
DROP TABLE IF EXISTS higher_education_providers;
DROP INDEX IF EXISTS idx_application_cycles_student_stream;
DROP INDEX IF EXISTS uq_application_cycles_active_student_stream;
CREATE UNIQUE INDEX uq_application_cycles_active_student
  ON application_cycles(student_id)
  WHERE status='active' AND student_id IS NOT NULL;
ALTER TABLE application_cycles DROP COLUMN education_stream;
ALTER TABLE students DROP COLUMN higher_education_course_type;
ALTER TABLE students DROP COLUMN application_stream;
