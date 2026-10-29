import json
import os
import re
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from staging_environment import ACTIVE as STAGING

from security import now_iso, password_hash
from student_case_feature_flags import StudentCaseFeatureFlags, read_student_case_feature_flags

DB_LOCK = threading.RLock()


def _db_perf_enabled():
    env = os.environ.get("AAIE_CRM_ENV", "development").strip().lower()
    flag = os.environ.get("AAIE_CRM_DB_PERF_LOG", "1").strip().lower()
    return env not in {"prod", "production"} and flag not in {"0", "false", "off", "no"}


def _db_slow_threshold_ms():
    try:
        return float(os.environ.get("AAIE_CRM_DB_SLOW_MS", "250"))
    except ValueError:
        return 250.0


def _db_perf_log(event, message="", **details):
    if not _db_perf_enabled():
        return
    try:
        log_dir = Path(__file__).resolve().parent / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        pieces = [now_iso(), event]
        if message:
            pieces.append(str(message))
        for key, value in details.items():
            if value is not None and value != "":
                pieces.append(f"{key}={value}")
        with (log_dir / "database.log").open("a", encoding="utf-8") as fh:
            fh.write(" | ".join(pieces) + "\n")
    except Exception:
        return


def _db_sql_summary(sql):
    return re.sub(r"\s+", " ", str(sql or "").strip())[:220]

SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS roles (
  id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL, permissions_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY, username TEXT UNIQUE NOT NULL, display_name TEXT NOT NULL,
  password_hash TEXT NOT NULL, role_id INTEGER NOT NULL REFERENCES roles(id),
  is_active INTEGER NOT NULL DEFAULT 1, must_change_password INTEGER NOT NULL DEFAULT 0,
  email TEXT, created_by INTEGER REFERENCES users(id),
  created_at TEXT NOT NULL, last_login_at TEXT, last_login_ip TEXT,
  deleted_at TEXT, deleted_by INTEGER REFERENCES users(id)
);
CREATE TABLE IF NOT EXISTS modules (
  id INTEGER PRIMARY KEY, module_key TEXT UNIQUE NOT NULL, module_name_en TEXT NOT NULL,
  module_name_zh TEXT NOT NULL, route_path TEXT NOT NULL, sidebar_order INTEGER NOT NULL,
  is_active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS user_module_permissions (
  id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  module_key TEXT NOT NULL, can_access INTEGER NOT NULL DEFAULT 0,
  can_view INTEGER NOT NULL DEFAULT 0, can_create INTEGER NOT NULL DEFAULT 0,
  can_edit INTEGER NOT NULL DEFAULT 0, can_delete INTEGER NOT NULL DEFAULT 0,
  can_open INTEGER NOT NULL DEFAULT 0,
  can_upload INTEGER NOT NULL DEFAULT 0, can_download INTEGER NOT NULL DEFAULT 0,
  can_restore INTEGER NOT NULL DEFAULT 0, can_apply_signature_stamp INTEGER NOT NULL DEFAULT 0,
  can_mark_delete INTEGER NOT NULL DEFAULT 0, can_mark_move INTEGER NOT NULL DEFAULT 0,
  can_mark_rename INTEGER NOT NULL DEFAULT 0, can_cancel_file_marker INTEGER NOT NULL DEFAULT 0,
  can_mark_file_marker_processed INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL, updated_by INTEGER,
  UNIQUE(user_id,module_key)
);
CREATE TABLE IF NOT EXISTS sessions (
  token TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id),
  csrf_token TEXT NOT NULL, expires_at TEXT NOT NULL, created_at TEXT NOT NULL,
  last_seen_at TEXT
);
CREATE TABLE IF NOT EXISTS student_sequences (
  year INTEGER PRIMARY KEY, last_value INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS students (
  student_id TEXT PRIMARY KEY, passport_last_name TEXT NOT NULL, passport_first_name TEXT NOT NULL,
  english_nickname TEXT, gender TEXT, date_of_birth TEXT, current_year_level TEXT,
  preferred_intake_year TEXT, preferred_intake_term TEXT, applying_year_level TEXT,
  preferred_cities_json TEXT, visa_status TEXT, previous_name TEXT, home_address TEXT,
  home_postcode TEXT, australia_address TEXT, australia_postcode TEXT, home_phone TEXT,
  mobile_phone TEXT, student_email TEXT, consultant_id INTEGER REFERENCES users(id),
  current_status TEXT NOT NULL DEFAULT 'New', folder_path TEXT, nas_status TEXT NOT NULL DEFAULT 'pending',
  general_notes TEXT, source_token TEXT, created_date TEXT NOT NULL, updated_at TEXT NOT NULL,
  updated_by INTEGER REFERENCES users(id), form_data_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS parents (
  id INTEGER PRIMARY KEY, student_id TEXT NOT NULL REFERENCES students(student_id),
  parent_type TEXT NOT NULL, passport_last_name TEXT, passport_first_name TEXT, date_of_birth TEXT,
  mobile TEXT, email TEXT, employer TEXT, work_address TEXT, work_postcode TEXT, work_phone TEXT,
  position TEXT, marital_status TEXT, religion TEXT, highest_school TEXT, highest_qualification TEXT,
  migration_application_status TEXT, migration_notes TEXT
);
CREATE TABLE IF NOT EXISTS siblings (
  id INTEGER PRIMARY KEY, student_id TEXT NOT NULL REFERENCES students(student_id),
  sibling_order INTEGER NOT NULL, name TEXT, gender TEXT, date_of_birth TEXT,
  school_company TEXT, year_level TEXT
);
CREATE TABLE IF NOT EXISTS accommodation_preferences (
  student_id TEXT PRIMARY KEY REFERENCES students(student_id), data_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS health_declarations (
  student_id TEXT PRIMARY KEY REFERENCES students(student_id), data_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS visa_risk_info (
  student_id TEXT PRIMARY KEY REFERENCES students(student_id), data_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS study_background (
  student_id TEXT PRIMARY KEY REFERENCES students(student_id), data_json TEXT NOT NULL,
  account_password_encrypted TEXT
);
CREATE TABLE IF NOT EXISTS emergency_contacts (
  student_id TEXT PRIMARY KEY REFERENCES students(student_id), data_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS parent_form_tokens (
  token TEXT PRIMARY KEY, consultant_id INTEGER REFERENCES users(id), student_hint TEXT,
  expires_at TEXT NOT NULL, submitted_at TEXT, invalidated_at TEXT, student_id TEXT,
  created_by INTEGER NOT NULL REFERENCES users(id), created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS student_documents (
  id INTEGER PRIMARY KEY, student_id TEXT NOT NULL REFERENCES students(student_id),
  folder_category TEXT NOT NULL, document_type TEXT NOT NULL, original_name TEXT NOT NULL,
  stored_name TEXT NOT NULL, file_path TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1,
  file_size INTEGER NOT NULL, uploaded_by INTEGER REFERENCES users(id), uploaded_at TEXT NOT NULL,
  preview_path TEXT, is_active INTEGER NOT NULL DEFAULT 1,
  document_status TEXT NOT NULL DEFAULT 'active', voided_at TEXT, voided_by INTEGER REFERENCES users(id), void_reason TEXT
);
CREATE TABLE IF NOT EXISTS upload_sessions (
  id INTEGER PRIMARY KEY, session_token TEXT UNIQUE NOT NULL,
  student_id TEXT NOT NULL REFERENCES students(student_id), module_key TEXT NOT NULL,
  uploaded_by INTEGER REFERENCES users(id), uploaded_at TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'NeedsReview', total_files INTEGER NOT NULL DEFAULT 0,
  confirmed_files INTEGER NOT NULL DEFAULT 0, cancelled_files INTEGER NOT NULL DEFAULT 0,
  notes TEXT
);
CREATE TABLE IF NOT EXISTS upload_review_items (
  id INTEGER PRIMARY KEY, upload_session_id INTEGER NOT NULL REFERENCES upload_sessions(id) ON DELETE CASCADE,
  student_id TEXT NOT NULL REFERENCES students(student_id), module_key TEXT NOT NULL,
  original_file_name TEXT NOT NULL, staging_file_path TEXT NOT NULL,
  final_file_name TEXT, final_file_path TEXT, file_type TEXT, file_size INTEGER NOT NULL DEFAULT 0,
  selected_document_type TEXT, target_name TEXT, notes TEXT,
  status TEXT NOT NULL DEFAULT 'NeedsReview',
  uploaded_by INTEGER REFERENCES users(id), uploaded_at TEXT NOT NULL,
  confirmed_by INTEGER REFERENCES users(id), confirmed_at TEXT,
  cancelled_by INTEGER REFERENCES users(id), cancelled_at TEXT,
  error_message TEXT
);
CREATE TABLE IF NOT EXISTS student_change_logs (
  id INTEGER PRIMARY KEY, student_id TEXT NOT NULL REFERENCES students(student_id),
  changed_by INTEGER REFERENCES users(id), changed_by_username TEXT,
  section TEXT NOT NULL, field_name TEXT NOT NULL, old_value TEXT, new_value TEXT,
  changed_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS offers (
  id INTEGER PRIMARY KEY, student_id TEXT NOT NULL REFERENCES students(student_id),
  school_name TEXT NOT NULL, document_type TEXT NOT NULL DEFAULT 'Offer',
  offer_date TEXT, offer_expiry_date TEXT, offer_status TEXT NOT NULL,
  offer_file_path TEXT, signed_offer_file_path TEXT, accepted_date TEXT, accepted_by INTEGER,
  deposit_amount REAL, deposit_currency TEXT, deposit_paid_date TEXT, payment_receipt_file_path TEXT,
  original_filename TEXT, uploaded_at TEXT,
  created_at TEXT NOT NULL,
  school_id INTEGER REFERENCES schools(id),
  city_id INTEGER REFERENCES school_cities(id),
  city_name_snapshot TEXT,
  sector_key TEXT CHECK(sector_key IN ('public','private')),
  classification_status TEXT NOT NULL DEFAULT 'unresolved'
    CHECK(classification_status IN ('resolved','unresolved','ambiguous'))
);
CREATE TABLE IF NOT EXISTS coe_oshc (
  id INTEGER PRIMARY KEY, student_id TEXT NOT NULL REFERENCES students(student_id),
  document_kind TEXT NOT NULL, file_path TEXT, school_name TEXT, student_name_on_coe TEXT,
  date_of_birth_on_coe TEXT, coe_start_date TEXT, coe_end_date TEXT, oshc_provider TEXT,
  oshc_policy_type TEXT, oshc_start_date TEXT, oshc_end_date TEXT,
  coe_provider TEXT, coe_course TEXT, coe_course_level TEXT,
  letter_language_course_name TEXT, letter_language_start_date TEXT, letter_language_end_date TEXT,
  letter_main_course_name TEXT, letter_main_start_date TEXT, letter_main_end_date TEXT,
  letter_year_level TEXT,
  caaw_welfare_start_date TEXT, caaw_welfare_end_date TEXT,
  oshc_company_name TEXT, oshc_policy_number TEXT, oshc_policy_issue_date TEXT,
  oshc_commencement_date TEXT, oshc_expiry_date TEXT,
  receipt_amount TEXT, receipt_date TEXT, payment_reference TEXT,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS visas (
  id INTEGER PRIMARY KEY, student_id TEXT NOT NULL REFERENCES students(student_id),
  file_path TEXT, visa_type TEXT, visa_sector TEXT, visa_grant_date TEXT, visa_start_date TEXT,
  visa_expiry_date TEXT, visa_conditions TEXT, visa_status TEXT, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS visa_cases (
  id INTEGER PRIMARY KEY, student_id TEXT NOT NULL REFERENCES students(student_id),
  application_id TEXT, visa_case_name TEXT, visa_type TEXT, applicant_name TEXT,
  relationship_to_student TEXT, application_type TEXT, responsible_consultant TEXT,
  main_applicant TEXT, guardian_applicant TEXT, applicant_count INTEGER DEFAULT 1,
  case_owner TEXT, target_lodgement_date TEXT, actual_lodgement_date TEXT,
  result_date TEXT, result_status TEXT DEFAULT 'Pending',
  case_type TEXT NOT NULL DEFAULT 'Student Visa',
  status TEXT NOT NULL DEFAULT 'Draft', visa_subclass TEXT,
  lodgement_date TEXT, grant_date TEXT, expiry_date TEXT, refusal_date TEXT, s56_due_date TEXT,
  decision_status TEXT NOT NULL DEFAULT 'Pending', notes TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL, archived_at TEXT
);
CREATE TABLE IF NOT EXISTS visa_case_document_links (
  id INTEGER PRIMARY KEY, visa_case_id INTEGER NOT NULL REFERENCES visa_cases(id) ON DELETE CASCADE,
  student_document_id INTEGER NOT NULL REFERENCES student_documents(id),
  document_role TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'Linked', notes TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  UNIQUE(visa_case_id, student_document_id, document_role)
);
CREATE TABLE IF NOT EXISTS visa_checklist_items (
  id INTEGER PRIMARY KEY, visa_case_id INTEGER NOT NULL REFERENCES visa_cases(id) ON DELETE CASCADE,
  item_key TEXT NOT NULL, title TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'Missing',
  required INTEGER NOT NULL DEFAULT 1, due_date TEXT, completed_at TEXT, notes TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  UNIQUE(visa_case_id, item_key)
);
CREATE TABLE IF NOT EXISTS visa_evidence_profiles (
  id INTEGER PRIMARY KEY, visa_case_id INTEGER NOT NULL REFERENCES visa_cases(id) ON DELETE CASCADE UNIQUE,
  total_funds_amount TEXT, total_funds_currency TEXT, funds_holder TEXT, funds_country TEXT,
  funds_bank_name TEXT, funds_source_explanation TEXT, has_large_recent_transfer TEXT,
  large_transfer_explanation TEXT, can_provide_deposit_certificate TEXT, can_provide_bank_statement TEXT,
  father_employer TEXT, father_position TEXT, father_annual_income TEXT, father_monthly_income TEXT,
  father_income_source TEXT, father_can_provide_income_certificate TEXT,
  father_can_provide_salary_statement TEXT, father_can_provide_tax_record TEXT,
  mother_employer TEXT, mother_position TEXT, mother_annual_income TEXT, mother_monthly_income TEXT,
  mother_income_source TEXT, mother_can_provide_income_certificate TEXT,
  mother_can_provide_salary_statement TEXT, mother_can_provide_tax_record TEXT,
  family_annual_income_total TEXT, family_monthly_income_total TEXT, rental_income_total TEXT,
  investment_income_total TEXT, business_income_total TEXT, income_stability_notes TEXT,
  income_funds_match_notes TEXT, has_property TEXT, property_summary TEXT,
  property_estimated_total_value TEXT, property_mortgage_total TEXT, has_company_or_business TEXT,
  company_business_summary TEXT, has_investment_assets TEXT, investment_assets_summary TEXT,
  total_asset_notes TEXT, has_property_in_home_country TEXT, has_company_in_home_country TEXT,
  has_job_in_home_country TEXT, has_elderly_family_members_in_home_country TEXT,
  elderly_family_details TEXT, has_other_children TEXT, other_children_details TEXT,
  close_relatives_in_home_country TEXT, family_responsibility_notes TEXT, return_home_plan TEXT,
  home_country_ties_summary TEXT, why_study_in_australia TEXT, why_this_school TEXT,
  why_this_city TEXT, why_this_year_level TEXT, why_not_continue_in_home_country TEXT,
  student_english_level TEXT, needs_english_program TEXT, future_study_plan TEXT,
  future_career_plan TEXT, plan_after_completion TEXT, family_support_plan TEXT,
  has_previous_visa_refusal TEXT, refusal_details TEXT, has_overstay_history TEXT,
  has_visa_cancellation_history TEXT, parents_divorced_or_separated TEXT,
  single_parent_application TEXT, non_parent_funds_provider TEXT, unusual_funds_notes TEXT,
  study_gap_notes TEXT, age_or_year_level_risk_notes TEXT, other_risk_notes TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS visa_evidence_financial_items (
  id INTEGER PRIMARY KEY, visa_case_id INTEGER NOT NULL REFERENCES visa_cases(id) ON DELETE CASCADE,
  item_type TEXT, provider TEXT, amount TEXT, currency TEXT, frequency TEXT,
  annualized_amount TEXT, source_country TEXT, evidence_available TEXT,
  evidence_notes TEXT, notes TEXT, is_archived INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS visa_evidence_asset_items (
  id INTEGER PRIMARY KEY, visa_case_id INTEGER NOT NULL REFERENCES visa_cases(id) ON DELETE CASCADE,
  asset_type TEXT, owner TEXT, asset_name TEXT, estimated_value TEXT, currency TEXT,
  country_or_region TEXT, city TEXT, evidence_available TEXT, evidence_notes TEXT,
  liquidity_status TEXT, notes TEXT, is_archived INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS visa_information_collection_packages (
  id INTEGER PRIMARY KEY,
  visa_case_id INTEGER NOT NULL REFERENCES visa_cases(id) ON DELETE CASCADE UNIQUE,
  status TEXT NOT NULL DEFAULT 'Not Started',
  notes TEXT,
  created_by INTEGER REFERENCES users(id),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS visa_information_collection_respondents (
  id INTEGER PRIMARY KEY,
  package_id INTEGER NOT NULL REFERENCES visa_information_collection_packages(id) ON DELETE CASCADE,
  visa_case_id INTEGER NOT NULL REFERENCES visa_cases(id) ON DELETE CASCADE,
  respondent_type TEXT NOT NULL,
  respondent_name TEXT,
  token TEXT NOT NULL UNIQUE,
  token_created_at TEXT,
  token_status TEXT NOT NULL DEFAULT 'Active',
  status TEXT NOT NULL DEFAULT 'Not Started',
  progress_percent INTEGER NOT NULL DEFAULT 0,
  last_saved_at TEXT,
  submitted_at TEXT,
  expires_at TEXT,
  invalidated_at TEXT,
  bound_wechat_openid TEXT,
  bound_wechat_unionid TEXT,
  bound_wechat_nickname TEXT,
  bound_wechat_avatar_url TEXT,
  bound_wechat_at TEXT,
  wechat_unbound_at TEXT,
  last_verified_at TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS visa_information_collection_access_logs (
  id INTEGER PRIMARY KEY,
  respondent_id INTEGER NOT NULL REFERENCES visa_information_collection_respondents(id) ON DELETE CASCADE,
  visa_case_id INTEGER NOT NULL REFERENCES visa_cases(id) ON DELETE CASCADE,
  event_type TEXT NOT NULL,
  event_at TEXT NOT NULL,
  ip_address TEXT,
  user_agent TEXT,
  device_type TEXT,
  wechat_openid TEXT,
  wechat_unionid TEXT,
  wechat_nickname TEXT,
  metadata_json TEXT
);
CREATE TABLE IF NOT EXISTS visa_form_assignments (
  id INTEGER PRIMARY KEY,
  package_id INTEGER NOT NULL REFERENCES visa_information_collection_packages(id) ON DELETE CASCADE,
  respondent_id INTEGER NOT NULL REFERENCES visa_information_collection_respondents(id) ON DELETE CASCADE,
  visa_case_id INTEGER NOT NULL REFERENCES visa_cases(id) ON DELETE CASCADE,
  form_type TEXT NOT NULL,
  template_version TEXT,
  status TEXT NOT NULL DEFAULT 'Not Started',
  progress_percent INTEGER NOT NULL DEFAULT 0,
  last_saved_at TEXT,
  submitted_at TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(respondent_id, form_type)
);
CREATE TABLE IF NOT EXISTS visa_form_drafts (
  id INTEGER PRIMARY KEY,
  assignment_id INTEGER NOT NULL REFERENCES visa_form_assignments(id) ON DELETE CASCADE UNIQUE,
  respondent_id INTEGER NOT NULL REFERENCES visa_information_collection_respondents(id) ON DELETE CASCADE,
  visa_case_id INTEGER NOT NULL REFERENCES visa_cases(id) ON DELETE CASCADE,
  draft_json TEXT NOT NULL DEFAULT '{}',
  status TEXT NOT NULL DEFAULT 'Draft',
  progress_percent INTEGER NOT NULL DEFAULT 0,
  last_saved_at TEXT,
  submitted_at TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS payments (
  id INTEGER PRIMARY KEY, student_id TEXT NOT NULL REFERENCES students(student_id),
  payment_category TEXT NOT NULL, amount REAL, currency TEXT, payment_method TEXT,
  paid_date TEXT, payer_name TEXT, receiver_name TEXT, receipt_file_path TEXT, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS workflow_status (
  student_id TEXT PRIMARY KEY REFERENCES students(student_id), materials_checked INTEGER NOT NULL DEFAULT 0,
  materials_checked_by INTEGER, materials_checked_at TEXT, current_stage TEXT NOT NULL DEFAULT '建档',
  contract_signed_at TEXT, visa_lodged_at TEXT, visa_finalized_at TEXT,
  completed_at TEXT, contract_to_visa_days INTEGER, visa_lodged_to_final_days INTEGER,
  completion_source TEXT, completion_metrics_updated_at TEXT
);
CREATE TABLE IF NOT EXISTS reminders (
  id INTEGER PRIMARY KEY, student_id TEXT REFERENCES students(student_id), reminder_type TEXT NOT NULL,
  title TEXT, description TEXT, related_module TEXT, related_record_id TEXT,
  assigned_to_user_id INTEGER REFERENCES users(id), assigned_to_name TEXT,
  priority TEXT NOT NULL DEFAULT 'Normal', due_date TEXT, due_datetime TEXT,
  reminder_datetime TEXT, repeat_rule TEXT, status TEXT NOT NULL DEFAULT 'Pending',
  snooze_until TEXT, completed_at TEXT, completed_by INTEGER REFERENCES users(id),
  dismissed_at TEXT, dismissed_by INTEGER REFERENCES users(id),
  created_by INTEGER REFERENCES users(id), created_at TEXT NOT NULL,
  updated_by INTEGER REFERENCES users(id), updated_at TEXT, source_type TEXT,
  source_record_id TEXT, auto_generated INTEGER NOT NULL DEFAULT 0,
  suggested_action TEXT, notes TEXT
);
CREATE TABLE IF NOT EXISTS audit_logs (
  id INTEGER PRIMARY KEY, user_id INTEGER, username TEXT, action_type TEXT NOT NULL,
  module_name TEXT NOT NULL, record_id TEXT, file_path TEXT, old_value TEXT, new_value TEXT,
  module_key TEXT, student_id TEXT, contract_id TEXT, ip_address TEXT, user_agent TEXT,
  device_info TEXT, status TEXT, error_message TEXT, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS file_action_markers (
  id INTEGER PRIMARY KEY, student_id TEXT,
  file_path TEXT NOT NULL, file_name TEXT NOT NULL, module_folder TEXT,
  marker_type TEXT NOT NULL, marker_status TEXT NOT NULL DEFAULT 'Active',
  suggested_target_folder TEXT, suggested_new_filename TEXT, reason TEXT, notes TEXT,
  created_by INTEGER REFERENCES users(id), created_at TEXT NOT NULL,
  cancelled_by INTEGER REFERENCES users(id), cancelled_at TEXT, processed_by INTEGER REFERENCES users(id),
  processed_at TEXT
);
CREATE TABLE IF NOT EXISTS system_settings (
  key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_by INTEGER, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS safebox_records (
  id INTEGER PRIMARY KEY,
  feature_name TEXT NOT NULL,
  safebox_name TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT '进行中',
  branch_name TEXT,
  before_commit TEXT,
  after_commit TEXT,
  backup_dir TEXT,
  database_changed INTEGER NOT NULL DEFAULT 0,
  test_result TEXT,
  notes TEXT,
  change_goal TEXT,
  allowed_scope TEXT,
  forbidden_scope TEXT,
  modified_files TEXT,
  unchanged_important_modules TEXT,
  test_commands TEXT,
  rollback_advice TEXT,
  codex_summary TEXT,
  record_source TEXT NOT NULL DEFAULT '手动',
  auto_started_at TEXT,
  auto_finished_at TEXT,
  git_status_before TEXT,
  git_status_after TEXT,
  stash_present INTEGER NOT NULL DEFAULT 0,
  workspace_clean_before INTEGER,
  workspace_clean_after INTEGER,
  diff_summary TEXT,
  database_risk TEXT,
  schema_change_risk TEXT,
  health_check_result TEXT,
  restore_instruction TEXT,
  created_by INTEGER REFERENCES users(id),
  updated_by INTEGER REFERENCES users(id),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS school_cities (
  id INTEGER PRIMARY KEY, city_name TEXT NOT NULL, state TEXT NOT NULL,
  country TEXT NOT NULL DEFAULT 'Australia', sort_order INTEGER NOT NULL DEFAULT 0,
  is_active INTEGER NOT NULL DEFAULT 1, UNIQUE(city_name,state)
);
CREATE TABLE IF NOT EXISTS school_sectors (
  id INTEGER PRIMARY KEY, sector_key TEXT UNIQUE NOT NULL,
  sector_name_zh TEXT NOT NULL, sector_name_en TEXT NOT NULL,
  sort_order INTEGER NOT NULL DEFAULT 0, is_active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS school_feature_categories (
  id INTEGER PRIMARY KEY, category_key TEXT UNIQUE NOT NULL,
  category_name_cn TEXT NOT NULL, category_name_en TEXT NOT NULL,
  description TEXT, sort_order INTEGER NOT NULL DEFAULT 0,
  is_active INTEGER NOT NULL DEFAULT 1,
  created_by INTEGER REFERENCES users(id), created_at TEXT NOT NULL,
  updated_by INTEGER REFERENCES users(id), updated_at TEXT
);
CREATE TABLE IF NOT EXISTS school_feature_tags (
  id INTEGER PRIMARY KEY, tag_key TEXT UNIQUE NOT NULL, tag_name_zh TEXT NOT NULL,
  tag_group TEXT NOT NULL, color TEXT, is_dynamic INTEGER NOT NULL DEFAULT 1,
  is_active INTEGER NOT NULL DEFAULT 1,
  category_id INTEGER REFERENCES school_feature_categories(id),
  tag_name_cn TEXT, tag_name_en TEXT, description TEXT, keywords TEXT,
  sort_order INTEGER NOT NULL DEFAULT 0,
  created_by INTEGER REFERENCES users(id), created_at TEXT,
  updated_by INTEGER REFERENCES users(id), updated_at TEXT
);
CREATE TABLE IF NOT EXISTS school_feature_tag_assignments (
  id INTEGER PRIMARY KEY, school_id INTEGER NOT NULL REFERENCES schools(id) ON DELETE CASCADE,
  category_id INTEGER REFERENCES school_feature_categories(id),
  tag_id INTEGER REFERENCES school_feature_tags(id),
  assignment_type TEXT NOT NULL DEFAULT 'ManualAdded',
  status TEXT NOT NULL DEFAULT 'Confirmed',
  confidence_rate REAL, source_url TEXT, source_title TEXT, source_type TEXT,
  source_excerpt TEXT, collected_at TEXT, collected_by_system INTEGER NOT NULL DEFAULT 0,
  confirmed_by INTEGER REFERENCES users(id), confirmed_at TEXT,
  rejected_by INTEGER REFERENCES users(id), rejected_at TEXT,
  notes TEXT, created_at TEXT NOT NULL, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS schools (
  id INTEGER PRIMARY KEY, school_name TEXT NOT NULL, city_id INTEGER REFERENCES school_cities(id),
  sector_key TEXT NOT NULL, school_type TEXT NOT NULL, region TEXT, suburb TEXT, state TEXT, postcode TEXT,
  suburb_id INTEGER REFERENCES school_suburbs(id), suburb_data_status TEXT NOT NULL DEFAULT 'Not Started',
  suburb_data_last_updated TEXT,
  address TEXT, website_url TEXT, phone TEXT, email TEXT, logo_file_path TEXT,
  logo_original_name TEXT, logo_source_url TEXT, year_levels TEXT, gender TEXT,
  boarding_available INTEGER NOT NULL DEFAULT 0, international_student_available INTEGER NOT NULL DEFAULT 0,
  cricos_code TEXT, provider_code TEXT, religion TEXT, curriculum TEXT,
  accommodation_type TEXT NOT NULL DEFAULT 'Unknown', accommodation_notes TEXT,
  international_year_level_min TEXT NOT NULL DEFAULT 'Unknown',
  international_year_level_max TEXT NOT NULL DEFAULT 'Unknown',
  accepted_international_year_levels TEXT NOT NULL DEFAULT '[]',
  accepted_international_year_level_pattern TEXT NOT NULL DEFAULT 'Unknown',
  international_year_level_notes TEXT,
  has_on_campus_language_program TEXT NOT NULL DEFAULT 'Unknown',
  language_program_type TEXT NOT NULL DEFAULT 'Unknown',
  language_program_notes TEXT,
  school_gender_type TEXT NOT NULL DEFAULT 'Coeducational',
  school_gender_notes TEXT,
  state_senior_certificate TEXT NOT NULL DEFAULT 'Unknown',
  offers_ib TEXT NOT NULL DEFAULT 'No',
  ib_programmes TEXT NOT NULL DEFAULT '[]',
  ib_notes TEXT,
  aeas_required TEXT NOT NULL DEFAULT 'No',
  idat_required TEXT NOT NULL DEFAULT 'No',
  ielts_required TEXT NOT NULL DEFAULT 'No',
  academic_test_notes TEXT,
  interview_required TEXT NOT NULL DEFAULT 'Unknown',
  interview_notes TEXT,
  available_intake_terms TEXT NOT NULL DEFAULT '[]',
  intake_notes TEXT,
  international_tuition_fee_amount TEXT,
  international_tuition_fee_currency TEXT NOT NULL DEFAULT 'AUD',
  international_tuition_fee_year TEXT,
  international_tuition_fee_year_level TEXT,
  international_application_fee_amount TEXT,
  international_application_fee_currency TEXT NOT NULL DEFAULT 'AUD',
  international_application_fee_year TEXT,
  international_application_fee_year_level TEXT,
  fee_notes TEXT,
  fee_source_url TEXT,
  better_education_gaokao_ranking TEXT,
  better_education_gaokao_ranking_year TEXT,
  better_education_gaokao_source_url TEXT,
  teaching_staff TEXT,
  fte_teaching_staff TEXT,
  non_teaching_staff TEXT,
  fte_non_teaching_staff TEXT,
  icsea_value TEXT,
  icsea_average TEXT,
  total_enrolments TEXT,
  male_enrolments TEXT,
  female_enrolments TEXT,
  language_background_other_than_english_pct TEXT,
  myschool_url TEXT,
  myschool_school_id TEXT,
  myschool_last_fetched_at TEXT,
  myschool_fetch_status TEXT NOT NULL DEFAULT 'Not Started',
  myschool_review_status TEXT NOT NULL DEFAULT 'Not Started',
  fte_students TEXT,
  icsea_percentile TEXT,
  indigenous_student_percentage TEXT,
  attendance_percentage TEXT,
  school_data_source_url TEXT,
  school_data_updated_at TEXT,
  latitude REAL,
  longitude REAL,
  geocode_status TEXT,
  geocoded_at TEXT,
  basic_info_json TEXT NOT NULL DEFAULT '{}', library_structure_json TEXT NOT NULL DEFAULT '{}',
  dynamic_tags_json TEXT NOT NULL DEFAULT '[]', zone_data_json TEXT NOT NULL DEFAULT '{}',
  academic_info_json TEXT NOT NULL DEFAULT '{}', data_quality_status TEXT NOT NULL DEFAULT 'Sample',
  notes TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, updated_by INTEGER REFERENCES users(id),
  UNIQUE(state, school_name)
);
CREATE TABLE IF NOT EXISTS school_suburbs (
  id INTEGER PRIMARY KEY, suburb TEXT NOT NULL, state TEXT NOT NULL, postcode TEXT,
  country TEXT NOT NULL DEFAULT 'Australia', normalized_suburb_name TEXT NOT NULL,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  UNIQUE(normalized_suburb_name,state,postcode)
);
CREATE TABLE IF NOT EXISTS school_geocode_cache (
  id INTEGER PRIMARY KEY,
  raw_input_text TEXT NOT NULL,
  normalized_query TEXT NOT NULL,
  input_type TEXT NOT NULL,
  state TEXT NOT NULL,
  formatted_address TEXT,
  suburb TEXT,
  postcode TEXT,
  latitude REAL,
  longitude REAL,
  geocode_source TEXT NOT NULL DEFAULT 'seed',
  geocoded_at TEXT,
  geocode_status TEXT NOT NULL DEFAULT 'Resolved',
  candidates_json TEXT NOT NULL DEFAULT '[]',
  warning_message TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(normalized_query,state,input_type)
);
CREATE TABLE IF NOT EXISTS school_suburb_data_sources (
  id INTEGER PRIMARY KEY, source_name TEXT NOT NULL, source_url TEXT,
  source_type TEXT NOT NULL DEFAULT 'Manual', enabled INTEGER NOT NULL DEFAULT 1,
  notes TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS school_suburb_data (
  id INTEGER PRIMARY KEY, suburb_id INTEGER NOT NULL REFERENCES school_suburbs(id) ON DELETE CASCADE,
  data_year TEXT, source_name TEXT, source_url TEXT, source_type TEXT,
  fetched_at TEXT, fetched_by INTEGER REFERENCES users(id),
  reviewed_status TEXT NOT NULL DEFAULT 'Needs Review',
  reviewed_by INTEGER REFERENCES users(id), reviewed_at TEXT,
  confidence_score REAL,
  population_total INTEGER, population_male INTEGER, population_female INTEGER, median_age REAL,
  family_count INTEGER, couple_family_count INTEGER, single_parent_family_count INTEGER,
  average_household_size REAL, household_count INTEGER,
  median_personal_income_weekly INTEGER, median_family_income_weekly INTEGER,
  median_household_income_weekly INTEGER,
  english_only_percentage REAL, non_english_language_percentage REAL, top_languages_json TEXT,
  bachelor_or_above_percentage REAL, year_12_completion_percentage REAL, education_summary TEXT,
  born_overseas_percentage REAL, top_birth_countries_json TEXT, ancestry_summary TEXT,
  separate_house_percentage REAL, apartment_percentage REAL, rented_percentage REAL, owned_percentage REAL,
  geography_type TEXT, geography_code TEXT, retrieved_at TEXT, raw_json TEXT,
  other_indicators_json TEXT, notes TEXT, error_message TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL, updated_by INTEGER REFERENCES users(id)
);
CREATE TABLE IF NOT EXISTS school_area_profiles (
  id INTEGER PRIMARY KEY,
  school_id INTEGER NOT NULL REFERENCES schools(id) ON DELETE CASCADE,
  suburb TEXT, postcode TEXT, state TEXT,
  latitude REAL, longitude REAL,
  sa1_code TEXT,
  sa2_code TEXT, sa2_name TEXT,
  sa3_code TEXT, sa3_name TEXT,
  sa4_code TEXT, sa4_name TEXT,
  lga_code TEXT, lga_name TEXT,
  data_year TEXT NOT NULL,
  source TEXT NOT NULL DEFAULT 'ABS Data API',
  source_url TEXT,
  last_checked_at TEXT,
  review_status TEXT NOT NULL DEFAULT 'Needs Review',
  confidence_score REAL,
  raw_json TEXT,
  notes TEXT,
  error_message TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  created_by INTEGER REFERENCES users(id),
  updated_by INTEGER REFERENCES users(id)
);
CREATE TABLE IF NOT EXISTS school_area_profile_metrics (
  id INTEGER PRIMARY KEY,
  profile_id INTEGER NOT NULL REFERENCES school_area_profiles(id) ON DELETE CASCADE,
  metric_key TEXT NOT NULL,
  metric_label TEXT NOT NULL,
  metric_value REAL,
  metric_unit TEXT,
  data_year TEXT NOT NULL,
  source TEXT NOT NULL DEFAULT 'ABS Data API',
  source_url TEXT,
  raw_json TEXT,
  notes TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS csa_crime_area_statistics (
  id INTEGER PRIMARY KEY,
  state TEXT NOT NULL DEFAULT 'VIC',
  data_level TEXT NOT NULL,
  area_name TEXT NOT NULL,
  normalized_area_key TEXT NOT NULL,
  suburb TEXT,
  postcode TEXT,
  lga_name TEXT,
  data_year_ending TEXT NOT NULL,
  source_dataset TEXT NOT NULL,
  source TEXT NOT NULL DEFAULT 'Crime Statistics Agency / DataVic',
  source_url TEXT NOT NULL,
  total_recorded_offences INTEGER,
  offence_rate_per_100000 REAL,
  criminal_incidents_total INTEGER,
  criminal_incidents_rate_per_100000 REAL,
  family_incidents_total INTEGER,
  family_incidents_rate_per_100000 REAL,
  crimes_against_person INTEGER,
  property_and_deception_offences INTEGER,
  drug_offences INTEGER,
  public_order_and_security_offences INTEGER,
  justice_procedures_offences INTEGER,
  other_offences INTEGER,
  residential_burglary_count INTEGER,
  theft_from_motor_vehicle_count INTEGER,
  motor_vehicle_theft_count INTEGER,
  robbery_count INTEGER,
  assault_count INTEGER,
  property_damage_count INTEGER,
  trend_1_year_percent REAL,
  trend_5_year_percent REAL,
  notes TEXT,
  raw_json TEXT,
  imported_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(state,data_level,normalized_area_key,data_year_ending,source_dataset)
);
CREATE TABLE IF NOT EXISTS school_area_crime_profiles (
  id INTEGER PRIMARY KEY,
  school_id INTEGER NOT NULL REFERENCES schools(id) ON DELETE CASCADE,
  state TEXT NOT NULL DEFAULT 'VIC',
  suburb TEXT,
  postcode TEXT,
  lga_name TEXT,
  data_level TEXT NOT NULL,
  data_year_ending TEXT NOT NULL,
  source TEXT NOT NULL DEFAULT 'Crime Statistics Agency / DataVic',
  source_url TEXT NOT NULL,
  total_recorded_offences INTEGER,
  offence_rate_per_100000 REAL,
  criminal_incidents_total INTEGER,
  criminal_incidents_rate_per_100000 REAL,
  family_incidents_total INTEGER,
  family_incidents_rate_per_100000 REAL,
  crimes_against_person INTEGER,
  property_and_deception_offences INTEGER,
  drug_offences INTEGER,
  public_order_and_security_offences INTEGER,
  justice_procedures_offences INTEGER,
  other_offences INTEGER,
  residential_burglary_count INTEGER,
  theft_from_motor_vehicle_count INTEGER,
  motor_vehicle_theft_count INTEGER,
  robbery_count INTEGER,
  assault_count INTEGER,
  property_damage_count INTEGER,
  trend_1_year_percent REAL,
  trend_5_year_percent REAL,
  notes TEXT,
  last_checked_at TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(school_id,data_level,data_year_ending)
);
CREATE TABLE IF NOT EXISTS rental_market (
  id INTEGER PRIMARY KEY,
  state TEXT NOT NULL,
  suburb TEXT NOT NULL,
  postcode TEXT,
  property_type TEXT NOT NULL,
  bedrooms TEXT,
  median_weekly_rent REAL,
  quarter TEXT NOT NULL,
  source TEXT NOT NULL,
  last_updated TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS official_rental_market_details (
  id INTEGER PRIMARY KEY,
  state TEXT NOT NULL,
  area_level TEXT NOT NULL,
  area_name TEXT NOT NULL,
  suburb TEXT,
  postcode TEXT,
  lga_name TEXT,
  property_type TEXT NOT NULL,
  bedrooms TEXT,
  median_weekly_rent REAL,
  quarter TEXT NOT NULL,
  year INTEGER,
  source TEXT NOT NULL,
  source_url TEXT,
  last_updated TEXT NOT NULL,
  raw_json TEXT,
  imported_at TEXT NOT NULL,
  UNIQUE(state,area_level,area_name,property_type,bedrooms,quarter,source)
);
CREATE TABLE IF NOT EXISTS property_market (
  id INTEGER PRIMARY KEY,
  state TEXT NOT NULL,
  suburb TEXT NOT NULL,
  postcode TEXT,
  property_type TEXT NOT NULL,
  median_sale_price REAL,
  period TEXT NOT NULL,
  sales_count INTEGER,
  source TEXT NOT NULL,
  last_updated TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS geo_areas (
  id INTEGER PRIMARY KEY,
  state TEXT NOT NULL,
  area_type TEXT NOT NULL,
  area_name TEXT NOT NULL,
  suburb TEXT,
  postcode TEXT,
  sa2_code TEXT,
  lga_code TEXT,
  geometry TEXT,
  latitude REAL,
  longitude REAL,
  UNIQUE(state,area_type,area_name,postcode)
);
CREATE TABLE IF NOT EXISTS school_radius_area_links (
  id INTEGER PRIMARY KEY,
  school_id INTEGER NOT NULL REFERENCES schools(id) ON DELETE CASCADE,
  radius_km INTEGER NOT NULL,
  area_type TEXT NOT NULL,
  area_id INTEGER REFERENCES geo_areas(id) ON DELETE CASCADE,
  area_name TEXT NOT NULL,
  distance_from_school_km REAL,
  overlap_ratio REAL,
  calculation_method TEXT NOT NULL,
  last_calculated_at TEXT NOT NULL,
  UNIQUE(school_id,radius_km,area_type,area_id)
);
CREATE TABLE IF NOT EXISTS school_radius_market_summary (
  id INTEGER PRIMARY KEY,
  school_id INTEGER NOT NULL REFERENCES schools(id) ON DELETE CASCADE,
  radius_km INTEGER NOT NULL,
  median_weekly_rent_all REAL,
  median_weekly_rent_house REAL,
  median_weekly_rent_unit REAL,
  median_house_price REAL,
  median_unit_price REAL,
  source_period_rent TEXT,
  source_period_sale TEXT,
  confidence_level TEXT NOT NULL DEFAULT 'low',
  notes TEXT,
  last_calculated_at TEXT NOT NULL,
  UNIQUE(school_id,radius_km)
);
CREATE TABLE IF NOT EXISTS data_sources (
  id INTEGER PRIMARY KEY,
  source_name TEXT NOT NULL,
  source_type TEXT NOT NULL,
  state TEXT NOT NULL DEFAULT 'VIC',
  source_url TEXT,
  update_frequency TEXT NOT NULL,
  auto_check_enabled INTEGER NOT NULL DEFAULT 0,
  requires_admin_confirmation INTEGER NOT NULL DEFAULT 1,
  last_checked_at TEXT,
  last_successful_update_at TEXT,
  latest_period TEXT,
  status TEXT NOT NULL DEFAULT 'Not Checked',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(source_name,state)
);
CREATE TABLE IF NOT EXISTS data_update_jobs (
  id INTEGER PRIMARY KEY,
  job_type TEXT NOT NULL,
  state TEXT NOT NULL DEFAULT 'VIC',
  source_id INTEGER REFERENCES data_sources(id),
  status TEXT NOT NULL DEFAULT 'Queued',
  started_at TEXT,
  finished_at TEXT,
  triggered_by INTEGER REFERENCES users(id),
  trigger_type TEXT NOT NULL DEFAULT 'manual',
  source_period TEXT,
  records_found INTEGER NOT NULL DEFAULT 0,
  records_created INTEGER NOT NULL DEFAULT 0,
  records_updated INTEGER NOT NULL DEFAULT 0,
  records_failed INTEGER NOT NULL DEFAULT 0,
  needs_manual_confirmation_count INTEGER NOT NULL DEFAULT 0,
  error_message TEXT,
  preview_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS data_update_logs (
  id INTEGER PRIMARY KEY,
  job_id INTEGER REFERENCES data_update_jobs(id) ON DELETE CASCADE,
  level TEXT NOT NULL DEFAULT 'info',
  message TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS data_update_previews (
  id INTEGER PRIMARY KEY,
  job_id INTEGER NOT NULL REFERENCES data_update_jobs(id) ON DELETE CASCADE,
  source_id INTEGER REFERENCES data_sources(id),
  source_name TEXT NOT NULL,
  source_period TEXT,
  source_file TEXT,
  records_found INTEGER NOT NULL DEFAULT 0,
  records_created INTEGER NOT NULL DEFAULT 0,
  records_updated INTEGER NOT NULL DEFAULT 0,
  records_inactivated INTEGER NOT NULL DEFAULT 0,
  unmatched_suburb_count INTEGER NOT NULL DEFAULT 0,
  unmatched_postcode_count INTEGER NOT NULL DEFAULT 0,
  unmatched_lga_sa2_count INTEGER NOT NULL DEFAULT 0,
  needs_manual_confirmation_count INTEGER NOT NULL DEFAULT 0,
  affected_school_count INTEGER NOT NULL DEFAULT 0,
  recalculates_market_summary INTEGER NOT NULL DEFAULT 0,
  recalculates_crime_summary INTEGER NOT NULL DEFAULT 0,
  preview_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS data_update_snapshots (
  id INTEGER PRIMARY KEY,
  job_id INTEGER NOT NULL REFERENCES data_update_jobs(id) ON DELETE CASCADE,
  table_name TEXT NOT NULL,
  operation TEXT NOT NULL,
  record_key TEXT NOT NULL,
  record_json TEXT NOT NULL DEFAULT '{}',
  restored_at TEXT,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS school_plan_reports (
  id INTEGER PRIMARY KEY,
  student_id TEXT NOT NULL REFERENCES students(student_id),
  application_id TEXT NOT NULL,
  report_title TEXT NOT NULL DEFAULT 'School Plan Report',
  report_version INTEGER NOT NULL,
  file_name TEXT,
  file_path TEXT,
  file_url TEXT,
  status TEXT NOT NULL DEFAULT 'draft',
  selected_school_ids TEXT NOT NULL DEFAULT '[]',
  created_by_user_id INTEGER REFERENCES users(id),
  created_by_name TEXT,
  shared_by_name TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  confirmed_at TEXT,
  confirmed_by_name TEXT,
  confirmed_by_contact_id INTEGER,
  confirmed_relationship_to_student TEXT,
  confirmed_contact_email TEXT,
  confirmed_contact_mobile TEXT,
  confirmed_handled_by_name TEXT,
  confirmation_ip TEXT,
  confirmation_user_agent TEXT,
  confirmation_note TEXT,
  invalidated_at TEXT,
  invalidated_reason TEXT,
  active_share_link_id INTEGER,
  snapshot_json TEXT NOT NULL DEFAULT '{}',
  allow_download INTEGER NOT NULL DEFAULT 0,
  copy_protection_enabled INTEGER NOT NULL DEFAULT 1,
  print_protection_enabled INTEGER NOT NULL DEFAULT 1,
  watermark_enabled INTEGER NOT NULL DEFAULT 1,
  custom_cover_template_id INTEGER,
  city_id INTEGER REFERENCES school_cities(id),
  city_name_snapshot TEXT,
  plan_sector TEXT CHECK(plan_sector IN ('public','private')),
  is_current INTEGER NOT NULL DEFAULT 0 CHECK(is_current IN (0,1)),
  superseded_by_report_id INTEGER REFERENCES school_plan_reports(id),
  scope_review_status TEXT NOT NULL DEFAULT 'pending'
    CHECK(scope_review_status IN ('pending','resolved','needs_review'))
);
CREATE TABLE IF NOT EXISTS school_plan_report_viewers (
  id INTEGER PRIMARY KEY,
  report_id INTEGER NOT NULL REFERENCES school_plan_reports(id) ON DELETE CASCADE,
  student_id TEXT NOT NULL REFERENCES students(student_id),
  application_id TEXT NOT NULL,
  viewer_role TEXT NOT NULL DEFAULT 'Primary Parent',
  viewer_name TEXT,
  viewer_mobile TEXT,
  viewer_email TEXT,
  can_confirm INTEGER NOT NULL DEFAULT 0,
  is_primary INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'active',
  created_by_user_id INTEGER REFERENCES users(id),
  created_by_name TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  revoked_at TEXT,
  revoked_reason TEXT
);
CREATE TABLE IF NOT EXISTS school_plan_report_links (
  id INTEGER PRIMARY KEY,
  viewer_id INTEGER REFERENCES school_plan_report_viewers(id) ON DELETE SET NULL,
  report_id INTEGER NOT NULL REFERENCES school_plan_reports(id) ON DELETE CASCADE,
  student_id TEXT NOT NULL REFERENCES students(student_id),
  application_id TEXT NOT NULL,
  token_hash TEXT NOT NULL UNIQUE,
  token_last_4 TEXT NOT NULL,
  share_url TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'active',
  expires_at TEXT NOT NULL,
  created_by_user_id INTEGER REFERENCES users(id),
  created_by_name TEXT,
  created_at TEXT NOT NULL,
  first_viewed_at TEXT,
  last_viewed_at TEXT,
  viewed_count INTEGER NOT NULL DEFAULT 0,
  confirmed_at TEXT,
  invalidated_at TEXT,
  invalidated_reason TEXT,
  viewer_watermark_text TEXT,
  download_allowed INTEGER NOT NULL DEFAULT 0,
  print_allowed INTEGER NOT NULL DEFAULT 0,
  copy_allowed INTEGER NOT NULL DEFAULT 0,
  security_mode TEXT NOT NULL DEFAULT 'wechat_required',
  bound_wechat_openid TEXT,
  bound_wechat_unionid TEXT,
  bound_wechat_nickname TEXT,
  bound_wechat_avatar_url TEXT,
  bound_wechat_at TEXT,
  unbound_wechat_at TEXT,
  last_login_at TEXT
);
CREATE TABLE IF NOT EXISTS school_plan_report_events (
  id INTEGER PRIMARY KEY,
  report_id INTEGER REFERENCES school_plan_reports(id) ON DELETE CASCADE,
  viewer_id INTEGER REFERENCES school_plan_report_viewers(id) ON DELETE SET NULL,
  link_id INTEGER REFERENCES school_plan_report_links(id) ON DELETE SET NULL,
  event_type TEXT NOT NULL,
  event_by_user_id INTEGER REFERENCES users(id),
  event_by_name TEXT,
  event_at TEXT NOT NULL,
  ip_address TEXT,
  user_agent TEXT,
  metadata_json TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS school_plan_report_view_logs (
  id INTEGER PRIMARY KEY,
  report_id INTEGER NOT NULL REFERENCES school_plan_reports(id) ON DELETE CASCADE,
  viewer_id INTEGER REFERENCES school_plan_report_viewers(id) ON DELETE SET NULL,
  link_id INTEGER NOT NULL REFERENCES school_plan_report_links(id) ON DELETE CASCADE,
  viewed_at TEXT NOT NULL,
  ip_address TEXT,
  user_agent TEXT,
  device_type TEXT,
  wechat_openid TEXT,
  wechat_unionid TEXT,
  wechat_nickname TEXT,
  page_number INTEGER,
  event_type TEXT NOT NULL DEFAULT 'view',
  dwell_seconds INTEGER,
  metadata_json TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS school_plan_report_confirmations (
  id INTEGER PRIMARY KEY,
  report_id INTEGER NOT NULL REFERENCES school_plan_reports(id) ON DELETE CASCADE,
  viewer_id INTEGER REFERENCES school_plan_report_viewers(id) ON DELETE SET NULL,
  student_id TEXT NOT NULL REFERENCES students(student_id),
  application_id TEXT NOT NULL,
  link_id INTEGER NOT NULL REFERENCES school_plan_report_links(id) ON DELETE CASCADE,
  confirmed_at TEXT NOT NULL,
  confirmed_ip TEXT,
  user_agent TEXT,
  wechat_openid TEXT,
  wechat_unionid TEXT,
  wechat_nickname TEXT,
  signature_image_path TEXT NOT NULL,
  confirmation_pdf_path TEXT,
  confirmation_statement TEXT,
  status TEXT NOT NULL DEFAULT 'confirmed',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS school_plan_report_school_rankings (
  id INTEGER PRIMARY KEY,
  confirmation_id INTEGER NOT NULL REFERENCES school_plan_report_confirmations(id) ON DELETE CASCADE,
  report_id INTEGER NOT NULL REFERENCES school_plan_reports(id) ON DELETE CASCADE,
  school_id INTEGER,
  school_name TEXT NOT NULL,
  rank_order INTEGER NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS school_plan_confirmed_schools (
  id INTEGER PRIMARY KEY,
  confirmation_id INTEGER NOT NULL REFERENCES school_plan_report_confirmations(id) ON DELETE CASCADE,
  report_id INTEGER NOT NULL REFERENCES school_plan_reports(id) ON DELETE CASCADE,
  student_id TEXT NOT NULL REFERENCES students(student_id),
  school_id INTEGER REFERENCES schools(id),
  school_name_snapshot TEXT NOT NULL,
  city_id INTEGER REFERENCES school_cities(id),
  city_name_snapshot TEXT,
  plan_sector TEXT NOT NULL CHECK(plan_sector IN ('public','private')),
  decision_mode TEXT NOT NULL CHECK(decision_mode IN ('ranked','selected')),
  rank_order INTEGER,
  is_selected INTEGER NOT NULL DEFAULT 1 CHECK(is_selected IN (0,1)),
  submission_status TEXT NOT NULL DEFAULT 'not_submitted' CHECK(submission_status IN ('not_submitted','submitted')),
  submitted_at TEXT,
  submitted_by_user_id INTEGER REFERENCES users(id),
  submitted_by_name TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(confirmation_id, school_id, school_name_snapshot)
);
CREATE TRIGGER IF NOT EXISTS school_plan_confirmed_schools_ownership_insert
BEFORE INSERT ON school_plan_confirmed_schools
WHEN NOT EXISTS (
  SELECT 1 FROM school_plan_report_confirmations
  WHERE id=NEW.confirmation_id AND report_id=NEW.report_id
)
OR NOT EXISTS (
  SELECT 1 FROM school_plan_reports
  WHERE id=NEW.report_id AND student_id=NEW.student_id
)
BEGIN
  SELECT RAISE(ABORT, 'confirmed school ownership mismatch');
END;
CREATE TRIGGER IF NOT EXISTS school_plan_confirmed_schools_ownership_update
BEFORE UPDATE OF confirmation_id,report_id,student_id ON school_plan_confirmed_schools
WHEN NOT EXISTS (
  SELECT 1 FROM school_plan_report_confirmations
  WHERE id=NEW.confirmation_id AND report_id=NEW.report_id
)
OR NOT EXISTS (
  SELECT 1 FROM school_plan_reports
  WHERE id=NEW.report_id AND student_id=NEW.student_id
)
BEGIN
  SELECT RAISE(ABORT, 'confirmed school ownership mismatch');
END;
CREATE UNIQUE INDEX IF NOT EXISTS uq_school_plan_confirmed_schools_null_school
ON school_plan_confirmed_schools(confirmation_id, school_name_snapshot)
WHERE school_id IS NULL;
CREATE TRIGGER IF NOT EXISTS school_plan_confirmed_schools_not_submitted_evidence_insert
BEFORE INSERT ON school_plan_confirmed_schools
WHEN NEW.submission_status='not_submitted'
 AND (NEW.submitted_at IS NOT NULL
      OR NEW.submitted_by_user_id IS NOT NULL
      OR NEW.submitted_by_name IS NOT NULL)
BEGIN
  SELECT RAISE(ABORT, 'not submitted schools cannot retain submission evidence');
END;
CREATE TRIGGER IF NOT EXISTS school_plan_confirmed_schools_not_submitted_evidence_update
BEFORE UPDATE OF submission_status,submitted_at,submitted_by_user_id,submitted_by_name
ON school_plan_confirmed_schools
WHEN NEW.submission_status='not_submitted'
 AND (NEW.submitted_at IS NOT NULL
      OR NEW.submitted_by_user_id IS NOT NULL
      OR NEW.submitted_by_name IS NOT NULL)
BEGIN
  SELECT RAISE(ABORT, 'not submitted schools cannot retain submission evidence');
END;
-- Service-internal audit table: application services must never insert rows directly.
-- Submission-status transition triggers are the sole supported event writer.
CREATE TABLE IF NOT EXISTS school_plan_school_submission_events (
  id INTEGER PRIMARY KEY,
  confirmed_school_id INTEGER NOT NULL REFERENCES school_plan_confirmed_schools(id) ON DELETE RESTRICT,
  student_id TEXT NOT NULL REFERENCES students(student_id),
  report_id INTEGER NOT NULL REFERENCES school_plan_reports(id) ON DELETE RESTRICT,
  previous_submission_status TEXT NOT NULL CHECK(previous_submission_status IN ('not_submitted','submitted')),
  new_submission_status TEXT NOT NULL CHECK(new_submission_status IN ('not_submitted','submitted')),
  submitted_at TEXT,
  submitted_by_user_id INTEGER REFERENCES users(id),
  submitted_by_name TEXT,
  occurred_at TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS school_plan_school_submission_events_ownership_insert
BEFORE INSERT ON school_plan_school_submission_events
WHEN NOT EXISTS (
  SELECT 1 FROM school_plan_confirmed_schools
  WHERE id=NEW.confirmed_school_id
    AND report_id=NEW.report_id
    AND student_id=NEW.student_id
)
BEGIN
  SELECT RAISE(ABORT, 'submission event ownership mismatch');
END;
CREATE TRIGGER IF NOT EXISTS school_plan_school_submission_events_ownership_update
BEFORE UPDATE OF confirmed_school_id,student_id,report_id ON school_plan_school_submission_events
WHEN NOT EXISTS (
  SELECT 1 FROM school_plan_confirmed_schools
  WHERE id=NEW.confirmed_school_id
    AND report_id=NEW.report_id
    AND student_id=NEW.student_id
)
BEGIN
  SELECT RAISE(ABORT, 'submission event ownership mismatch');
END;
CREATE TRIGGER IF NOT EXISTS school_plan_confirmed_schools_identity_no_update_after_event
BEFORE UPDATE OF confirmation_id,report_id,student_id ON school_plan_confirmed_schools
WHEN EXISTS (
  SELECT 1 FROM school_plan_school_submission_events
  WHERE confirmed_school_id=OLD.id
)
BEGIN
  SELECT RAISE(ABORT, 'submitted school identity is immutable');
END;
CREATE TRIGGER IF NOT EXISTS school_plan_confirmed_schools_submission_event_after_update
AFTER UPDATE OF submission_status ON school_plan_confirmed_schools
WHEN OLD.submission_status <> NEW.submission_status
BEGIN
  INSERT INTO school_plan_school_submission_events(
    confirmed_school_id,student_id,report_id,
    previous_submission_status,new_submission_status,
    submitted_at,submitted_by_user_id,submitted_by_name,
    occurred_at,created_at
  ) VALUES(
    NEW.id,NEW.student_id,NEW.report_id,
    OLD.submission_status,NEW.submission_status,
    CASE WHEN NEW.submission_status='submitted' THEN NEW.submitted_at END,
    CASE WHEN NEW.submission_status='submitted' THEN NEW.submitted_by_user_id END,
    CASE WHEN NEW.submission_status='submitted' THEN NEW.submitted_by_name END,
    NEW.updated_at,NEW.updated_at
  );
END;
CREATE TRIGGER IF NOT EXISTS school_plan_school_submission_events_no_update
BEFORE UPDATE ON school_plan_school_submission_events
BEGIN
  SELECT RAISE(ABORT, 'school plan submission events are append-only');
END;
CREATE TRIGGER IF NOT EXISTS school_plan_school_submission_events_no_delete
BEFORE DELETE ON school_plan_school_submission_events
BEGIN
  SELECT RAISE(ABORT, 'school plan submission events are append-only');
END;
CREATE TABLE IF NOT EXISTS student_offer_acceptances (
  student_id TEXT PRIMARY KEY REFERENCES students(student_id) ON DELETE CASCADE,
  offer_id INTEGER NOT NULL UNIQUE REFERENCES offers(id) ON DELETE CASCADE,
  status_before_acceptance TEXT NOT NULL,
  accepted_at TEXT NOT NULL,
  accepted_by_user_id INTEGER REFERENCES users(id),
  accepted_by_name TEXT,
  updated_at TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS student_offer_acceptances_ownership_insert
BEFORE INSERT ON student_offer_acceptances
WHEN NOT EXISTS (
  SELECT 1 FROM offers
  WHERE id=NEW.offer_id AND student_id=NEW.student_id
)
BEGIN
  SELECT RAISE(ABORT, 'accepted Offer ownership mismatch');
END;
CREATE TRIGGER IF NOT EXISTS student_offer_acceptances_ownership_update
BEFORE UPDATE OF student_id,offer_id ON student_offer_acceptances
WHEN NOT EXISTS (
  SELECT 1 FROM offers
  WHERE id=NEW.offer_id AND student_id=NEW.student_id
)
BEGIN
  SELECT RAISE(ABORT, 'accepted Offer ownership mismatch');
END;
CREATE TABLE IF NOT EXISTS offer_acceptance_history (
  id INTEGER PRIMARY KEY,
  student_id TEXT NOT NULL REFERENCES students(student_id),
  previous_offer_id INTEGER REFERENCES offers(id) ON DELETE RESTRICT,
  new_offer_id INTEGER NOT NULL REFERENCES offers(id) ON DELETE RESTRICT,
  previous_offer_restored_status TEXT,
  new_offer_previous_status TEXT NOT NULL,
  accepted_by_user_id INTEGER REFERENCES users(id),
  accepted_by_name TEXT,
  accepted_at TEXT NOT NULL,
  coe_present INTEGER NOT NULL CHECK(coe_present IN (0,1)),
  coe_warning_acknowledged INTEGER NOT NULL CHECK(coe_warning_acknowledged IN (0,1)),
  created_at TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS offer_acceptance_history_ownership_insert
BEFORE INSERT ON offer_acceptance_history
WHEN NOT EXISTS (
  SELECT 1 FROM offers
  WHERE id=NEW.new_offer_id AND student_id=NEW.student_id
)
OR (
  NEW.previous_offer_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM offers
    WHERE id=NEW.previous_offer_id AND student_id=NEW.student_id
  )
)
BEGIN
  SELECT RAISE(ABORT, 'Offer acceptance history ownership mismatch');
END;
CREATE TRIGGER IF NOT EXISTS offer_acceptance_history_ownership_update
BEFORE UPDATE OF student_id,previous_offer_id,new_offer_id ON offer_acceptance_history
WHEN NOT EXISTS (
  SELECT 1 FROM offers
  WHERE id=NEW.new_offer_id AND student_id=NEW.student_id
)
OR (
  NEW.previous_offer_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM offers
    WHERE id=NEW.previous_offer_id AND student_id=NEW.student_id
  )
)
BEGIN
  SELECT RAISE(ABORT, 'Offer acceptance history ownership mismatch');
END;
CREATE TRIGGER IF NOT EXISTS offer_acceptance_history_no_update
BEFORE UPDATE ON offer_acceptance_history
BEGIN
  SELECT RAISE(ABORT, 'offer acceptance history is append-only');
END;
CREATE TRIGGER IF NOT EXISTS offer_acceptance_history_no_delete
BEFORE DELETE ON offer_acceptance_history
BEGIN
  SELECT RAISE(ABORT, 'offer acceptance history is append-only');
END;
CREATE TRIGGER IF NOT EXISTS offers_student_identity_no_update
BEFORE UPDATE OF student_id ON offers
WHEN EXISTS (
  SELECT 1 FROM student_offer_acceptances
  WHERE offer_id=OLD.id
)
OR EXISTS (
  SELECT 1 FROM offer_acceptance_history
  WHERE previous_offer_id=OLD.id OR new_offer_id=OLD.id
)
BEGIN
  SELECT RAISE(ABORT, 'referenced Offer ownership is immutable');
END;
CREATE TRIGGER IF NOT EXISTS school_plan_reports_student_identity_no_update
BEFORE UPDATE OF student_id ON school_plan_reports
WHEN EXISTS (
  SELECT 1 FROM school_plan_report_confirmations WHERE report_id=OLD.id
)
OR EXISTS (
  SELECT 1 FROM school_plan_confirmed_schools WHERE report_id=OLD.id
)
OR EXISTS (
  SELECT 1 FROM school_plan_school_submission_events WHERE report_id=OLD.id
)
BEGIN
  SELECT RAISE(ABORT, 'referenced school plan report ownership is immutable');
END;
CREATE TRIGGER IF NOT EXISTS school_plan_report_confirmations_report_identity_no_update
BEFORE UPDATE OF report_id ON school_plan_report_confirmations
WHEN EXISTS (
  SELECT 1 FROM school_plan_confirmed_schools
  WHERE confirmation_id=OLD.id
)
BEGIN
  SELECT RAISE(ABORT, 'confirmed school report identity is immutable');
END;
CREATE TABLE IF NOT EXISTS school_plan_offer_migration_exceptions (
  id INTEGER PRIMARY KEY,
  entity_type TEXT NOT NULL,
  entity_id INTEGER NOT NULL,
  student_id TEXT REFERENCES students(student_id),
  exception_type TEXT NOT NULL,
  details_json TEXT NOT NULL DEFAULT '{}',
  status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','resolved')),
  created_at TEXT NOT NULL,
  resolved_at TEXT,
  resolved_by_user_id INTEGER REFERENCES users(id),
  resolution_notes TEXT
);
CREATE TABLE IF NOT EXISTS staging_schools (
  id INTEGER PRIMARY KEY,
  job_id INTEGER REFERENCES data_update_jobs(id) ON DELETE CASCADE,
  school_id INTEGER,
  school_name TEXT NOT NULL,
  state TEXT NOT NULL,
  sector TEXT,
  school_type TEXT,
  address TEXT,
  suburb TEXT,
  postcode TEXT,
  latitude REAL,
  longitude REAL,
  year_levels TEXT,
  website TEXT,
  source TEXT,
  status TEXT,
  last_updated TEXT NOT NULL,
  validation_status TEXT NOT NULL DEFAULT 'Pending',
  validation_error TEXT
);
CREATE TABLE IF NOT EXISTS school_zones (
  id INTEGER PRIMARY KEY,
  school_id INTEGER REFERENCES schools(id) ON DELETE CASCADE,
  state TEXT NOT NULL,
  enrolment_year TEXT NOT NULL,
  year_level TEXT,
  zone_name TEXT,
  geometry TEXT,
  source TEXT,
  source_file TEXT,
  last_updated TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'active'
);
CREATE TABLE IF NOT EXISTS crime_statistics (
  id INTEGER PRIMARY KEY,
  state TEXT NOT NULL,
  area_type TEXT NOT NULL,
  area_name TEXT NOT NULL,
  suburb TEXT,
  postcode TEXT,
  lga_name TEXT,
  offence_category TEXT,
  offence_subcategory TEXT,
  offence_count INTEGER,
  offence_rate_per_100k REAL,
  period_start TEXT,
  period_end TEXT,
  source TEXT,
  source_period TEXT,
  last_updated TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS school_area_crime_summary (
  id INTEGER PRIMARY KEY,
  school_id INTEGER NOT NULL REFERENCES schools(id) ON DELETE CASCADE,
  area_type TEXT NOT NULL,
  radius_km INTEGER,
  suburb TEXT,
  postcode TEXT,
  lga_name TEXT,
  total_offence_count INTEGER,
  offence_rate_per_100k REAL,
  top_offence_categories TEXT,
  year_on_year_change_percent REAL,
  source_period TEXT,
  confidence_level TEXT NOT NULL DEFAULT 'low',
  notes TEXT,
  last_calculated_at TEXT NOT NULL,
  UNIQUE(school_id,area_type,radius_km,source_period)
);
CREATE TABLE IF NOT EXISTS staging_rental_market (
  id INTEGER PRIMARY KEY,
  job_id INTEGER REFERENCES data_update_jobs(id) ON DELETE CASCADE,
  state TEXT NOT NULL,
  suburb TEXT NOT NULL,
  postcode TEXT,
  property_type TEXT NOT NULL,
  bedrooms TEXT,
  median_weekly_rent REAL,
  quarter TEXT NOT NULL,
  source TEXT NOT NULL,
  last_updated TEXT NOT NULL,
  validation_status TEXT NOT NULL DEFAULT 'Pending',
  validation_error TEXT
);
CREATE TABLE IF NOT EXISTS staging_property_market (
  id INTEGER PRIMARY KEY,
  job_id INTEGER REFERENCES data_update_jobs(id) ON DELETE CASCADE,
  state TEXT NOT NULL,
  suburb TEXT NOT NULL,
  postcode TEXT,
  property_type TEXT NOT NULL,
  median_sale_price REAL,
  period TEXT NOT NULL,
  sales_count INTEGER,
  source TEXT NOT NULL,
  last_updated TEXT NOT NULL,
  validation_status TEXT NOT NULL DEFAULT 'Pending',
  validation_error TEXT
);
CREATE TABLE IF NOT EXISTS staging_school_zones (
  id INTEGER PRIMARY KEY,
  job_id INTEGER REFERENCES data_update_jobs(id) ON DELETE CASCADE,
  school_id INTEGER,
  state TEXT NOT NULL,
  enrolment_year TEXT NOT NULL,
  year_level TEXT,
  zone_name TEXT,
  geometry TEXT,
  source TEXT,
  source_file TEXT,
  last_updated TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'staged',
  validation_status TEXT NOT NULL DEFAULT 'Pending',
  validation_error TEXT
);
CREATE TABLE IF NOT EXISTS school_profile_data (
  id INTEGER PRIMARY KEY,
  school_id INTEGER NOT NULL REFERENCES schools(id) ON DELETE CASCADE,
  acara_school_id TEXT,
  myschool_school_id TEXT,
  source_year TEXT NOT NULL,
  school_icsea_value INTEGER,
  average_icsea_value INTEGER,
  icsea_comparison_baseline INTEGER,
  teaching_staff_headcount INTEGER,
  teaching_staff_fte REAL,
  non_teaching_staff_headcount INTEGER,
  non_teaching_staff_fte REAL,
  total_enrolments INTEGER,
  boys_count INTEGER,
  girls_count INTEGER,
  boys_percentage REAL,
  girls_percentage REAL,
  gender_balance_score REAL,
  indigenous_students_value REAL,
  indigenous_students_value_type TEXT,
  indigenous_students_percentage REAL,
  lbote_value REAL,
  lbote_value_type TEXT,
  lbote_percentage REAL,
  student_to_teacher_ratio REAL,
  icsea_difference_from_baseline INTEGER,
  source_name TEXT NOT NULL,
  source_url TEXT,
  source_file_name TEXT,
  imported_at TEXT NOT NULL,
  last_updated TEXT NOT NULL,
  is_active INTEGER NOT NULL DEFAULT 1,
  version_number INTEGER NOT NULL DEFAULT 1,
  confidence_level TEXT NOT NULL DEFAULT 'medium',
  notes TEXT
);
CREATE TABLE IF NOT EXISTS staging_school_profile_data (
  id INTEGER PRIMARY KEY,
  job_id INTEGER REFERENCES data_update_jobs(id) ON DELETE CASCADE,
  school_id INTEGER REFERENCES schools(id) ON DELETE CASCADE,
  acara_school_id TEXT,
  myschool_school_id TEXT,
  source_year TEXT,
  school_icsea_value INTEGER,
  average_icsea_value INTEGER,
  icsea_comparison_baseline INTEGER,
  teaching_staff_headcount INTEGER,
  teaching_staff_fte REAL,
  non_teaching_staff_headcount INTEGER,
  non_teaching_staff_fte REAL,
  total_enrolments INTEGER,
  boys_count INTEGER,
  girls_count INTEGER,
  boys_percentage REAL,
  girls_percentage REAL,
  gender_balance_score REAL,
  indigenous_students_value REAL,
  indigenous_students_value_type TEXT,
  indigenous_students_percentage REAL,
  lbote_value REAL,
  lbote_value_type TEXT,
  lbote_percentage REAL,
  student_to_teacher_ratio REAL,
  icsea_difference_from_baseline INTEGER,
  source_name TEXT,
  source_url TEXT,
  source_file_name TEXT,
  imported_at TEXT,
  last_updated TEXT,
  is_active INTEGER NOT NULL DEFAULT 0,
  version_number INTEGER NOT NULL DEFAULT 1,
  confidence_level TEXT,
  notes TEXT,
  source_school_name TEXT,
  source_state TEXT,
  source_suburb TEXT,
  source_postcode TEXT,
  source_sector TEXT,
  match_method TEXT,
  match_confidence REAL,
  validation_status TEXT NOT NULL DEFAULT 'Pending',
  validation_error TEXT
);
CREATE TABLE IF NOT EXISTS school_external_ids (
  id INTEGER PRIMARY KEY,
  school_id INTEGER NOT NULL REFERENCES schools(id) ON DELETE CASCADE,
  source_name TEXT NOT NULL,
  external_school_id TEXT NOT NULL,
  external_school_name TEXT,
  state TEXT,
  suburb TEXT,
  postcode TEXT,
  sector TEXT,
  match_method TEXT,
  match_confidence REAL,
  verified_by_admin INTEGER NOT NULL DEFAULT 0,
  verified_at TEXT,
  notes TEXT,
  UNIQUE(source_name, external_school_id)
);
CREATE TABLE IF NOT EXISTS staging_crime_statistics (
  id INTEGER PRIMARY KEY,
  job_id INTEGER REFERENCES data_update_jobs(id) ON DELETE CASCADE,
  state TEXT NOT NULL,
  area_type TEXT NOT NULL,
  area_name TEXT NOT NULL,
  suburb TEXT,
  postcode TEXT,
  lga_name TEXT,
  offence_category TEXT,
  offence_subcategory TEXT,
  offence_count INTEGER,
  offence_rate_per_100k REAL,
  period_start TEXT,
  period_end TEXT,
  source TEXT,
  source_period TEXT,
  last_updated TEXT NOT NULL,
  validation_status TEXT NOT NULL DEFAULT 'Pending',
  validation_error TEXT
);
CREATE TABLE IF NOT EXISTS school_documents (
  id INTEGER PRIMARY KEY, school_id INTEGER NOT NULL REFERENCES schools(id) ON DELETE CASCADE,
  document_type TEXT NOT NULL, original_filename TEXT NOT NULL, stored_filename TEXT NOT NULL,
  file_path TEXT NOT NULL, file_size INTEGER NOT NULL DEFAULT 0, source_url TEXT,
  notes TEXT, uploaded_by INTEGER REFERENCES users(id), uploaded_at TEXT NOT NULL,
  updated_by INTEGER REFERENCES users(id), updated_at TEXT
);
CREATE TABLE IF NOT EXISTS school_features (
  id INTEGER PRIMARY KEY, school_id INTEGER NOT NULL REFERENCES schools(id) ON DELETE CASCADE,
  feature_category TEXT, feature_name TEXT NOT NULL, feature_description TEXT,
  source_type TEXT NOT NULL DEFAULT 'Manual', source_url TEXT, source_file TEXT,
  confidence_score REAL, review_status TEXT NOT NULL DEFAULT 'Approved',
  approved_by INTEGER REFERENCES users(id), approved_at TEXT, merged_into_feature_id INTEGER,
  notes TEXT, created_by INTEGER REFERENCES users(id), created_at TEXT NOT NULL,
  updated_by INTEGER REFERENCES users(id), updated_at TEXT
);
CREATE TABLE IF NOT EXISTS school_source_tracking (
  id INTEGER PRIMARY KEY, school_id INTEGER NOT NULL REFERENCES schools(id) ON DELETE CASCADE,
  section_key TEXT NOT NULL, field_name TEXT, source_url TEXT, source_file TEXT,
  extracted_at TEXT, extracted_by INTEGER REFERENCES users(id), confidence_score REAL,
  reviewed_by INTEGER REFERENCES users(id), reviewed_at TEXT,
  last_updated_by INTEGER REFERENCES users(id), last_updated_at TEXT NOT NULL,
  notes TEXT
);
CREATE TABLE IF NOT EXISTS school_trusted_sources (
  id INTEGER PRIMARY KEY, school_id INTEGER NOT NULL REFERENCES schools(id) ON DELETE CASCADE,
  source_url TEXT NOT NULL, source_type TEXT NOT NULL DEFAULT 'Official Website',
  source_notes TEXT, added_by INTEGER REFERENCES users(id), added_at TEXT NOT NULL,
  is_active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS school_data_sources (
  id INTEGER PRIMARY KEY, school_id INTEGER NOT NULL REFERENCES schools(id) ON DELETE CASCADE,
  source_name TEXT NOT NULL, source_type TEXT NOT NULL,
  source_url TEXT NOT NULL, source_status TEXT NOT NULL DEFAULT 'Active',
  notes TEXT, added_by INTEGER REFERENCES users(id), added_at TEXT NOT NULL,
  updated_at TEXT
);
CREATE TABLE IF NOT EXISTS school_myschool_profiles (
  id INTEGER PRIMARY KEY, school_id INTEGER NOT NULL REFERENCES schools(id) ON DELETE CASCADE,
  myschool_url TEXT NOT NULL, myschool_school_id TEXT,
  source_name TEXT NOT NULL DEFAULT 'My School / ACARA',
  source_type TEXT NOT NULL DEFAULT 'official_reference',
  fetched_at TEXT, fetched_by INTEGER REFERENCES users(id),
  fetch_status TEXT NOT NULL DEFAULT 'Not Started',
  review_status TEXT NOT NULL DEFAULT 'Needs Review',
  reviewed_by INTEGER REFERENCES users(id), reviewed_at TEXT,
  school_name_from_myschool TEXT, sector TEXT, school_type TEXT,
  address TEXT, suburb TEXT, state TEXT, postcode TEXT, school_website TEXT,
  year_range TEXT, gender_type TEXT, campus_information TEXT,
  total_enrolment TEXT, boys_count TEXT, girls_count TEXT, fte_students TEXT,
  teaching_staff_count TEXT, fte_teaching_staff TEXT,
  non_teaching_staff_count TEXT, fte_non_teaching_staff TEXT,
  icsea_value TEXT, icsea_percentile TEXT, sea_indicators_json TEXT,
  lbote_percentage TEXT, indigenous_student_percentage TEXT,
  attendance_percentage TEXT, background_indicators_json TEXT,
  data_year TEXT, academic_indicators_json TEXT,
  raw_extract_json TEXT, manual_paste TEXT, error_message TEXT,
  notes TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  updated_by INTEGER REFERENCES users(id)
);
CREATE TABLE IF NOT EXISTS school_ranking_imports (
  id INTEGER PRIMARY KEY,
  source_name TEXT NOT NULL DEFAULT 'Better Education',
  source_type TEXT NOT NULL DEFAULT 'third_party_ranking',
  original_filename TEXT NOT NULL,
  stored_file_path TEXT NOT NULL,
  source_url TEXT,
  ranking_system TEXT,
  state TEXT,
  data_year TEXT,
  total_rows INTEGER NOT NULL DEFAULT 0,
  matched_rows INTEGER NOT NULL DEFAULT 0,
  possible_rows INTEGER NOT NULL DEFAULT 0,
  unmatched_rows INTEGER NOT NULL DEFAULT 0,
  conflict_rows INTEGER NOT NULL DEFAULT 0,
  failed_rows INTEGER NOT NULL DEFAULT 0,
  confirmed_rows INTEGER NOT NULL DEFAULT 0,
  skipped_rows INTEGER NOT NULL DEFAULT 0,
  import_status TEXT NOT NULL DEFAULT 'Uploaded',
  review_status TEXT NOT NULL DEFAULT 'Needs Review',
  report_json TEXT,
  report_md_path TEXT,
  report_json_path TEXT,
  imported_by INTEGER REFERENCES users(id),
  imported_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  notes TEXT
);
CREATE TABLE IF NOT EXISTS school_ranking_import_rows (
  id INTEGER PRIMARY KEY,
  import_id INTEGER NOT NULL REFERENCES school_ranking_imports(id) ON DELETE CASCADE,
  row_number INTEGER NOT NULL,
  school_name_raw TEXT NOT NULL,
  suggested_school_id INTEGER REFERENCES schools(id),
  suggested_school_name TEXT,
  confidence_score REAL,
  match_status TEXT NOT NULL DEFAULT 'Unmatched',
  ranking_system TEXT,
  state TEXT,
  data_year TEXT,
  rank TEXT,
  score TEXT,
  median_score TEXT,
  top_scores_percentage TEXT,
  percentile TEXT,
  result_metric_name TEXT,
  result_metric_value TEXT,
  raw_row_json TEXT,
  review_status TEXT NOT NULL DEFAULT 'Needs Review',
  reviewed_by INTEGER REFERENCES users(id),
  reviewed_at TEXT,
  notes TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS bettereducation_fetch_jobs (
  id INTEGER PRIMARY KEY,
  data_update_job_id INTEGER REFERENCES data_update_jobs(id) ON DELETE SET NULL,
  state TEXT NOT NULL DEFAULT 'VIC',
  source_name TEXT NOT NULL DEFAULT 'Better Education',
  year_start INTEGER NOT NULL DEFAULT 2016,
  year_end INTEGER NOT NULL DEFAULT 2025,
  school_scope TEXT NOT NULL DEFAULT 'missing',
  status TEXT NOT NULL DEFAULT 'Queued',
  total_schools INTEGER NOT NULL DEFAULT 0,
  processed_schools INTEGER NOT NULL DEFAULT 0,
  success_count INTEGER NOT NULL DEFAULT 0,
  failed_count INTEGER NOT NULL DEFAULT 0,
  review_required_count INTEGER NOT NULL DEFAULT 0,
  started_by INTEGER REFERENCES users(id),
  started_at TEXT,
  completed_at TEXT,
  stop_requested INTEGER NOT NULL DEFAULT 0,
  settings_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS bettereducation_fetch_queue (
  id INTEGER PRIMARY KEY,
  fetch_job_id INTEGER NOT NULL REFERENCES bettereducation_fetch_jobs(id) ON DELETE CASCADE,
  data_update_job_id INTEGER REFERENCES data_update_jobs(id) ON DELETE SET NULL,
  school_id INTEGER REFERENCES schools(id) ON DELETE SET NULL,
  crm_school_name TEXT NOT NULL,
  suburb TEXT,
  state TEXT NOT NULL DEFAULT 'VIC',
  sector TEXT,
  sector_key TEXT,
  fetch_status TEXT NOT NULL DEFAULT 'pending',
  retry_count INTEGER NOT NULL DEFAULT 0,
  last_attempt_at TEXT,
  error_message TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS bettereducation_fetch_log (
  id INTEGER PRIMARY KEY,
  data_update_job_id INTEGER REFERENCES data_update_jobs(id) ON DELETE SET NULL,
  fetch_job_id INTEGER REFERENCES bettereducation_fetch_jobs(id) ON DELETE CASCADE,
  queue_id INTEGER REFERENCES bettereducation_fetch_queue(id) ON DELETE SET NULL,
  school_id INTEGER REFERENCES schools(id) ON DELETE SET NULL,
  crm_school_name TEXT,
  source_school_name TEXT,
  state TEXT NOT NULL DEFAULT 'VIC',
  started_at TEXT,
  completed_at TEXT,
  fetch_status TEXT NOT NULL DEFAULT 'pending',
  years_found TEXT,
  records_found INTEGER NOT NULL DEFAULT 0,
  records_saved_to_staging INTEGER NOT NULL DEFAULT 0,
  match_status TEXT,
  error_type TEXT,
  error_message TEXT,
  screenshot_path TEXT,
  html_snapshot_path TEXT,
  retry_count INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS bettereducation_school_mapping (
  id INTEGER PRIMARY KEY,
  source_name TEXT NOT NULL DEFAULT 'Better Education',
  source_school_name TEXT NOT NULL,
  crm_school_id INTEGER NOT NULL REFERENCES schools(id) ON DELETE CASCADE,
  crm_school_name TEXT NOT NULL,
  state TEXT NOT NULL DEFAULT 'VIC',
  suburb TEXT,
  confidence_score REAL,
  confirmed_by INTEGER REFERENCES users(id),
  confirmed_at TEXT,
  active INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS academic_results_import_staging (
  id INTEGER PRIMARY KEY,
  import_batch_id INTEGER REFERENCES school_ranking_imports(id) ON DELETE CASCADE,
  data_update_job_id INTEGER REFERENCES data_update_jobs(id) ON DELETE SET NULL,
  state TEXT NOT NULL DEFAULT 'VIC',
  source_type TEXT NOT NULL DEFAULT 'third_party',
  source_name TEXT NOT NULL DEFAULT 'Better Education',
  source_url TEXT,
  crm_school_id INTEGER REFERENCES schools(id) ON DELETE SET NULL,
  crm_school_name TEXT,
  source_school_name TEXT NOT NULL,
  data_year TEXT NOT NULL,
  bettereducation_rank TEXT,
  bettereducation_percentile TEXT,
  median_vce_score TEXT,
  percentage_study_scores_40_plus TEXT,
  students TEXT,
  exams_or_entries TEXT,
  raw_row_json TEXT NOT NULL DEFAULT '{}',
  fetched_at TEXT,
  match_status TEXT NOT NULL DEFAULT 'Unmatched',
  confidence_score REAL,
  review_status TEXT NOT NULL DEFAULT 'Needs Review',
  reviewed_by INTEGER REFERENCES users(id),
  reviewed_at TEXT,
  error_message TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS school_data_import_batches (
  id INTEGER PRIMARY KEY,
  source_name TEXT NOT NULL DEFAULT 'School Data Import',
  original_filename TEXT NOT NULL,
  stored_file_path TEXT NOT NULL,
  source_state TEXT NOT NULL,
  file_type TEXT,
  total_rows INTEGER NOT NULL DEFAULT 0,
  importable_rows INTEGER NOT NULL DEFAULT 0,
  existing_rows INTEGER NOT NULL DEFAULT 0,
  new_rows INTEGER NOT NULL DEFAULT 0,
  review_required_rows INTEGER NOT NULL DEFAULT 0,
  error_rows INTEGER NOT NULL DEFAULT 0,
  imported_rows INTEGER NOT NULL DEFAULT 0,
  skipped_rows INTEGER NOT NULL DEFAULT 0,
  import_status TEXT NOT NULL DEFAULT 'Uploaded',
  review_status TEXT NOT NULL DEFAULT 'Needs Review',
  report_json TEXT,
  imported_by INTEGER REFERENCES users(id),
  imported_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  notes TEXT
);
CREATE TABLE IF NOT EXISTS school_data_import_rows (
  id INTEGER PRIMARY KEY,
  batch_id INTEGER NOT NULL REFERENCES school_data_import_batches(id) ON DELETE CASCADE,
  row_number INTEGER NOT NULL,
  source_state TEXT NOT NULL,
  school_name_raw TEXT,
  school_name TEXT,
  school_name_normalized TEXT,
  campus_locations_raw TEXT,
  region_raw TEXT,
  website_raw TEXT,
  year_levels_raw TEXT,
  year_level_min TEXT,
  year_level_max TEXT,
  year_levels_list_json TEXT NOT NULL DEFAULT '[]',
  suggested_school_id INTEGER REFERENCES schools(id),
  suggested_school_name TEXT,
  confidence_score REAL,
  match_status TEXT NOT NULL DEFAULT 'Unmatched',
  import_status TEXT NOT NULL DEFAULT 'Needs Review',
  review_status TEXT NOT NULL DEFAULT 'Needs Review',
  review_required INTEGER NOT NULL DEFAULT 1,
  error_reason TEXT,
  raw_row_json TEXT,
  edited_row_json TEXT,
  final_school_id INTEGER REFERENCES schools(id),
  reviewed_by INTEGER REFERENCES users(id),
  reviewed_at TEXT,
  imported_by INTEGER REFERENCES users(id),
  imported_at TEXT,
  notes TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS school_academic_rankings (
  id INTEGER PRIMARY KEY,
  school_id INTEGER REFERENCES schools(id) ON DELETE SET NULL,
  school_name_raw TEXT NOT NULL,
  ranking_system TEXT,
  state TEXT,
  data_year TEXT,
  rank TEXT,
  score TEXT,
  median_score TEXT,
  top_scores_percentage TEXT,
  percentile TEXT,
  result_metric_name TEXT,
  result_metric_value TEXT,
  source_name TEXT NOT NULL DEFAULT 'Better Education',
  source_type TEXT NOT NULL DEFAULT 'third_party_ranking',
  source_url TEXT,
  source_file_path TEXT,
  import_id INTEGER REFERENCES school_ranking_imports(id) ON DELETE SET NULL,
  import_row_id INTEGER REFERENCES school_ranking_import_rows(id) ON DELETE SET NULL,
  imported_at TEXT NOT NULL,
  imported_by INTEGER REFERENCES users(id),
  review_status TEXT NOT NULL DEFAULT 'Needs Review',
  reviewed_by INTEGER REFERENCES users(id),
  reviewed_at TEXT,
  notes TEXT,
  UNIQUE(import_row_id)
);
CREATE TABLE IF NOT EXISTS school_third_party_profiles (
  id INTEGER PRIMARY KEY, school_id INTEGER NOT NULL REFERENCES schools(id) ON DELETE CASCADE,
  source_name TEXT NOT NULL, source_type TEXT NOT NULL DEFAULT 'third_party',
  source_url TEXT NOT NULL, profile_url TEXT, about_url TEXT, fees_url TEXT, naplan_url TEXT, location_url TEXT,
  fetched_at TEXT, review_status TEXT NOT NULL DEFAULT 'Needs Review',
  reviewed_by INTEGER REFERENCES users(id), reviewed_at TEXT,
  school_name TEXT, address TEXT, suburb TEXT, postcode TEXT, state TEXT, website TEXT,
  school_type TEXT, sector TEXT, gender TEXT, year_levels TEXT, religion TEXT, contact_details TEXT,
  location_page_url TEXT, third_party_description TEXT, third_party_summary TEXT, strengths_json TEXT,
  raw_extract_json TEXT, manual_paste TEXT, fetch_status TEXT NOT NULL DEFAULT 'not_started',
  error_message TEXT, notes TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  updated_by INTEGER REFERENCES users(id)
);
CREATE TABLE IF NOT EXISTS school_fees (
  id INTEGER PRIMARY KEY, school_id INTEGER NOT NULL REFERENCES schools(id) ON DELETE CASCADE,
  source_name TEXT NOT NULL, source_type TEXT NOT NULL DEFAULT 'third_party', source_url TEXT,
  fetched_at TEXT, review_status TEXT NOT NULL DEFAULT 'Needs Review',
  reviewed_by INTEGER REFERENCES users(id), reviewed_at TEXT,
  fee_type TEXT, year_level TEXT, amount TEXT, currency TEXT NOT NULL DEFAULT 'AUD',
  fee_year TEXT, raw_text TEXT, notes TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  updated_by INTEGER REFERENCES users(id)
);
CREATE TABLE IF NOT EXISTS school_international_fee_records (
  id INTEGER PRIMARY KEY,
  data_update_job_id INTEGER REFERENCES data_update_jobs(id) ON DELETE SET NULL,
  school_id INTEGER NOT NULL REFERENCES schools(id) ON DELETE CASCADE,
  school_name TEXT NOT NULL,
  state TEXT NOT NULL DEFAULT 'VIC',
  fee_year TEXT,
  source_year TEXT,
  currency TEXT NOT NULL DEFAULT 'AUD',
  source_url TEXT,
  file_url TEXT,
  source_type TEXT NOT NULL DEFAULT 'html',
  document_title TEXT,
  file_name TEXT,
  file_hash TEXT,
  last_modified TEXT,
  downloaded_at TEXT,
  extraction_method TEXT NOT NULL DEFAULT 'rule',
  confidence_score REAL NOT NULL DEFAULT 0,
  verification_status TEXT NOT NULL DEFAULT 'pending',
  verification_notes TEXT,
  reviewed_by INTEGER REFERENCES users(id),
  reviewed_at TEXT,
  is_current INTEGER NOT NULL DEFAULT 0,
  notes TEXT,
  raw_text TEXT,
  raw_table_data TEXT NOT NULL DEFAULT '[]',
  screenshot_path TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  updated_by INTEGER REFERENCES users(id)
);
CREATE TABLE IF NOT EXISTS school_international_fee_items (
  id INTEGER PRIMARY KEY,
  fee_record_id INTEGER NOT NULL REFERENCES school_international_fee_records(id) ON DELETE CASCADE,
  fee_category TEXT NOT NULL,
  year_level TEXT NOT NULL DEFAULT 'Unknown',
  campus TEXT,
  amount REAL,
  amount_text TEXT,
  amount_period TEXT NOT NULL DEFAULT 'annual',
  currency TEXT NOT NULL DEFAULT 'AUD',
  is_compulsory INTEGER NOT NULL DEFAULT 1,
  raw_text TEXT,
  page_number TEXT,
  table_reference TEXT,
  confidence_score REAL NOT NULL DEFAULT 0,
  notes TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS school_international_fee_fetch_sources (
  id INTEGER PRIMARY KEY,
  data_update_job_id INTEGER REFERENCES data_update_jobs(id) ON DELETE CASCADE,
  school_id INTEGER NOT NULL REFERENCES schools(id) ON DELETE CASCADE,
  source_url TEXT NOT NULL,
  file_url TEXT,
  source_type TEXT NOT NULL DEFAULT 'html',
  document_title TEXT,
  detected_year TEXT,
  priority_score REAL NOT NULL DEFAULT 0,
  fetch_status TEXT NOT NULL DEFAULT 'candidate',
  error_message TEXT,
  file_hash TEXT,
  downloaded_at TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS state_fee_schedules (
  id INTEGER PRIMARY KEY, state TEXT NOT NULL, sector TEXT NOT NULL,
  schedule_name TEXT NOT NULL, source_name TEXT, source_url TEXT,
  effective_year INTEGER NOT NULL, effective_from_date TEXT, effective_to_date TEXT,
  is_active INTEGER NOT NULL DEFAULT 0, notes TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  UNIQUE(state,sector,effective_year,schedule_name)
);
CREATE TABLE IF NOT EXISTS state_fee_items (
  id INTEGER PRIMARY KEY, schedule_id INTEGER NOT NULL REFERENCES state_fee_schedules(id) ON DELETE CASCADE,
  category TEXT NOT NULL, fee_name TEXT NOT NULL, school_level TEXT,
  year_level_from TEXT, year_level_to TEXT,
  amount REAL, amount_min REAL, amount_max REAL, currency TEXT NOT NULL DEFAULT 'AUD',
  billing_frequency TEXT, is_mandatory INTEGER NOT NULL DEFAULT 0,
  notes TEXT, sort_order INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS school_academic_indicators (
  id INTEGER PRIMARY KEY, school_id INTEGER NOT NULL REFERENCES schools(id) ON DELETE CASCADE,
  source_name TEXT NOT NULL, source_type TEXT NOT NULL DEFAULT 'third_party', source_url TEXT,
  fetched_at TEXT, review_status TEXT NOT NULL DEFAULT 'Needs Review',
  reviewed_by INTEGER REFERENCES users(id), reviewed_at TEXT,
  indicator_name TEXT NOT NULL, year_level TEXT, data_year TEXT, indicator_value TEXT,
  comparison_notes TEXT, raw_text TEXT, notes TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  updated_by INTEGER REFERENCES users(id)
);
CREATE TABLE IF NOT EXISTS school_comparison_sources (
  id INTEGER PRIMARY KEY, school_id INTEGER NOT NULL REFERENCES schools(id) ON DELETE CASCADE,
  source_name TEXT NOT NULL, source_type TEXT NOT NULL DEFAULT 'third_party', source_url TEXT,
  fetched_at TEXT, review_status TEXT NOT NULL DEFAULT 'Needs Review',
  nearby_school_name TEXT, suburb TEXT, postcode TEXT, state TEXT, school_type TEXT,
  comparison_notes TEXT, raw_text TEXT, notes TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  updated_by INTEGER REFERENCES users(id)
);
CREATE TABLE IF NOT EXISTS school_data_conflicts (
  id INTEGER PRIMARY KEY, school_id INTEGER NOT NULL REFERENCES schools(id) ON DELETE CASCADE,
  source_name TEXT NOT NULL, source_url TEXT, field_name TEXT NOT NULL,
  official_value TEXT, third_party_value TEXT, conflict_status TEXT NOT NULL DEFAULT 'Data Conflict / Needs Review',
  review_status TEXT NOT NULL DEFAULT 'Needs Review', reviewed_by INTEGER REFERENCES users(id),
  reviewed_at TEXT, notes TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS school_collection_jobs (
  id INTEGER PRIMARY KEY, school_id INTEGER REFERENCES schools(id) ON DELETE SET NULL,
  selected_fields_json TEXT NOT NULL DEFAULT '[]',
  source_ids_json TEXT NOT NULL DEFAULT '[]',
  status TEXT NOT NULL DEFAULT 'Draft', request_notes TEXT,
  created_by INTEGER REFERENCES users(id), created_at TEXT NOT NULL,
  updated_at TEXT
);
CREATE TABLE IF NOT EXISTS school_collection_drafts (
  id INTEGER PRIMARY KEY, job_id INTEGER REFERENCES school_collection_jobs(id) ON DELETE CASCADE,
  school_id INTEGER REFERENCES schools(id) ON DELETE SET NULL,
  field_name TEXT NOT NULL, extracted_value TEXT, editable_value TEXT,
  source_url TEXT, collected_at TEXT, confidence_score REAL,
  review_status TEXT NOT NULL DEFAULT 'Pending',
  confirmed_by INTEGER REFERENCES users(id), confirmed_at TEXT,
  notes TEXT, created_at TEXT NOT NULL, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS backup_records (
  id INTEGER PRIMARY KEY, backup_id TEXT UNIQUE NOT NULL, file_name TEXT UNIQUE NOT NULL,
  file_path TEXT NOT NULL, file_size INTEGER NOT NULL DEFAULT 0, backup_notes TEXT,
  created_by INTEGER, created_by_username TEXT, created_at TEXT NOT NULL,
  app_version TEXT, database_type TEXT NOT NULL DEFAULT 'sqlite',
  manifest_json TEXT, deleted_at TEXT
);
CREATE TABLE IF NOT EXISTS contract_refund_templates (
  id INTEGER PRIMARY KEY, template_name TEXT UNIQUE NOT NULL, refund_policy_type TEXT NOT NULL,
  refund_policy_text TEXT NOT NULL, refusal_deduction_currency TEXT,
  refusal_deduction_amount TEXT, refusal_refund_currency TEXT, refusal_refund_amount TEXT,
  refund_notes TEXT, is_active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS template_records (
  id INTEGER PRIMARY KEY, template_key TEXT UNIQUE NOT NULL,
  template_name TEXT NOT NULL, template_category TEXT NOT NULL,
  contract_type TEXT NOT NULL, description TEXT, file_path TEXT NOT NULL,
  original_name TEXT, version INTEGER NOT NULL DEFAULT 1,
  is_active INTEGER NOT NULL DEFAULT 0, notes TEXT,
  created_by INTEGER REFERENCES users(id), created_at TEXT NOT NULL,
  updated_by INTEGER REFERENCES users(id), updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS contracts (
  id INTEGER PRIMARY KEY, student_id TEXT REFERENCES students(student_id),
  contract_uid TEXT UNIQUE, is_standalone_contract INTEGER NOT NULL DEFAULT 0,
  standalone_data_json TEXT,
  contract_category TEXT NOT NULL, contract_type TEXT NOT NULL, contract_title TEXT NOT NULL,
  contract_status TEXT NOT NULL DEFAULT 'Draft', contract_date TEXT, service_type TEXT,
  service_scope_json TEXT NOT NULL DEFAULT '[]', other_service_scope_text TEXT,
  service_fee_currency TEXT, consulting_and_visa_service_fee TEXT,
  discount_amount TEXT, final_fee_amount TEXT, payment_due_date TEXT,
  refund_policy_type TEXT, refund_policy_text TEXT, refusal_deduction_currency TEXT,
  refusal_deduction_amount TEXT, refusal_refund_currency TEXT, refusal_refund_amount TEXT,
  refund_notes TEXT, special_terms TEXT, parent_signer_name TEXT,
  parent_phone TEXT, parent_email TEXT, parent_address TEXT, notes TEXT, contract_file_path TEXT,
  signed_file_path TEXT, final_signed_file_path TEXT, template_version TEXT, generated_by INTEGER REFERENCES users(id),
  generated_at TEXT, signed_at TEXT, fully_executed_at TEXT,
  deleted_at TEXT, deleted_by INTEGER REFERENCES users(id), deleted_reason_code TEXT, deleted_reason TEXT,
  voided_at TEXT, voided_by INTEGER REFERENCES users(id), void_reason_code TEXT, void_reason TEXT,
  superseded_by_contract_id INTEGER REFERENCES contracts(id),
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS contract_versions (
  id INTEGER PRIMARY KEY, contract_id INTEGER NOT NULL REFERENCES contracts(id) ON DELETE CASCADE,
  student_id TEXT REFERENCES students(student_id), version_number INTEGER NOT NULL,
  version_label TEXT, file_path_docx TEXT, file_path_pdf TEXT, status TEXT NOT NULL DEFAULT 'Generated',
  revision_reason TEXT, parent_request_text TEXT, change_summary TEXT,
  generated_by INTEGER REFERENCES users(id), generated_at TEXT,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS contract_signing_links (
  id INTEGER PRIMARY KEY, contract_id INTEGER NOT NULL REFERENCES contracts(id) ON DELETE CASCADE,
  student_id TEXT REFERENCES students(student_id),
  token_hash TEXT NOT NULL UNIQUE, token_last_4 TEXT NOT NULL, signing_url TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'active', expires_at TEXT NOT NULL,
  created_by_user_id INTEGER REFERENCES users(id), created_by_name TEXT, created_at TEXT NOT NULL, updated_at TEXT,
  first_viewed_at TEXT, last_viewed_at TEXT, viewed_count INTEGER NOT NULL DEFAULT 0,
  invalidated_at TEXT, invalidated_reason TEXT, signed_at TEXT,
  signed_signature_id INTEGER,
  bound_wechat_openid TEXT, bound_wechat_unionid TEXT, bound_wechat_nickname TEXT,
  bound_wechat_avatar_url TEXT, bound_wechat_at TEXT, last_login_at TEXT
);
CREATE TABLE IF NOT EXISTS contract_signing_logs (
  id INTEGER PRIMARY KEY, contract_id INTEGER NOT NULL REFERENCES contracts(id) ON DELETE CASCADE,
  link_id INTEGER REFERENCES contract_signing_links(id) ON DELETE SET NULL,
  event_at TEXT NOT NULL, event_type TEXT NOT NULL, ip_address TEXT, user_agent TEXT, device_type TEXT,
  wechat_openid TEXT, wechat_unionid TEXT, wechat_nickname TEXT, metadata_json TEXT
);
CREATE TABLE IF NOT EXISTS contract_signatures (
  id INTEGER PRIMARY KEY, contract_id INTEGER NOT NULL REFERENCES contracts(id) ON DELETE CASCADE,
  student_id TEXT REFERENCES students(student_id), link_id INTEGER REFERENCES contract_signing_links(id) ON DELETE SET NULL,
  signed_at TEXT NOT NULL, signed_ip TEXT, user_agent TEXT,
  wechat_openid TEXT, wechat_unionid TEXT, wechat_nickname TEXT, wechat_avatar_url TEXT,
  signature_image_path TEXT NOT NULL, signed_pdf_path TEXT, confirmation_pdf_path TEXT,
  confirmation_statement TEXT, status TEXT NOT NULL DEFAULT 'signed',
  archive_status TEXT NOT NULL DEFAULT 'pending', archive_error TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS contract_sections (
  id INTEGER PRIMARY KEY, contract_id INTEGER NOT NULL REFERENCES contracts(id) ON DELETE CASCADE,
  version_id INTEGER REFERENCES contract_versions(id) ON DELETE CASCADE,
  section_key TEXT NOT NULL, section_title TEXT NOT NULL, section_content TEXT,
  source_type TEXT, updated_by INTEGER REFERENCES users(id),
  is_locked INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS contract_revision_requests (
  id INTEGER PRIMARY KEY, contract_id INTEGER NOT NULL REFERENCES contracts(id) ON DELETE CASCADE,
  student_id TEXT REFERENCES students(student_id), base_version_id INTEGER REFERENCES contract_versions(id),
  requested_by_name TEXT, revision_instruction TEXT, parent_request_text TEXT, request_channel TEXT,
  request_received_date TEXT, revision_reason TEXT NOT NULL,
  risk_warning TEXT,
  status TEXT NOT NULL DEFAULT 'Generated',
  prepared_by INTEGER REFERENCES users(id), prepared_at TEXT,
  notes TEXT, created_at TEXT, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS contract_revision_changes (
  id INTEGER PRIMARY KEY, revision_request_id INTEGER NOT NULL REFERENCES contract_revision_requests(id) ON DELETE CASCADE,
  contract_id INTEGER REFERENCES contracts(id) ON DELETE CASCADE,
  version_id INTEGER REFERENCES contract_versions(id) ON DELETE CASCADE,
  section_key TEXT NOT NULL, section_title TEXT,
  original_content TEXT, proposed_content TEXT, final_content TEXT,
  old_content TEXT, new_content TEXT, change_reason TEXT, changed_by INTEGER REFERENCES users(id), changed_at TEXT,
  change_summary TEXT, risk_level TEXT, risk_warning TEXT,
  status TEXT NOT NULL DEFAULT 'Generated', created_at TEXT NOT NULL, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS contract_clause_library (
  id INTEGER PRIMARY KEY, clause_category TEXT NOT NULL, clause_title TEXT NOT NULL,
  clause_content TEXT NOT NULL, is_active INTEGER NOT NULL DEFAULT 1,
  is_locked INTEGER NOT NULL DEFAULT 0, created_by INTEGER REFERENCES users(id),
  created_at TEXT NOT NULL, updated_by INTEGER REFERENCES users(id), updated_at TEXT
);
CREATE TABLE IF NOT EXISTS contract_manual_placements (
  id INTEGER PRIMARY KEY, contract_id INTEGER NOT NULL REFERENCES contracts(id),
  student_id TEXT REFERENCES students(student_id),
  source_file_path TEXT NOT NULL, output_file_path TEXT NOT NULL,
  page_number INTEGER NOT NULL, page_rotation INTEGER NOT NULL DEFAULT 0,
  stamp_x REAL NOT NULL, stamp_y REAL NOT NULL, stamp_width REAL NOT NULL, stamp_height REAL NOT NULL,
  signature_x REAL NOT NULL, signature_y REAL NOT NULL, signature_width REAL NOT NULL, signature_height REAL NOT NULL,
  date_x REAL NOT NULL, date_y REAL NOT NULL, date_font_size REAL NOT NULL,
  created_by INTEGER REFERENCES users(id), created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_students_created ON students(created_date DESC);
CREATE INDEX IF NOT EXISTS idx_docs_student ON student_documents(student_id, folder_category, document_type);
CREATE INDEX IF NOT EXISTS idx_student_documents_student_active_uploaded ON student_documents(student_id, is_active, uploaded_at DESC);
CREATE INDEX IF NOT EXISTS idx_student_documents_file_active ON student_documents(file_path, is_active, id DESC);
CREATE INDEX IF NOT EXISTS idx_upload_sessions_student ON upload_sessions(student_id, module_key, uploaded_at DESC);
CREATE INDEX IF NOT EXISTS idx_upload_review_items_session ON upload_review_items(upload_session_id, status);
CREATE INDEX IF NOT EXISTS idx_student_change_logs_student ON student_change_logs(student_id, changed_at DESC);
CREATE INDEX IF NOT EXISTS idx_audit_created ON audit_logs(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_parent_form_tokens_student_created ON parent_form_tokens(student_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_parent_form_tokens_created ON parent_form_tokens(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_offers_student_created ON offers(student_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_coe_oshc_student_created ON coe_oshc(student_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_visas_student_created ON visas(student_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_payments_student_created ON payments(student_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_reminders_student_time ON reminders(student_id, reminder_datetime, due_datetime, due_date);
CREATE INDEX IF NOT EXISTS idx_file_action_markers_file ON file_action_markers(file_path, marker_status);
CREATE INDEX IF NOT EXISTS idx_backups_created ON backup_records(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_schools_city_sector ON schools(city_id, sector_key, school_name);
CREATE INDEX IF NOT EXISTS idx_school_suburbs_lookup ON school_suburbs(state, postcode, normalized_suburb_name);
CREATE INDEX IF NOT EXISTS idx_school_geocode_cache_lookup ON school_geocode_cache(normalized_query, state, input_type, geocode_status);
CREATE INDEX IF NOT EXISTS idx_school_suburb_data_suburb ON school_suburb_data(suburb_id, reviewed_status, data_year);
CREATE INDEX IF NOT EXISTS idx_school_suburb_sources_enabled ON school_suburb_data_sources(enabled, source_type);
CREATE INDEX IF NOT EXISTS idx_school_area_profiles_school_year ON school_area_profiles(school_id, data_year, last_checked_at);
CREATE INDEX IF NOT EXISTS idx_school_area_profiles_geo ON school_area_profiles(sa2_code, lga_code, data_year);
CREATE INDEX IF NOT EXISTS idx_school_area_profile_metrics_profile ON school_area_profile_metrics(profile_id, metric_key);
CREATE INDEX IF NOT EXISTS idx_csa_crime_area_statistics_lookup ON csa_crime_area_statistics(state, data_level, normalized_area_key, data_year_ending);
CREATE INDEX IF NOT EXISTS idx_school_area_crime_profiles_school_year ON school_area_crime_profiles(school_id, data_year_ending, data_level);
CREATE INDEX IF NOT EXISTS idx_rental_market_lookup ON rental_market(state, suburb, property_type, quarter);
CREATE INDEX IF NOT EXISTS idx_official_rental_market_details_lookup ON official_rental_market_details(state, area_level, area_name, property_type, quarter);
CREATE INDEX IF NOT EXISTS idx_property_market_lookup ON property_market(state, suburb, property_type, period);
CREATE INDEX IF NOT EXISTS idx_geo_areas_lookup ON geo_areas(state, area_type, suburb, postcode);
CREATE INDEX IF NOT EXISTS idx_school_radius_area_links_school ON school_radius_area_links(school_id, radius_km);
CREATE INDEX IF NOT EXISTS idx_school_radius_market_summary_school ON school_radius_market_summary(school_id, radius_km);
CREATE INDEX IF NOT EXISTS idx_school_feature_tags_group ON school_feature_tags(tag_group, is_active);
CREATE INDEX IF NOT EXISTS idx_school_documents_school ON school_documents(school_id, uploaded_at DESC);
CREATE INDEX IF NOT EXISTS idx_school_features_school ON school_features(school_id, review_status, feature_category);
CREATE INDEX IF NOT EXISTS idx_school_source_tracking_school ON school_source_tracking(school_id, section_key);
CREATE INDEX IF NOT EXISTS idx_school_trusted_sources_school ON school_trusted_sources(school_id, is_active);
CREATE INDEX IF NOT EXISTS idx_school_data_sources_school ON school_data_sources(school_id, source_name, source_status);
CREATE INDEX IF NOT EXISTS idx_school_myschool_profiles_school ON school_myschool_profiles(school_id, review_status, fetched_at DESC);
CREATE INDEX IF NOT EXISTS idx_school_profile_data_school_year ON school_profile_data(school_id, source_year, is_active);
CREATE INDEX IF NOT EXISTS idx_staging_school_profile_data_job ON staging_school_profile_data(job_id, validation_status, match_confidence);
CREATE INDEX IF NOT EXISTS idx_school_external_ids_lookup ON school_external_ids(source_name, external_school_id);
CREATE INDEX IF NOT EXISTS idx_school_ranking_imports_status ON school_ranking_imports(source_name, import_status, imported_at DESC);
CREATE INDEX IF NOT EXISTS idx_school_ranking_import_rows_import ON school_ranking_import_rows(import_id, match_status, review_status);
CREATE INDEX IF NOT EXISTS idx_school_academic_rankings_school ON school_academic_rankings(school_id, ranking_system, data_year);
CREATE INDEX IF NOT EXISTS idx_school_data_import_batches_status ON school_data_import_batches(source_state, import_status, imported_at DESC);
CREATE INDEX IF NOT EXISTS idx_school_data_import_rows_batch ON school_data_import_rows(batch_id, import_status, review_status);
CREATE INDEX IF NOT EXISTS idx_school_data_import_rows_school ON school_data_import_rows(source_state, school_name_normalized);
CREATE INDEX IF NOT EXISTS idx_school_third_party_profiles_school ON school_third_party_profiles(school_id, source_name, review_status);
CREATE INDEX IF NOT EXISTS idx_school_fees_school ON school_fees(school_id, source_name, review_status);
CREATE INDEX IF NOT EXISTS idx_school_international_fee_records_school ON school_international_fee_records(school_id, fee_year, verification_status);
CREATE INDEX IF NOT EXISTS idx_school_international_fee_records_job ON school_international_fee_records(data_update_job_id, verification_status);
CREATE INDEX IF NOT EXISTS idx_school_international_fee_items_record ON school_international_fee_items(fee_record_id, fee_category, year_level);
CREATE INDEX IF NOT EXISTS idx_school_international_fee_fetch_sources_job ON school_international_fee_fetch_sources(data_update_job_id, school_id);
CREATE INDEX IF NOT EXISTS idx_school_plan_reports_student_app ON school_plan_reports(student_id, application_id, report_version);
CREATE INDEX IF NOT EXISTS idx_school_plan_reports_status ON school_plan_reports(status, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_school_plan_report_viewers_report ON school_plan_report_viewers(report_id, status);
CREATE INDEX IF NOT EXISTS idx_school_plan_report_links_report ON school_plan_report_links(report_id, status);
CREATE INDEX IF NOT EXISTS idx_school_plan_report_links_token_hash ON school_plan_report_links(token_hash);
CREATE INDEX IF NOT EXISTS idx_school_plan_report_events_report ON school_plan_report_events(report_id, event_at DESC);
CREATE INDEX IF NOT EXISTS idx_school_plan_report_view_logs_report ON school_plan_report_view_logs(report_id, viewed_at DESC);
CREATE INDEX IF NOT EXISTS idx_school_plan_report_view_logs_link ON school_plan_report_view_logs(link_id, viewed_at DESC);
CREATE INDEX IF NOT EXISTS idx_school_plan_report_confirmations_report ON school_plan_report_confirmations(report_id, confirmed_at DESC);
CREATE INDEX IF NOT EXISTS idx_school_plan_report_rankings_confirmation ON school_plan_report_school_rankings(confirmation_id, rank_order);
CREATE UNIQUE INDEX IF NOT EXISTS uq_school_plan_current_public_city
ON school_plan_reports(student_id, city_id)
WHERE plan_sector='public' AND is_current=1;
CREATE INDEX IF NOT EXISTS idx_state_fee_schedules_lookup ON state_fee_schedules(state, sector, effective_year, is_active);
CREATE INDEX IF NOT EXISTS idx_state_fee_items_schedule ON state_fee_items(schedule_id, category, sort_order);
CREATE INDEX IF NOT EXISTS idx_school_academic_indicators_school ON school_academic_indicators(school_id, source_name, review_status);
CREATE INDEX IF NOT EXISTS idx_school_comparison_sources_school ON school_comparison_sources(school_id, source_name, review_status);
CREATE INDEX IF NOT EXISTS idx_school_data_conflicts_school ON school_data_conflicts(school_id, review_status);
CREATE INDEX IF NOT EXISTS idx_school_collection_jobs_school ON school_collection_jobs(school_id, status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_school_collection_drafts_job ON school_collection_drafts(job_id, review_status);
CREATE INDEX IF NOT EXISTS idx_template_records_contract ON template_records(contract_type, is_active, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_contracts_student ON contracts(student_id, contract_category, generated_at);
CREATE INDEX IF NOT EXISTS idx_contracts_student_generated ON contracts(student_id, generated_at DESC);
CREATE INDEX IF NOT EXISTS idx_contract_versions_contract ON contract_versions(contract_id, version_number);
CREATE INDEX IF NOT EXISTS idx_contract_sections_contract ON contract_sections(contract_id, version_id, section_key);
CREATE INDEX IF NOT EXISTS idx_contract_revision_requests_contract ON contract_revision_requests(contract_id, status, prepared_at);
CREATE INDEX IF NOT EXISTS idx_contract_revision_changes_request ON contract_revision_changes(revision_request_id, section_key);
CREATE INDEX IF NOT EXISTS idx_contract_manual_placements_contract ON contract_manual_placements(contract_id, created_at DESC);
CREATE TABLE IF NOT EXISTS content_topics (
  id INTEGER PRIMARY KEY, topic_name TEXT NOT NULL, category TEXT, status TEXT NOT NULL DEFAULT 'Active',
  content_year INTEGER, source TEXT, source_type TEXT, source_reference TEXT, tags_json TEXT,
  legacy_system_name TEXT, legacy_table_name TEXT, legacy_record_id INTEGER,
  legacy_created_at TEXT, legacy_updated_at TEXT, imported_at TEXT, imported_by INTEGER,
  import_batch_id TEXT, potential_duplicate INTEGER NOT NULL DEFAULT 0, migration_notes TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS content_scripts (
  id INTEGER PRIMARY KEY, title TEXT NOT NULL, body TEXT DEFAULT '', category TEXT,
  topic_id INTEGER REFERENCES content_topics(id), status TEXT NOT NULL DEFAULT 'Draft',
  review_status TEXT, version_number INTEGER NOT NULL DEFAULT 1, source_type TEXT,
  content_year INTEGER, content_month INTEGER, content_week INTEGER, recorded_at TEXT, published_at TEXT, tags_json TEXT,
  notes TEXT, legacy_system_name TEXT, legacy_table_name TEXT, legacy_record_id INTEGER,
  legacy_created_at TEXT, legacy_updated_at TEXT, imported_at TEXT, imported_by INTEGER,
  import_batch_id TEXT, potential_duplicate INTEGER NOT NULL DEFAULT 0, migration_notes TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS content_script_versions (
  id INTEGER PRIMARY KEY, script_id INTEGER REFERENCES content_scripts(id) ON DELETE CASCADE,
  title TEXT, body TEXT, version_number INTEGER NOT NULL DEFAULT 1, version_note TEXT, legacy_system_name TEXT, legacy_table_name TEXT,
  legacy_record_id INTEGER, legacy_created_at TEXT, legacy_updated_at TEXT,
  imported_at TEXT, imported_by INTEGER, import_batch_id TEXT, potential_duplicate INTEGER NOT NULL DEFAULT 0,
  migration_notes TEXT, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS content_publish_plans (
  id INTEGER PRIMARY KEY, script_id INTEGER REFERENCES content_scripts(id) ON DELETE CASCADE,
  planned_publish_date TEXT, channel TEXT DEFAULT 'Xiaohongshu', status TEXT NOT NULL DEFAULT 'Planned',
  legacy_system_name TEXT, legacy_table_name TEXT, legacy_record_id INTEGER,
  imported_at TEXT, imported_by INTEGER, import_batch_id TEXT, potential_duplicate INTEGER NOT NULL DEFAULT 0,
  migration_notes TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS content_published_posts (
  id INTEGER PRIMARY KEY, script_id INTEGER REFERENCES content_scripts(id) ON DELETE CASCADE,
  published_at TEXT, post_url TEXT, platform TEXT DEFAULT 'Xiaohongshu', status TEXT NOT NULL DEFAULT 'Published',
  legacy_system_name TEXT, legacy_table_name TEXT, legacy_record_id INTEGER,
  imported_at TEXT, imported_by INTEGER, import_batch_id TEXT, potential_duplicate INTEGER NOT NULL DEFAULT 0,
  migration_notes TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS content_performance_metrics (
  id INTEGER PRIMARY KEY, script_id INTEGER REFERENCES content_scripts(id) ON DELETE CASCADE,
  checkpoint TEXT, views INTEGER NOT NULL DEFAULT 0, likes INTEGER NOT NULL DEFAULT 0,
  comments INTEGER NOT NULL DEFAULT 0, measured_at TEXT,
  legacy_system_name TEXT, legacy_table_name TEXT, legacy_record_id INTEGER,
  imported_at TEXT, imported_by INTEGER, import_batch_id TEXT, potential_duplicate INTEGER NOT NULL DEFAULT 0,
  migration_notes TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS content_materials (
  id INTEGER PRIMARY KEY, script_id INTEGER REFERENCES content_scripts(id) ON DELETE SET NULL,
  material_type TEXT, title TEXT, source_path TEXT, source_url TEXT, file_hash TEXT,
  source_modified_at TEXT,
  legacy_system_name TEXT, legacy_table_name TEXT, legacy_record_id INTEGER,
  legacy_created_at TEXT, legacy_updated_at TEXT, imported_at TEXT, imported_by INTEGER,
  import_batch_id TEXT, potential_duplicate INTEGER NOT NULL DEFAULT 0, migration_notes TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS content_hooks (
  id INTEGER PRIMARY KEY, hook_text TEXT NOT NULL, category TEXT, source TEXT,
  legacy_system_name TEXT, legacy_table_name TEXT, legacy_record_id INTEGER,
  imported_at TEXT, imported_by INTEGER, import_batch_id TEXT, potential_duplicate INTEGER NOT NULL DEFAULT 0,
  migration_notes TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS content_prompt_templates (
  id INTEGER PRIMARY KEY, template_name TEXT NOT NULL, template_type TEXT, prompt_body TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'Active', legacy_system_name TEXT, legacy_table_name TEXT,
  legacy_record_id INTEGER, imported_at TEXT, imported_by INTEGER, import_batch_id TEXT,
  potential_duplicate INTEGER NOT NULL DEFAULT 0, migration_notes TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS content_import_staging (
  id INTEGER PRIMARY KEY, import_batch_id TEXT NOT NULL, legacy_system_name TEXT NOT NULL,
  legacy_table_name TEXT NOT NULL, legacy_record_id INTEGER NOT NULL,
  old_title TEXT, old_body TEXT, old_category TEXT, old_status TEXT,
  old_year INTEGER, old_month INTEGER, old_week INTEGER, old_publish_date TEXT,
  old_written_date TEXT, old_recorded_at TEXT, old_tags TEXT, old_notes TEXT,
  old_source_path TEXT, old_source_type TEXT, old_source_url TEXT, old_file_hash TEXT,
  legacy_created_at TEXT, legacy_updated_at TEXT, content_hash TEXT, title_hash TEXT,
  duplicate_group_id TEXT, suggested_year INTEGER, issue_flags_json TEXT NOT NULL DEFAULT '{}',
  suggested_category TEXT, confirmed_year INTEGER, confirmed_category TEXT,
  reviewed_by INTEGER, reviewed_at TEXT, review_status TEXT NOT NULL DEFAULT 'Needs Review',
  has_empty_body INTEGER NOT NULL DEFAULT 0, missing_gdoc_source INTEGER NOT NULL DEFAULT 0,
  duplicate_title INTEGER NOT NULL DEFAULT 0, pending_manual_category INTEGER NOT NULL DEFAULT 0,
  missing_year INTEGER NOT NULL DEFAULT 0, needs_review INTEGER NOT NULL DEFAULT 0,
  ready_for_import INTEGER NOT NULL DEFAULT 0, potential_duplicate INTEGER NOT NULL DEFAULT 0,
  migration_notes TEXT, final_imported_at TEXT, final_import_batch_id TEXT,
  final_import_topic_id INTEGER, final_import_script_id INTEGER,
  skipped_existing_import INTEGER NOT NULL DEFAULT 0,
  imported_at TEXT NOT NULL, imported_by INTEGER,
  UNIQUE(legacy_system_name, legacy_table_name, legacy_record_id)
);
CREATE TABLE IF NOT EXISTS content_import_logs (
  id INTEGER PRIMARY KEY, import_batch_id TEXT NOT NULL, action TEXT NOT NULL,
  status TEXT NOT NULL, total_records_found INTEGER NOT NULL DEFAULT 0,
  records_imported_to_staging INTEGER NOT NULL DEFAULT 0, empty_body_count INTEGER NOT NULL DEFAULT 0,
  missing_gdoc_source_count INTEGER NOT NULL DEFAULT 0, duplicate_title_groups INTEGER NOT NULL DEFAULT 0,
  duplicate_title_records INTEGER NOT NULL DEFAULT 0, pending_manual_category_count INTEGER NOT NULL DEFAULT 0,
  missing_year_count INTEGER NOT NULL DEFAULT 0, ready_count INTEGER NOT NULL DEFAULT 0,
  failed_count INTEGER NOT NULL DEFAULT 0, report_md_path TEXT, report_json_path TEXT,
  error_message TEXT, created_by INTEGER, created_at TEXT NOT NULL, completed_at TEXT
);
CREATE TABLE IF NOT EXISTS content_compliance_checks (
  id INTEGER PRIMARY KEY, script_id INTEGER REFERENCES content_scripts(id) ON DELETE CASCADE,
  staging_id INTEGER REFERENCES content_import_staging(id) ON DELETE CASCADE,
  check_type TEXT NOT NULL, status TEXT NOT NULL, result_json TEXT,
  legacy_system_name TEXT, legacy_table_name TEXT, legacy_record_id INTEGER,
  imported_at TEXT, imported_by INTEGER, import_batch_id TEXT, potential_duplicate INTEGER NOT NULL DEFAULT 0,
  migration_notes TEXT, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS content_extraction_jobs (
  id INTEGER PRIMARY KEY,
  source_platform TEXT NOT NULL DEFAULT 'Xiaohongshu',
  source_url TEXT,
  source_type TEXT,
  content_type TEXT,
  author_name TEXT,
  note_id TEXT,
  xsec_token TEXT,
  note_title TEXT,
  note_text TEXT,
  image_urls_json TEXT NOT NULL DEFAULT '[]',
  video_url TEXT,
  comment_count INTEGER,
  like_count INTEGER,
  share_count INTEGER,
  tikhub_raw_json TEXT,
  tikhub_status TEXT,
  tikhub_error TEXT,
  local_video_path TEXT,
  uploaded_file_path TEXT,
  pasted_text TEXT,
  video_transcript TEXT,
  video_transcript_json TEXT,
  ocr_text TEXT,
  merged_text TEXT,
  speech_transcript TEXT,
  speech_transcript_with_timestamps TEXT,
  subtitle_ocr_text TEXT,
  subtitle_ocr_frames_count INTEGER NOT NULL DEFAULT 0,
  original_speech_text TEXT,
  original_subtitle_text TEXT,
  merged_original_text TEXT,
  cleaned_text TEXT,
  extraction_confidence REAL NOT NULL DEFAULT 0,
  needs_manual_review INTEGER NOT NULL DEFAULT 0,
  manual_review_reason TEXT,
  calibrated_text TEXT,
  calibration_notes_json TEXT,
  low_confidence_segments_json TEXT,
  ocr_completeness REAL NOT NULL DEFAULT 0,
  asr_completeness REAL NOT NULL DEFAULT 0,
  ocr_asr_similarity REAL NOT NULL DEFAULT 0,
  calibration_confirmed_at TEXT,
  calibration_confirmed_text TEXT,
  cleaned_original_text TEXT,
  transcript_text TEXT,
  hook_analysis TEXT,
  main_topic TEXT,
  hook TEXT,
  pain_points TEXT,
  conflict_points TEXT,
  emotional_points TEXT,
  contrarian_view TEXT,
  case_usage TEXT,
  structure_analysis TEXT,
  content_structure TEXT,
  viral_reason TEXT,
  reusable_sentences TEXT,
  reusable_structure TEXT,
  rewrite_direction TEXT,
  selling_points TEXT,
  topic_tags TEXT,
  rewrite_for_fage TEXT,
  rewritten_titles TEXT,
  rewritten_script TEXT,
  rewritten_hook TEXT,
  suggested_tags TEXT,
  key_message TEXT,
  final_script TEXT,
  status TEXT NOT NULL DEFAULT '已提取',
  category TEXT,
  tags TEXT,
  planned_recording_date TEXT,
  planned_publish_date TEXT,
  actual_publish_date TEXT,
  is_in_script_library INTEGER NOT NULL DEFAULT 0,
  confirmed_at TEXT,
  confirmed_title TEXT,
  confirmed_script TEXT,
  confirmed_hook TEXT,
  confirmed_tags TEXT,
  review_notes TEXT,
  view_count INTEGER NOT NULL DEFAULT 0,
  favorite_count INTEGER NOT NULL DEFAULT 0,
  private_message_count INTEGER NOT NULL DEFAULT 0,
  wechat_add_count INTEGER NOT NULL DEFAULT 0,
  lead_quality TEXT,
  conversion_result TEXT,
  review_summary TEXT,
  next_topic_recommendations TEXT,
  second_rewrite_suggestions TEXT,
  reviewed_at TEXT,
  process_status TEXT NOT NULL DEFAULT '待处理',
  error_message TEXT,
  dependency_status_json TEXT,
  created_script_id INTEGER REFERENCES content_scripts(id) ON DELETE SET NULL,
  published_post_id INTEGER REFERENCES content_published_posts(id) ON DELETE SET NULL,
  created_by INTEGER REFERENCES users(id),
  updated_by INTEGER REFERENCES users(id),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_content_staging_batch ON content_import_staging(import_batch_id);
CREATE INDEX IF NOT EXISTS idx_content_staging_issues ON content_import_staging(needs_review, ready_for_import);
CREATE INDEX IF NOT EXISTS idx_content_staging_legacy ON content_import_staging(legacy_system_name, legacy_table_name, legacy_record_id);
CREATE INDEX IF NOT EXISTS idx_content_scripts_legacy ON content_scripts(legacy_system_name, legacy_table_name, legacy_record_id);
CREATE INDEX IF NOT EXISTS idx_content_materials_source_modified_at ON content_materials(source_modified_at DESC,id DESC);
CREATE INDEX IF NOT EXISTS idx_content_materials_script_id_id ON content_materials(script_id,id DESC);
CREATE INDEX IF NOT EXISTS idx_content_extraction_jobs_status ON content_extraction_jobs(process_status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_content_extraction_jobs_script ON content_extraction_jobs(created_script_id);
"""

ROLE_PERMISSIONS = {
    "Admin": ["*"],
    "Manager": ["students.view", "students.edit", "documents.view", "documents.manage", "offers.manage", "coe.manage", "visas.manage", "payments.manage", "contracts.manage", "audit.view"],
    "Content Staff": ["content.view", "content.manage", "content.import"],
    "Consultant": ["students.view", "students.edit", "documents.view", "documents.manage", "offers.manage", "coe.manage", "visas.manage"],
    "Visa Officer": ["students.view", "documents.view", "coe.manage", "visas.manage"],
    "Finance": ["students.view", "documents.view", "payments.manage"],
    "View Only": ["students.view", "documents.view"],
}

STUDENT_CASE_PERMISSION_PREFIX = "student_cases."
ADMIN_STUDENT_CASE_PRESERVED_PERMISSIONS = frozenset(
    {f"{STUDENT_CASE_PERMISSION_PREFIX}view"}
)
SAFE_PRODUCTION_ADMIN_STUDENT_CASE_PERMISSIONS = frozenset(
    {
        f"{STUDENT_CASE_PERMISSION_PREFIX}view",
        f"{STUDENT_CASE_PERMISSION_PREFIX}create",
        f"{STUDENT_CASE_PERMISSION_PREFIX}edit",
        f"{STUDENT_CASE_PERMISSION_PREFIX}assign",
        f"{STUDENT_CASE_PERMISSION_PREFIX}link_records",
        f"{STUDENT_CASE_PERMISSION_PREFIX}archive",
    }
)

TRANSFER_ADMIN_STUDENT_CASE_PERMISSIONS = frozenset(
    {
        "student_cases.create",
        "student_cases.edit",
        "student_cases.assign",
        "student_cases.link_records",
        "student_cases.archive",
    }
)
PROGRESSION_ADMIN_STUDENT_CASE_PERMISSIONS = frozenset(
    {
        "student_cases.edit",
        "student_cases.assign",
    }
)
COMPLETION_ADMIN_STUDENT_CASE_PERMISSIONS = frozenset(
    {
        "student_cases.complete",
    }
)
TRANSFER_COMPLETE_ROLE_NAMES = frozenset(
    {
        "Admin",
        "Manager + Student Cases",
        "Consultant + Student Cases",
    }
)
TRANSFER_COMPLETE_REQUIRED_PERMISSIONS = frozenset(
    {
        "student_cases.view",
        "student_cases.create",
        "student_cases.edit",
        "student_cases.assign",
        "student_cases.link_records",
        "student_cases.archive",
    }
)


class RolePermissionsInitializationError(ValueError):
    """Raised when stored role permissions cannot be safely reconciled."""


def reconciled_role_permissions(
    role_name,
    raw_permissions,
    base_permissions,
    *,
    feature_flags=None,
):
    try:
        parsed = json.loads(raw_permissions)
    except (TypeError, json.JSONDecodeError) as exc:
        raise RolePermissionsInitializationError(
            f"{role_name}: invalid permissions_json"
        ) from exc
    if not isinstance(parsed, list) or any(
        type(permission) is not str for permission in parsed
    ):
        raise RolePermissionsInitializationError(
            f"{role_name}: invalid permissions_json"
        )

    parsed_permissions = frozenset(parsed)
    merged = []
    seen = set()
    approved_role_permissions = set()
    if isinstance(feature_flags, StudentCaseFeatureFlags):
        if role_name in TRANSFER_COMPLETE_ROLE_NAMES:
            if feature_flags.read_enabled:
                approved_role_permissions.update(
                    ADMIN_STUDENT_CASE_PRESERVED_PERMISSIONS
                )
            if feature_flags.transfer_write_enabled:
                approved_role_permissions.update(
                    TRANSFER_ADMIN_STUDENT_CASE_PERMISSIONS
                )
            if role_name == "Admin" and feature_flags.progression_write_enabled:
                approved_role_permissions.update(
                    PROGRESSION_ADMIN_STUDENT_CASE_PERMISSIONS
                )
            if (
                feature_flags.completion_enabled
                and feature_flags.transfer_write_enabled
                and TRANSFER_COMPLETE_REQUIRED_PERMISSIONS.issubset(
                    parsed_permissions
                )
            ):
                approved_role_permissions.update(
                    COMPLETION_ADMIN_STUDENT_CASE_PERMISSIONS
                )
    for permission in parsed:
        if permission in seen:
            continue
        permission_namespace, separator, _permission_name = permission.partition(".")
        if separator and permission_namespace == "student_cases":
            safe_production_admin_permission = (
                os.environ.get("AAIE_SAFE_PRODUCTION") == "1"
                and role_name == "Admin"
                and permission in SAFE_PRODUCTION_ADMIN_STUDENT_CASE_PERMISSIONS
            )
            allowed = (
                permission in approved_role_permissions
            ) or safe_production_admin_permission
            if not allowed:
                raise RolePermissionsInitializationError(
                    f"{role_name}: forbidden Student Case permission"
                )
        merged.append(permission)
        seen.add(permission)

    for permission in base_permissions:
        if permission not in seen:
            merged.append(permission)
            seen.add(permission)
    return merged, merged != parsed


MODULE_DEFINITIONS = [
    ("dashboard", "Dashboard", "首页 / 工作台", "/", 10, "Core"),
    ("students", "Students", "学生管理", "/students", 20, "Core"),
    ("parent_form", "Parent Form", "家长表单", "/parent-links", 30, "Core"),
    ("reminders", "Reminder Center", "提醒中心", "/reminders", 35, "Core"),
    ("school_library", "School Management Center", "学校管理", "/schools", 37, "Core"),
    ("school_plan_reports", "School Plan Reports", "学校方案报告", "/school-plan-reports", 38, "Core"),
    ("higher_education", "Higher Education", "高等教育申请", "/higher-education", 39, "Student Workflow"),
    ("content_center", "Content Management Center", "内容管理中心", "/content", 39, "Content Management"),
    ("agent_reach", "Agent Reach", "Agent Reach", "/agent-reach", 40, "Content Management"),
    ("applications", "Applications", "申请材料", "/students/{student_id}?tab=applications", 40, "Student Workflow"),
    ("offer", "Offer", "Offer 管理", "/students/{student_id}?tab=offer", 50, "Student Workflow"),
    ("coe_oshc", "COE + OSHC", "COE + OSHC", "/students/{student_id}?tab=coe", 60, "Student Workflow"),
    ("visa", "VISA", "VISA", "/students/{student_id}?tab=visa", 70, "Student Workflow"),
    ("payments", "Payments", "Payments", "/students/{student_id}?tab=payments", 80, "Student Workflow"),
    ("student_contracts", "Student Contract Management", "学生合同管理", "/students/{student_id}?tab=contracts", 90, "Contracts"),
    ("contract_management_center", "Global Contract Management Center", "合同管理", "/contracts", 100, "Contracts"),
    ("template_management", "Template Management", "模板管理", "/templates", 110, "Contracts"),
    ("revenue_commission", "Revenue & Commission", "收入与佣金", "/revenue", 115, "Management"),
    ("reports", "Reports", "数据报表", "/reports", 120, "Management"),
    ("data_update_centre", "Data Update Centre", "数据更新中心", "/data-updates", 125, "Management"),
    ("file_manager", "File Manager", "文件管理", "/file-manager", 130, "Management"),
    ("backup_restore", "System Backup & Restore", "系统备份与恢复", "/settings/backup-restore", 140, "Management"),
    ("safebox_centre", "SAFEBOX Centre", "SAFEBOX / 恢复记录", "/safebox", 145, "Management"),
    ("user_management", "User Management", "用户管理", "/users", 150, "Management"),
    ("audit_logs", "Audit Logs", "操作日志", "/audit-logs", 160, "Management"),
    ("system_settings", "System Settings", "系统设置", "/settings", 170, "Management"),
]

MODULE_CATEGORIES = ("Core", "Content Management", "Student Workflow", "Contracts", "Management")

ROLE_MODULE_PRESETS = {
    "Admin": [key for key, *_ in MODULE_DEFINITIONS],
    "Manager": [
        "dashboard", "students", "parent_form", "reminders", "school_library", "school_plan_reports", "higher_education", "data_update_centre", "applications", "offer", "coe_oshc",
        "visa", "payments", "student_contracts", "contract_management_center", "template_management",
        "reports", "file_manager", "audit_logs", "system_settings",
    ],
    "Content Staff": ["dashboard"],
    "Consultant": ["dashboard", "students", "parent_form", "reminders", "school_library", "school_plan_reports", "higher_education", "applications", "offer", "coe_oshc", "visa"],
    "Visa Officer": ["dashboard", "students", "school_library", "applications", "coe_oshc", "visa"],
    "Finance": ["dashboard", "reminders", "payments", "contract_management_center"],
    "View Only": ["dashboard", "students", "school_library"],
}


class Database:
    def __init__(self, path):
        self.path = STAGING.check(Path(path)) if STAGING else Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._request_local = threading.local()

    def _open_connection(self):
        if STAGING:
            STAGING.check(self.path)
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        gate = getattr(conn, "write_gate", None)
        if gate is None or gate.status()["state"] == "OPEN":
            conn.execute("PRAGMA journal_mode = WAL")
        return conn

    def _request_state(self):
        request_local = getattr(self, "_request_local", None)
        return getattr(request_local, "state", None)

    def connect(self):
        state = self._request_state()
        if state is None:
            return self._open_connection()
        if state["thread_id"] != threading.get_ident():
            raise RuntimeError("request-scoped SQLite connection cannot cross threads")
        if state["connection"] is None:
            state["connection"] = self._open_connection()
        return state["connection"]

    def _release_connection(self, conn):
        state = self._request_state()
        if state is not None and state["connection"] is conn:
            return
        conn.close()

    @contextmanager
    def request_scope(self):
        state = self._request_state()
        if state is not None:
            if state["thread_id"] != threading.get_ident():
                raise RuntimeError("request-scoped SQLite connection cannot cross threads")
            state["depth"] += 1
            try:
                yield
            finally:
                state["depth"] -= 1
            return

        state = {
            "connection": None,
            "depth": 1,
            "thread_id": threading.get_ident(),
        }
        self._request_local.state = state
        try:
            yield
        finally:
            connection = state["connection"]
            try:
                if connection is not None:
                    connection.close()
            finally:
                del self._request_local.state

    @contextmanager
    def read_snapshot(self):
        """Own a read-only snapshot; restore the request's previous connection."""
        with self.request_scope():
            state = self._request_state()
            previous = state["connection"]
            if previous is not None and previous.in_transaction:
                raise RuntimeError("cannot replace an active request transaction")
            connection = sqlite3.connect(
                self.path.absolute().as_uri() + "?mode=ro", uri=True, timeout=30
            )
            connection.row_factory = sqlite3.Row
            try:
                connection.execute("BEGIN")
                state["connection"] = connection
                yield connection
            finally:
                state["connection"] = previous
                try:
                    connection.rollback()
                finally:
                    connection.close()

    @contextmanager
    def transaction(self):
        with DB_LOCK:
            conn = self.connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                self._release_connection(conn)

    def initialize(self):
        with self.transaction() as conn:
            conn.executescript(SCHEMA)
            self.ensure_visa_schema_v1_compatibility(conn)
            self.ensure_columns(conn, "school_profile_data", {
                "boys_count": "INTEGER",
                "girls_count": "INTEGER",
                "total_enrolments": "INTEGER",
                "boys_percentage": "REAL",
                "girls_percentage": "REAL",
                "gender_balance_score": "REAL",
                "lbote_value": "REAL",
                "lbote_value_type": "TEXT",
                "lbote_percentage": "REAL",
            })
            self.ensure_columns(conn, "staging_school_profile_data", {
                "boys_count": "INTEGER",
                "girls_count": "INTEGER",
                "total_enrolments": "INTEGER",
                "boys_percentage": "REAL",
                "girls_percentage": "REAL",
                "gender_balance_score": "REAL",
                "lbote_value": "REAL",
                "lbote_value_type": "TEXT",
                "lbote_percentage": "REAL",
            })
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_school_profile_data_search_metrics "
                "ON school_profile_data(is_active, gender_balance_score, lbote_percentage)"
            )
            self.ensure_nullable_contract_student_id(conn)
            self.ensure_manual_placements_contract_fk(conn)
            self.ensure_columns(conn, "users", {
                "email": "TEXT",
                "created_by": "INTEGER REFERENCES users(id)",
                "deleted_at": "TEXT",
                "deleted_by": "INTEGER REFERENCES users(id)",
            })
            self.ensure_columns(conn, "sessions", {
                "last_seen_at": "TEXT",
            })
            self.ensure_columns(conn, "audit_logs", {
                "module_key": "TEXT",
                "student_id": "TEXT",
                "contract_id": "TEXT",
                "user_agent": "TEXT",
                "status": "TEXT",
                "error_message": "TEXT",
            })
            self.ensure_columns(conn, "school_plan_report_links", {
                "viewer_id": "INTEGER REFERENCES school_plan_report_viewers(id) ON DELETE SET NULL",
                "security_mode": "TEXT NOT NULL DEFAULT 'wechat_required'",
                "bound_wechat_openid": "TEXT",
                "bound_wechat_unionid": "TEXT",
                "bound_wechat_nickname": "TEXT",
                "bound_wechat_avatar_url": "TEXT",
                "bound_wechat_at": "TEXT",
                "unbound_wechat_at": "TEXT",
                "last_login_at": "TEXT",
            })
            self.ensure_columns(conn, "school_plan_report_events", {
                "viewer_id": "INTEGER REFERENCES school_plan_report_viewers(id) ON DELETE SET NULL",
            })
            self.ensure_columns(conn, "school_plan_report_view_logs", {
                "viewer_id": "INTEGER REFERENCES school_plan_report_viewers(id) ON DELETE SET NULL",
            })
            self.ensure_columns(conn, "school_plan_report_confirmations", {
                "viewer_id": "INTEGER REFERENCES school_plan_report_viewers(id) ON DELETE SET NULL",
            })
            conn.execute("CREATE INDEX IF NOT EXISTS idx_school_plan_report_links_viewer ON school_plan_report_links(viewer_id, status)")
            self.ensure_columns(conn, "workflow_status", {
                "contract_signed_at": "TEXT",
                "visa_lodged_at": "TEXT",
                "visa_finalized_at": "TEXT",
                "completed_at": "TEXT",
                "contract_to_visa_days": "INTEGER",
                "visa_lodged_to_final_days": "INTEGER",
                "completion_source": "TEXT",
                "completion_metrics_updated_at": "TEXT",
            })
            self.ensure_columns(conn, "user_module_permissions", {
                "can_open": "INTEGER NOT NULL DEFAULT 0",
                "can_mark_delete": "INTEGER NOT NULL DEFAULT 0",
                "can_mark_move": "INTEGER NOT NULL DEFAULT 0",
                "can_mark_rename": "INTEGER NOT NULL DEFAULT 0",
                "can_cancel_file_marker": "INTEGER NOT NULL DEFAULT 0",
                "can_mark_file_marker_processed": "INTEGER NOT NULL DEFAULT 0",
                "can_complete": "INTEGER NOT NULL DEFAULT 0",
                "can_dismiss": "INTEGER NOT NULL DEFAULT 0",
                "can_reassign": "INTEGER NOT NULL DEFAULT 0",
                "can_manage_rules": "INTEGER NOT NULL DEFAULT 0",
                "can_confirm_upload_review": "INTEGER NOT NULL DEFAULT 0",
                "can_view_dashboard": "INTEGER NOT NULL DEFAULT 0",
                "can_view_topics": "INTEGER NOT NULL DEFAULT 0",
                "can_create_topics": "INTEGER NOT NULL DEFAULT 0",
                "can_edit_topics": "INTEGER NOT NULL DEFAULT 0",
                "can_generate_scripts": "INTEGER NOT NULL DEFAULT 0",
                "can_edit_scripts": "INTEGER NOT NULL DEFAULT 0",
                "can_review_scripts": "INTEGER NOT NULL DEFAULT 0",
                "can_approve_scripts": "INTEGER NOT NULL DEFAULT 0",
                "can_schedule_posts": "INTEGER NOT NULL DEFAULT 0",
                "can_mark_published": "INTEGER NOT NULL DEFAULT 0",
                "can_enter_performance_data": "INTEGER NOT NULL DEFAULT 0",
                "can_view_performance": "INTEGER NOT NULL DEFAULT 0",
                "can_manage_material_library": "INTEGER NOT NULL DEFAULT 0",
                "can_import_old_data": "INTEGER NOT NULL DEFAULT 0",
                "can_upload_documents": "INTEGER NOT NULL DEFAULT 0",
                "can_review_features": "INTEGER NOT NULL DEFAULT 0",
                "can_edit_zone_data": "INTEGER NOT NULL DEFAULT 0",
                "can_edit_academic_info": "INTEGER NOT NULL DEFAULT 0",
                "can_upload_logo": "INTEGER NOT NULL DEFAULT 0",
                "can_run_scraper": "INTEGER NOT NULL DEFAULT 0",
                "can_view_reports": "INTEGER NOT NULL DEFAULT 0",
                "can_generate_reports": "INTEGER NOT NULL DEFAULT 0",
                "can_view_feature_tags": "INTEGER NOT NULL DEFAULT 0",
                "can_manage_feature_categories": "INTEGER NOT NULL DEFAULT 0",
                "can_manage_feature_tags": "INTEGER NOT NULL DEFAULT 0",
                "can_collect_feature_tags": "INTEGER NOT NULL DEFAULT 0",
                "can_review_feature_suggestions": "INTEGER NOT NULL DEFAULT 0",
                "can_confirm_feature_tags": "INTEGER NOT NULL DEFAULT 0",
                "can_reject_feature_tags": "INTEGER NOT NULL DEFAULT 0",
                "can_add_manual_feature_tags": "INTEGER NOT NULL DEFAULT 0",
                "can_view_suburb_data": "INTEGER NOT NULL DEFAULT 0",
                "can_fetch_suburb_data": "INTEGER NOT NULL DEFAULT 0",
                "can_edit_suburb_data": "INTEGER NOT NULL DEFAULT 0",
                "can_review_suburb_data": "INTEGER NOT NULL DEFAULT 0",
                "can_manage_suburb_sources": "INTEGER NOT NULL DEFAULT 0",
                "can_view_myschool_data": "INTEGER NOT NULL DEFAULT 0",
                "can_fetch_myschool_data": "INTEGER NOT NULL DEFAULT 0",
                "can_edit_myschool_data": "INTEGER NOT NULL DEFAULT 0",
                "can_review_myschool_data": "INTEGER NOT NULL DEFAULT 0",
                "can_resolve_school_data_conflicts": "INTEGER NOT NULL DEFAULT 0",
                "can_upload_ranking_source": "INTEGER NOT NULL DEFAULT 0",
                "can_import_rankings": "INTEGER NOT NULL DEFAULT 0",
                "can_review_ranking_matches": "INTEGER NOT NULL DEFAULT 0",
                "can_view_rankings": "INTEGER NOT NULL DEFAULT 0",
                "can_edit_ranking_data": "INTEGER NOT NULL DEFAULT 0",
                "can_view_collection_center": "INTEGER NOT NULL DEFAULT 0",
                "can_fetch_school_website": "INTEGER NOT NULL DEFAULT 0",
                "can_fetch_education_department": "INTEGER NOT NULL DEFAULT 0",
                "can_upload_bettereducation_file": "INTEGER NOT NULL DEFAULT 0",
                "can_fetch_goodschools": "INTEGER NOT NULL DEFAULT 0",
                "can_fetch_abs_data": "INTEGER NOT NULL DEFAULT 0",
                "can_review_collected_data": "INTEGER NOT NULL DEFAULT 0",
                "can_approve_collected_data": "INTEGER NOT NULL DEFAULT 0",
                "can_manage_source_urls": "INTEGER NOT NULL DEFAULT 0",
            })
            self.ensure_columns(conn, "safebox_records", {
                "record_source": "TEXT NOT NULL DEFAULT '手动'",
                "auto_started_at": "TEXT",
                "auto_finished_at": "TEXT",
                "git_status_before": "TEXT",
                "git_status_after": "TEXT",
                "stash_present": "INTEGER NOT NULL DEFAULT 0",
                "workspace_clean_before": "INTEGER",
                "workspace_clean_after": "INTEGER",
                "diff_summary": "TEXT",
                "database_risk": "TEXT",
                "schema_change_risk": "TEXT",
                "health_check_result": "TEXT",
                "restore_instruction": "TEXT",
            })
            self.ensure_columns(conn, "school_ranking_imports", {
                "report_md_path": "TEXT",
                "report_json_path": "TEXT",
                "confirmed_rows": "INTEGER NOT NULL DEFAULT 0",
                "skipped_rows": "INTEGER NOT NULL DEFAULT 0",
                "review_status": "TEXT NOT NULL DEFAULT 'Needs Review'",
                "data_update_job_id": "INTEGER REFERENCES data_update_jobs(id) ON DELETE SET NULL",
                "update_policy": "TEXT NOT NULL DEFAULT 'save_as_new_version'",
                "version_key": "TEXT",
                "version_number": "INTEGER NOT NULL DEFAULT 0",
                "duplicate_rows": "INTEGER NOT NULL DEFAULT 0",
            })
            self.ensure_columns(conn, "school_academic_rankings", {
                "version_key": "TEXT",
                "version_number": "INTEGER NOT NULL DEFAULT 0",
                "is_latest_verified": "INTEGER NOT NULL DEFAULT 0",
            })
            self.ensure_columns(conn, "data_update_jobs", {
                "source_type": "TEXT",
                "source_name": "TEXT",
                "source_url": "TEXT",
                "uploaded_file_reference": "TEXT",
                "data_year": "TEXT",
                "records_skipped": "INTEGER NOT NULL DEFAULT 0",
                "duplicate_records": "INTEGER NOT NULL DEFAULT 0",
                "update_policy": "TEXT NOT NULL DEFAULT 'save_as_new_version'",
                "version_key": "TEXT",
                "version_number": "INTEGER NOT NULL DEFAULT 0",
            })
            self.ensure_columns(conn, "bettereducation_fetch_queue", {
                "sector_key": "TEXT",
            })
            self.ensure_academic_results_update_schema(conn)
            self.ensure_columns(conn, "school_suburb_data", {
                "geography_type": "TEXT",
                "geography_code": "TEXT",
                "retrieved_at": "TEXT",
                "raw_json": "TEXT",
            })
            conn.execute(
                """CREATE TABLE IF NOT EXISTS csa_crime_area_statistics (
                   id INTEGER PRIMARY KEY,
                   state TEXT NOT NULL DEFAULT 'VIC',
                   data_level TEXT NOT NULL,
                   area_name TEXT NOT NULL,
                   normalized_area_key TEXT NOT NULL,
                   suburb TEXT,
                   postcode TEXT,
                   lga_name TEXT,
                   data_year_ending TEXT NOT NULL,
                   source_dataset TEXT NOT NULL,
                   source TEXT NOT NULL DEFAULT 'Crime Statistics Agency / DataVic',
                   source_url TEXT NOT NULL,
                   total_recorded_offences INTEGER,
                   offence_rate_per_100000 REAL,
                   criminal_incidents_total INTEGER,
                   criminal_incidents_rate_per_100000 REAL,
                   family_incidents_total INTEGER,
                   family_incidents_rate_per_100000 REAL,
                   crimes_against_person INTEGER,
                   property_and_deception_offences INTEGER,
                   drug_offences INTEGER,
                   public_order_and_security_offences INTEGER,
                   justice_procedures_offences INTEGER,
                   other_offences INTEGER,
                   residential_burglary_count INTEGER,
                   theft_from_motor_vehicle_count INTEGER,
                   motor_vehicle_theft_count INTEGER,
                   robbery_count INTEGER,
                   assault_count INTEGER,
                   property_damage_count INTEGER,
                   trend_1_year_percent REAL,
                   trend_5_year_percent REAL,
                   notes TEXT,
                   raw_json TEXT,
                   imported_at TEXT NOT NULL,
                   updated_at TEXT NOT NULL,
                   UNIQUE(state,data_level,normalized_area_key,data_year_ending,source_dataset)
                )"""
            )
            conn.execute(
                """CREATE TABLE IF NOT EXISTS school_area_crime_profiles (
                   id INTEGER PRIMARY KEY,
                   school_id INTEGER NOT NULL REFERENCES schools(id) ON DELETE CASCADE,
                   state TEXT NOT NULL DEFAULT 'VIC',
                   suburb TEXT,
                   postcode TEXT,
                   lga_name TEXT,
                   data_level TEXT NOT NULL,
                   data_year_ending TEXT NOT NULL,
                   source TEXT NOT NULL DEFAULT 'Crime Statistics Agency / DataVic',
                   source_url TEXT NOT NULL,
                   total_recorded_offences INTEGER,
                   offence_rate_per_100000 REAL,
                   criminal_incidents_total INTEGER,
                   criminal_incidents_rate_per_100000 REAL,
                   family_incidents_total INTEGER,
                   family_incidents_rate_per_100000 REAL,
                   crimes_against_person INTEGER,
                   property_and_deception_offences INTEGER,
                   drug_offences INTEGER,
                   public_order_and_security_offences INTEGER,
                   justice_procedures_offences INTEGER,
                   other_offences INTEGER,
                   residential_burglary_count INTEGER,
                   theft_from_motor_vehicle_count INTEGER,
                   motor_vehicle_theft_count INTEGER,
                   robbery_count INTEGER,
                   assault_count INTEGER,
                   property_damage_count INTEGER,
                   trend_1_year_percent REAL,
                   trend_5_year_percent REAL,
                   notes TEXT,
                   last_checked_at TEXT,
                   created_at TEXT NOT NULL,
                   updated_at TEXT NOT NULL,
                   UNIQUE(school_id,data_level,data_year_ending)
                )"""
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_csa_crime_area_statistics_lookup ON csa_crime_area_statistics(state,data_level,normalized_area_key,data_year_ending)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_school_area_crime_profiles_school_year ON school_area_crime_profiles(school_id,data_year_ending,data_level)")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS rental_market (
                  id INTEGER PRIMARY KEY,
                  state TEXT NOT NULL,
                  suburb TEXT NOT NULL,
                  postcode TEXT,
                  property_type TEXT NOT NULL,
                  bedrooms TEXT,
                  median_weekly_rent REAL,
                  quarter TEXT NOT NULL,
                  source TEXT NOT NULL,
                  last_updated TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS official_rental_market_details (
                  id INTEGER PRIMARY KEY,
                  state TEXT NOT NULL,
                  area_level TEXT NOT NULL,
                  area_name TEXT NOT NULL,
                  suburb TEXT,
                  postcode TEXT,
                  lga_name TEXT,
                  property_type TEXT NOT NULL,
                  bedrooms TEXT,
                  median_weekly_rent REAL,
                  quarter TEXT NOT NULL,
                  year INTEGER,
                  source TEXT NOT NULL,
                  source_url TEXT,
                  last_updated TEXT NOT NULL,
                  raw_json TEXT,
                  imported_at TEXT NOT NULL,
                  UNIQUE(state,area_level,area_name,property_type,bedrooms,quarter,source)
                );
                CREATE TABLE IF NOT EXISTS property_market (
                  id INTEGER PRIMARY KEY,
                  state TEXT NOT NULL,
                  suburb TEXT NOT NULL,
                  postcode TEXT,
                  property_type TEXT NOT NULL,
                  median_sale_price REAL,
                  period TEXT NOT NULL,
                  sales_count INTEGER,
                  source TEXT NOT NULL,
                  last_updated TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS geo_areas (
                  id INTEGER PRIMARY KEY,
                  state TEXT NOT NULL,
                  area_type TEXT NOT NULL,
                  area_name TEXT NOT NULL,
                  suburb TEXT,
                  postcode TEXT,
                  sa2_code TEXT,
                  lga_code TEXT,
                  geometry TEXT,
                  latitude REAL,
                  longitude REAL,
                  UNIQUE(state,area_type,area_name,postcode)
                );
                CREATE TABLE IF NOT EXISTS school_radius_area_links (
                  id INTEGER PRIMARY KEY,
                  school_id INTEGER NOT NULL REFERENCES schools(id) ON DELETE CASCADE,
                  radius_km INTEGER NOT NULL,
                  area_type TEXT NOT NULL,
                  area_id INTEGER REFERENCES geo_areas(id) ON DELETE CASCADE,
                  area_name TEXT NOT NULL,
                  distance_from_school_km REAL,
                  overlap_ratio REAL,
                  calculation_method TEXT NOT NULL,
                  last_calculated_at TEXT NOT NULL,
                  UNIQUE(school_id,radius_km,area_type,area_id)
                );
                CREATE TABLE IF NOT EXISTS school_radius_market_summary (
                  id INTEGER PRIMARY KEY,
                  school_id INTEGER NOT NULL REFERENCES schools(id) ON DELETE CASCADE,
                  radius_km INTEGER NOT NULL,
                  median_weekly_rent_all REAL,
                  median_weekly_rent_house REAL,
                  median_weekly_rent_unit REAL,
                  median_house_price REAL,
                  median_unit_price REAL,
                  source_period_rent TEXT,
                  source_period_sale TEXT,
                  confidence_level TEXT NOT NULL DEFAULT 'low',
                  notes TEXT,
                  last_calculated_at TEXT NOT NULL,
                  UNIQUE(school_id,radius_km)
                );
                CREATE INDEX IF NOT EXISTS idx_rental_market_lookup ON rental_market(state, suburb, property_type, quarter);
                CREATE INDEX IF NOT EXISTS idx_official_rental_market_details_lookup ON official_rental_market_details(state, area_level, area_name, property_type, quarter);
                CREATE INDEX IF NOT EXISTS idx_property_market_lookup ON property_market(state, suburb, property_type, period);
                CREATE INDEX IF NOT EXISTS idx_geo_areas_lookup ON geo_areas(state, area_type, suburb, postcode);
                CREATE INDEX IF NOT EXISTS idx_school_radius_area_links_school ON school_radius_area_links(school_id, radius_km);
                CREATE INDEX IF NOT EXISTS idx_school_radius_market_summary_school ON school_radius_market_summary(school_id, radius_km);
                """
            )
            self.ensure_columns(conn, "school_radius_market_summary", {
                "median_weekly_rent_all": "REAL",
            })
            conn.execute(
                """CREATE TABLE IF NOT EXISTS school_feature_categories (
                   id INTEGER PRIMARY KEY, category_key TEXT UNIQUE NOT NULL,
                   category_name_cn TEXT NOT NULL, category_name_en TEXT NOT NULL,
                   description TEXT, sort_order INTEGER NOT NULL DEFAULT 0,
                   is_active INTEGER NOT NULL DEFAULT 1,
                   created_by INTEGER REFERENCES users(id), created_at TEXT NOT NULL,
                   updated_by INTEGER REFERENCES users(id), updated_at TEXT
                )"""
            )
            conn.execute(
                """CREATE TABLE IF NOT EXISTS school_feature_tag_assignments (
                   id INTEGER PRIMARY KEY, school_id INTEGER NOT NULL REFERENCES schools(id) ON DELETE CASCADE,
                   category_id INTEGER REFERENCES school_feature_categories(id),
                   tag_id INTEGER REFERENCES school_feature_tags(id),
                   assignment_type TEXT NOT NULL DEFAULT 'ManualAdded',
                   status TEXT NOT NULL DEFAULT 'Confirmed',
                   confidence_rate REAL, source_url TEXT, source_title TEXT, source_type TEXT,
                   source_excerpt TEXT, collected_at TEXT, collected_by_system INTEGER NOT NULL DEFAULT 0,
                   confirmed_by INTEGER REFERENCES users(id), confirmed_at TEXT,
                   rejected_by INTEGER REFERENCES users(id), rejected_at TEXT,
                   notes TEXT, created_at TEXT NOT NULL, updated_at TEXT
                )"""
            )
            self.ensure_columns(conn, "school_feature_tags", {
                "category_id": "INTEGER REFERENCES school_feature_categories(id)",
                "tag_name_cn": "TEXT",
                "tag_name_en": "TEXT",
                "description": "TEXT",
                "keywords": "TEXT",
                "sort_order": "INTEGER NOT NULL DEFAULT 0",
                "created_by": "INTEGER REFERENCES users(id)",
                "created_at": "TEXT",
                "updated_by": "INTEGER REFERENCES users(id)",
                "updated_at": "TEXT",
            })
            self.ensure_columns(conn, "school_feature_tag_assignments", {
                "category_id": "INTEGER REFERENCES school_feature_categories(id)",
                "tag_id": "INTEGER REFERENCES school_feature_tags(id)",
                "assignment_type": "TEXT NOT NULL DEFAULT 'ManualAdded'",
                "status": "TEXT NOT NULL DEFAULT 'Confirmed'",
                "confidence_rate": "REAL",
                "source_url": "TEXT",
                "source_title": "TEXT",
                "source_type": "TEXT",
                "source_excerpt": "TEXT",
                "collected_at": "TEXT",
                "collected_by_system": "INTEGER NOT NULL DEFAULT 0",
                "confirmed_by": "INTEGER REFERENCES users(id)",
                "confirmed_at": "TEXT",
                "rejected_by": "INTEGER REFERENCES users(id)",
                "rejected_at": "TEXT",
                "notes": "TEXT",
                "created_by": "INTEGER REFERENCES users(id)",
                "created_at": "TEXT",
                "updated_by": "INTEGER REFERENCES users(id)",
                "updated_at": "TEXT",
            })
            conn.execute("CREATE INDEX IF NOT EXISTS idx_school_feature_categories_active ON school_feature_categories(is_active, sort_order)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_school_feature_tags_category ON school_feature_tags(category_id, is_active, sort_order)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_school_feature_assignments_school ON school_feature_tag_assignments(school_id, status, category_id, tag_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_school_feature_assignments_tag ON school_feature_tag_assignments(tag_id, status, confidence_rate)")
            self.ensure_columns(conn, "upload_sessions", {
                "session_token": "TEXT",
                "notes": "TEXT",
            })
            self.ensure_columns(conn, "upload_review_items", {
                "error_message": "TEXT",
            })
            self.ensure_columns(conn, "reminders", {
                "title": "TEXT",
                "description": "TEXT",
                "related_module": "TEXT",
                "related_record_id": "TEXT",
                "assigned_to_user_id": "INTEGER REFERENCES users(id)",
                "assigned_to_name": "TEXT",
                "priority": "TEXT NOT NULL DEFAULT 'Normal'",
                "due_datetime": "TEXT",
                "reminder_datetime": "TEXT",
                "repeat_rule": "TEXT",
                "snooze_until": "TEXT",
                "completed_at": "TEXT",
                "completed_by": "INTEGER REFERENCES users(id)",
                "dismissed_at": "TEXT",
                "dismissed_by": "INTEGER REFERENCES users(id)",
                "created_by": "INTEGER REFERENCES users(id)",
                "updated_by": "INTEGER REFERENCES users(id)",
                "updated_at": "TEXT",
                "source_type": "TEXT",
                "source_record_id": "TEXT",
                "auto_generated": "INTEGER NOT NULL DEFAULT 0",
                "suggested_action": "TEXT",
            })
            conn.execute("CREATE INDEX IF NOT EXISTS idx_reminders_status_time ON reminders(status, reminder_datetime, due_datetime)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_reminders_source ON reminders(source_type, source_record_id, reminder_type, reminder_datetime)")
            self.ensure_columns(conn, "students", {
                "updated_by": "INTEGER REFERENCES users(id)",
                "deleted_at": "TEXT",
                "deleted_by": "INTEGER REFERENCES users(id)",
            })
            self.ensure_columns(conn, "student_documents", {
                "document_status": "TEXT NOT NULL DEFAULT 'active'",
                "voided_at": "TEXT",
                "voided_by": "INTEGER REFERENCES users(id)",
                "void_reason": "TEXT",
            })
            self.ensure_columns(conn, "visas", {
                "visa_sector": "TEXT",
            })
            self.ensure_columns(conn, "offers", {
                "document_type": "TEXT NOT NULL DEFAULT 'Offer'",
                "original_filename": "TEXT",
                "uploaded_at": "TEXT",
            })
            conn.execute(
                """UPDATE offers
                   SET document_type='SignedOffer'
                   WHERE COALESCE(document_type,'') IN ('', 'Offer')
                     AND COALESCE(signed_offer_file_path,'') <> ''
                     AND COALESCE(offer_status,'') = 'signed'"""
            )
            conn.execute(
                """UPDATE offers
                   SET document_type='OfferAcceptance'
                   WHERE COALESCE(document_type,'') IN ('', 'Offer')
                     AND COALESCE(signed_offer_file_path,'') <> ''
                     AND COALESCE(offer_status,'') = 'accepted'"""
            )
            conn.execute(
                """UPDATE offers
                   SET original_filename=(
                       SELECT sd.original_name FROM student_documents sd
                       WHERE sd.file_path=offers.offer_file_path
                       ORDER BY sd.id DESC LIMIT 1
                   )
                   WHERE COALESCE(original_filename,'') = ''
                     AND COALESCE(offer_file_path,'') <> ''
                     AND EXISTS (
                       SELECT 1 FROM student_documents sd WHERE sd.file_path=offers.offer_file_path
                     )"""
            )
            conn.execute(
                """UPDATE offers
                   SET uploaded_at=COALESCE((
                       SELECT sd.uploaded_at FROM student_documents sd
                       WHERE sd.file_path=offers.offer_file_path
                       ORDER BY sd.id DESC LIMIT 1
                   ), created_at)
                   WHERE COALESCE(uploaded_at,'') = ''"""
            )
            self.ensure_columns(conn, "coe_oshc", {
                "coe_provider": "TEXT",
                "coe_course": "TEXT",
                "coe_course_level": "TEXT",
                "letter_language_course_name": "TEXT",
                "letter_language_start_date": "TEXT",
                "letter_language_end_date": "TEXT",
                "letter_main_course_name": "TEXT",
                "letter_main_start_date": "TEXT",
                "letter_main_end_date": "TEXT",
                "letter_year_level": "TEXT",
                "caaw_welfare_start_date": "TEXT",
                "caaw_welfare_end_date": "TEXT",
                "oshc_company_name": "TEXT",
                "oshc_policy_number": "TEXT",
                "oshc_policy_issue_date": "TEXT",
                "oshc_commencement_date": "TEXT",
                "oshc_expiry_date": "TEXT",
                "receipt_amount": "TEXT",
                "receipt_date": "TEXT",
                "payment_reference": "TEXT",
            })
            self.ensure_columns(conn, "contracts", {
                "contract_uid": "TEXT",
                "is_standalone_contract": "INTEGER NOT NULL DEFAULT 0",
                "standalone_data_json": "TEXT",
                "signed_file_path": "TEXT",
                "signed_at": "TEXT",
                "final_signed_file_path": "TEXT",
                "fully_executed_at": "TEXT",
                "parent_signer_name": "TEXT",
                "parent_phone": "TEXT",
                "parent_email": "TEXT",
                "parent_address": "TEXT",
                "notes": "TEXT",
                "deleted_at": "TEXT",
                "deleted_by": "INTEGER REFERENCES users(id)",
                "deleted_reason_code": "TEXT",
                "deleted_reason": "TEXT",
                "voided_at": "TEXT",
                "voided_by": "INTEGER REFERENCES users(id)",
                "void_reason_code": "TEXT",
                "void_reason": "TEXT",
                "superseded_by_contract_id": "INTEGER REFERENCES contracts(id)",
            })
            self.ensure_columns(conn, "contract_signing_links", {
                "updated_at": "TEXT",
            })
            conn.execute("CREATE INDEX IF NOT EXISTS idx_contract_signing_links_contract ON contract_signing_links(contract_id, status)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_contract_signing_links_token_hash ON contract_signing_links(token_hash)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_contract_signing_logs_link ON contract_signing_logs(link_id, event_at DESC)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_contract_signatures_contract ON contract_signatures(contract_id, signed_at DESC)")
            self.ensure_columns(conn, "contract_versions", {
                "student_id": "TEXT REFERENCES students(student_id)",
                "version_label": "TEXT",
                "file_path_pdf": "TEXT",
                "parent_request_text": "TEXT",
                "change_summary": "TEXT",
            })
            self.ensure_columns(conn, "contract_sections", {
                "source_type": "TEXT",
                "updated_by": "INTEGER REFERENCES users(id)",
            })
            self.ensure_columns(conn, "contract_revision_requests", {
                "requested_by_name": "TEXT",
                "risk_warning": "TEXT",
                "created_at": "TEXT",
                "updated_at": "TEXT",
            })
            self.ensure_columns(conn, "contract_revision_changes", {
                "contract_id": "INTEGER REFERENCES contracts(id) ON DELETE CASCADE",
                "version_id": "INTEGER REFERENCES contract_versions(id) ON DELETE CASCADE",
                "section_title": "TEXT",
                "old_content": "TEXT",
                "new_content": "TEXT",
                "change_reason": "TEXT",
                "changed_by": "INTEGER REFERENCES users(id)",
                "changed_at": "TEXT",
            })
            self.ensure_columns(conn, "content_topics", {
                "content_year": "INTEGER",
                "source_type": "TEXT",
                "source_reference": "TEXT",
                "tags_json": "TEXT",
            })
            self.ensure_columns(conn, "content_scripts", {
                "review_status": "TEXT",
                "version_number": "INTEGER NOT NULL DEFAULT 1",
                "source_type": "TEXT",
                "recorded_at": "TEXT",
                "published_at": "TEXT",
                "hook": "TEXT",
                "source_url": "TEXT",
                "original_text": "TEXT",
                "cleaned_text": "TEXT",
                "viral_analysis_json": "TEXT",
                "rewrite_direction": "TEXT",
                "planned_recording_date": "TEXT",
                "planned_publish_date": "TEXT",
                "actual_publish_date": "TEXT",
                "platform": "TEXT DEFAULT 'Xiaohongshu'",
                "source_extraction_job_id": "INTEGER REFERENCES content_extraction_jobs(id)",
                "confirmed_at": "TEXT",
                "review_summary": "TEXT",
                "next_topic_recommendations": "TEXT",
                "second_rewrite_suggestions": "TEXT",
            })
            self.ensure_columns(conn, "content_script_versions", {
                "version_number": "INTEGER NOT NULL DEFAULT 1",
            })
            self.ensure_columns(conn, "content_publish_plans", {
                "planned_publish_time": "TEXT",
                "platform": "TEXT DEFAULT 'Xiaohongshu'",
                "account_name": "TEXT",
                "topic_id": "INTEGER REFERENCES content_topics(id)",
                "publish_status": "TEXT",
                "responsible_user": "TEXT",
                "reminder_enabled": "INTEGER NOT NULL DEFAULT 0",
                "notes": "TEXT",
            })
            self.ensure_columns(conn, "content_published_posts", {
                "topic_id": "INTEGER REFERENCES content_topics(id)",
                "account_name": "TEXT",
                "post_title": "TEXT",
                "notes": "TEXT",
            })
            self.ensure_columns(conn, "content_performance_metrics", {
                "saves": "INTEGER NOT NULL DEFAULT 0",
                "shares": "INTEGER NOT NULL DEFAULT 0",
                "private_messages": "INTEGER NOT NULL DEFAULT 0",
                "new_wechat_contacts": "INTEGER NOT NULL DEFAULT 0",
                "consultation_calls": "INTEGER NOT NULL DEFAULT 0",
                "signed_clients": "INTEGER NOT NULL DEFAULT 0",
                "lead_quality": "TEXT",
                "conversion_result": "TEXT",
                "review_notes": "TEXT",
                "notes": "TEXT",
            })
            self.ensure_columns(conn, "content_materials", {
                "body": "TEXT",
                "category": "TEXT",
                "tags_json": "TEXT",
            })
            self.ensure_columns(conn, "content_hooks", {
                "hook_type": "TEXT",
                "use_case": "TEXT",
                "performance_notes": "TEXT",
                "tags_json": "TEXT",
                "created_by": "INTEGER REFERENCES users(id)",
            })
            self.ensure_columns(conn, "content_extraction_jobs", {
                "note_id": "TEXT",
                "xsec_token": "TEXT",
                "image_urls_json": "TEXT NOT NULL DEFAULT '[]'",
                "comment_count": "INTEGER",
                "like_count": "INTEGER",
                "share_count": "INTEGER",
                "tikhub_raw_json": "TEXT",
                "tikhub_status": "TEXT",
                "tikhub_error": "TEXT",
                "speech_transcript": "TEXT",
                "speech_transcript_with_timestamps": "TEXT",
                "subtitle_ocr_text": "TEXT",
                "subtitle_ocr_frames_count": "INTEGER NOT NULL DEFAULT 0",
                "original_speech_text": "TEXT",
                "original_subtitle_text": "TEXT",
                "merged_original_text": "TEXT",
                "cleaned_text": "TEXT",
                "extraction_confidence": "REAL NOT NULL DEFAULT 0",
                "needs_manual_review": "INTEGER NOT NULL DEFAULT 0",
                "manual_review_reason": "TEXT",
                "calibrated_text": "TEXT",
                "calibration_notes_json": "TEXT",
                "low_confidence_segments_json": "TEXT",
                "ocr_completeness": "REAL NOT NULL DEFAULT 0",
                "asr_completeness": "REAL NOT NULL DEFAULT 0",
                "ocr_asr_similarity": "REAL NOT NULL DEFAULT 0",
                "calibration_confirmed_at": "TEXT",
                "calibration_confirmed_text": "TEXT",
                "main_topic": "TEXT",
                "hook": "TEXT",
                "pain_points": "TEXT",
                "conflict_points": "TEXT",
                "emotional_points": "TEXT",
                "contrarian_view": "TEXT",
                "case_usage": "TEXT",
                "content_structure": "TEXT",
                "viral_reason": "TEXT",
                "reusable_sentences": "TEXT",
                "reusable_structure": "TEXT",
                "rewrite_direction": "TEXT",
                "rewritten_titles": "TEXT",
                "rewritten_script": "TEXT",
                "rewritten_hook": "TEXT",
                "suggested_tags": "TEXT",
                "key_message": "TEXT",
                "status": "TEXT NOT NULL DEFAULT '已提取'",
                "category": "TEXT",
                "tags": "TEXT",
                "planned_recording_date": "TEXT",
                "planned_publish_date": "TEXT",
                "actual_publish_date": "TEXT",
                "is_in_script_library": "INTEGER NOT NULL DEFAULT 0",
                "confirmed_at": "TEXT",
                "confirmed_title": "TEXT",
                "confirmed_script": "TEXT",
                "confirmed_hook": "TEXT",
                "confirmed_tags": "TEXT",
                "review_notes": "TEXT",
                "view_count": "INTEGER NOT NULL DEFAULT 0",
                "favorite_count": "INTEGER NOT NULL DEFAULT 0",
                "private_message_count": "INTEGER NOT NULL DEFAULT 0",
                "wechat_add_count": "INTEGER NOT NULL DEFAULT 0",
                "lead_quality": "TEXT",
                "conversion_result": "TEXT",
                "review_summary": "TEXT",
                "next_topic_recommendations": "TEXT",
                "second_rewrite_suggestions": "TEXT",
                "reviewed_at": "TEXT",
            })
            self.ensure_columns(conn, "content_import_staging", {
                "suggested_category": "TEXT",
                "confirmed_year": "INTEGER",
                "confirmed_category": "TEXT",
                "reviewed_by": "INTEGER",
                "reviewed_at": "TEXT",
                "review_status": "TEXT NOT NULL DEFAULT 'Needs Review'",
                "duplicate_review_status": "TEXT NOT NULL DEFAULT 'Needs Review'",
                "duplicate_review_action": "TEXT",
                "duplicate_review_notes": "TEXT",
                "duplicate_reviewed_by": "INTEGER",
                "duplicate_reviewed_at": "TEXT",
                "duplicate_primary_script_id": "INTEGER",
                "duplicate_primary_staging_id": "INTEGER",
                "duplicate_skip_reason": "TEXT",
                "resolved_duplicate": "INTEGER NOT NULL DEFAULT 0",
                "final_imported_at": "TEXT",
                "final_import_batch_id": "TEXT",
                "final_import_topic_id": "INTEGER",
                "final_import_script_id": "INTEGER",
                "skipped_existing_import": "INTEGER NOT NULL DEFAULT 0",
            })
            self.ensure_columns(conn, "schools", {
                "school_id": "TEXT",
                "school_name_english": "TEXT",
                "school_name_display": "TEXT",
                "region": "TEXT",
                "suburb_id": "INTEGER REFERENCES school_suburbs(id)",
                "suburb_data_status": "TEXT NOT NULL DEFAULT 'Not Started'",
                "suburb_data_last_updated": "TEXT",
                "logo_updated_at": "TEXT",
                "logo_url": "TEXT",
                "logo_source_type": "TEXT",
                "logo_confidence_score": "REAL",
                "logo_status": "TEXT NOT NULL DEFAULT 'missing'",
                "logo_checked_at": "TEXT",
                "logo_notes": "TEXT",
                "google_image_match_url": "TEXT",
                "google_image_match_source": "TEXT",
                "google_cross_check_status": "TEXT",
                "source_url": "TEXT",
                "source_file": "TEXT",
                "extracted_at": "TEXT",
                "extracted_by": "INTEGER REFERENCES users(id)",
                "confidence_score": "REAL",
                "reviewed_by": "INTEGER REFERENCES users(id)",
                "reviewed_at": "TEXT",
                "last_updated_by": "INTEGER REFERENCES users(id)",
                "last_updated_at": "TEXT",
                "accommodation_type": "TEXT NOT NULL DEFAULT 'Unknown'",
                "accommodation_notes": "TEXT",
                "international_year_level_min": "TEXT NOT NULL DEFAULT 'Unknown'",
                "international_year_level_max": "TEXT NOT NULL DEFAULT 'Unknown'",
                "accepted_international_year_levels": "TEXT NOT NULL DEFAULT '[]'",
                "accepted_international_year_level_pattern": "TEXT NOT NULL DEFAULT 'Unknown'",
                "international_year_level_notes": "TEXT",
                "has_on_campus_language_program": "TEXT NOT NULL DEFAULT 'Unknown'",
                "language_program_type": "TEXT NOT NULL DEFAULT 'Unknown'",
                "language_program_notes": "TEXT",
                "school_gender_type": "TEXT NOT NULL DEFAULT 'Coeducational'",
                "school_gender_notes": "TEXT",
                "state_senior_certificate": "TEXT NOT NULL DEFAULT 'Unknown'",
                "offers_ib": "TEXT NOT NULL DEFAULT 'No'",
                "ib_programmes": "TEXT NOT NULL DEFAULT '[]'",
                "ib_notes": "TEXT",
                "aeas_required": "TEXT NOT NULL DEFAULT 'No'",
                "idat_required": "TEXT NOT NULL DEFAULT 'No'",
                "ielts_required": "TEXT NOT NULL DEFAULT 'No'",
                "academic_test_notes": "TEXT",
                "interview_required": "TEXT NOT NULL DEFAULT 'Unknown'",
                "interview_notes": "TEXT",
                "available_intake_terms": "TEXT NOT NULL DEFAULT '[]'",
                "intake_notes": "TEXT",
                "international_tuition_fee_amount": "TEXT",
                "international_tuition_fee_currency": "TEXT NOT NULL DEFAULT 'AUD'",
                "international_tuition_fee_year": "TEXT",
                "international_tuition_fee_year_level": "TEXT",
                "international_application_fee_amount": "TEXT",
                "international_application_fee_currency": "TEXT NOT NULL DEFAULT 'AUD'",
                "international_application_fee_year": "TEXT",
                "international_application_fee_year_level": "TEXT",
                "fee_notes": "TEXT",
                "fee_source_url": "TEXT",
                "better_education_gaokao_ranking": "TEXT",
                "better_education_gaokao_ranking_year": "TEXT",
                "better_education_gaokao_source_url": "TEXT",
                "teaching_staff": "TEXT",
                "fte_teaching_staff": "TEXT",
                "non_teaching_staff": "TEXT",
                "fte_non_teaching_staff": "TEXT",
                "icsea_value": "TEXT",
                "icsea_average": "TEXT",
                "total_enrolments": "TEXT",
                "male_enrolments": "TEXT",
                "female_enrolments": "TEXT",
                "language_background_other_than_english_pct": "TEXT",
                "myschool_url": "TEXT",
                "myschool_school_id": "TEXT",
                "myschool_last_fetched_at": "TEXT",
                "myschool_fetch_status": "TEXT NOT NULL DEFAULT 'Not Started'",
                "myschool_review_status": "TEXT NOT NULL DEFAULT 'Not Started'",
                "fte_students": "TEXT",
                "icsea_percentile": "TEXT",
                "indigenous_student_percentage": "TEXT",
                "attendance_percentage": "TEXT",
                "school_data_source_url": "TEXT",
                "school_data_updated_at": "TEXT",
                "latitude": "REAL",
                "longitude": "REAL",
                "geocode_status": "TEXT",
                "geocoded_at": "TEXT",
                "deleted_at": "TEXT",
                "deleted_by": "INTEGER REFERENCES users(id)",
            })
            conn.execute(
                """CREATE TABLE IF NOT EXISTS school_logo_candidates (
                   id INTEGER PRIMARY KEY,
                   school_id INTEGER NOT NULL REFERENCES schools(id) ON DELETE CASCADE,
                   candidate_file_path TEXT,
                   source_url TEXT,
                   source_type TEXT,
                   source_label TEXT,
                   source_domain TEXT,
                   confidence_score REAL,
                   logo_status TEXT NOT NULL DEFAULT 'pending_review',
                   google_image_match_url TEXT,
                   google_image_match_source TEXT,
                   google_cross_check_status TEXT,
                   review_status TEXT NOT NULL DEFAULT 'Pending Review',
                   notes TEXT,
                   raw_json TEXT NOT NULL DEFAULT '{}',
                   created_at TEXT NOT NULL,
                   updated_at TEXT NOT NULL,
                   created_by INTEGER REFERENCES users(id),
                   approved_at TEXT,
                   approved_by INTEGER REFERENCES users(id),
                   rejected_at TEXT,
                   rejected_by INTEGER REFERENCES users(id)
                )"""
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_school_logo_candidates_school ON school_logo_candidates(school_id, review_status, confidence_score)")
            conn.execute(
                """CREATE TABLE IF NOT EXISTS school_geocode_cache (
                   id INTEGER PRIMARY KEY,
                   raw_input_text TEXT NOT NULL,
                   normalized_query TEXT NOT NULL,
                   input_type TEXT NOT NULL,
                   state TEXT NOT NULL,
                   formatted_address TEXT,
                   suburb TEXT,
                   postcode TEXT,
                   latitude REAL,
                   longitude REAL,
                   geocode_source TEXT NOT NULL DEFAULT 'seed',
                   geocoded_at TEXT,
                   geocode_status TEXT NOT NULL DEFAULT 'Resolved',
                   candidates_json TEXT NOT NULL DEFAULT '[]',
                   warning_message TEXT,
                   created_at TEXT NOT NULL,
                   updated_at TEXT NOT NULL,
                   UNIQUE(normalized_query,state,input_type)
                )"""
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_school_geocode_cache_lookup ON school_geocode_cache(normalized_query,state,input_type,geocode_status)")
            self.ensure_columns(conn, "school_data_import_rows", {
                "region_raw": "TEXT",
            })
            conn.execute(
                """CREATE TABLE IF NOT EXISTS school_suburbs (
                   id INTEGER PRIMARY KEY, suburb TEXT NOT NULL, state TEXT NOT NULL, postcode TEXT,
                   country TEXT NOT NULL DEFAULT 'Australia', normalized_suburb_name TEXT NOT NULL,
                   created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                   UNIQUE(normalized_suburb_name,state,postcode)
                )"""
            )
            conn.execute(
                """CREATE TABLE IF NOT EXISTS school_suburb_data_sources (
                   id INTEGER PRIMARY KEY, source_name TEXT NOT NULL, source_url TEXT,
                   source_type TEXT NOT NULL DEFAULT 'Manual', enabled INTEGER NOT NULL DEFAULT 1,
                   notes TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                )"""
            )
            conn.execute(
                """CREATE TABLE IF NOT EXISTS school_suburb_data (
                   id INTEGER PRIMARY KEY, suburb_id INTEGER NOT NULL REFERENCES school_suburbs(id) ON DELETE CASCADE,
                   data_year TEXT, source_name TEXT, source_url TEXT, source_type TEXT,
                   fetched_at TEXT, fetched_by INTEGER REFERENCES users(id),
                   reviewed_status TEXT NOT NULL DEFAULT 'Needs Review',
                   reviewed_by INTEGER REFERENCES users(id), reviewed_at TEXT,
                   confidence_score REAL,
                   population_total INTEGER, population_male INTEGER, population_female INTEGER, median_age REAL,
                   family_count INTEGER, couple_family_count INTEGER, single_parent_family_count INTEGER,
                   average_household_size REAL, household_count INTEGER,
                   median_personal_income_weekly INTEGER, median_family_income_weekly INTEGER,
                   median_household_income_weekly INTEGER,
                   english_only_percentage REAL, non_english_language_percentage REAL, top_languages_json TEXT,
                   bachelor_or_above_percentage REAL, year_12_completion_percentage REAL, education_summary TEXT,
                   born_overseas_percentage REAL, top_birth_countries_json TEXT, ancestry_summary TEXT,
                   separate_house_percentage REAL, apartment_percentage REAL, rented_percentage REAL, owned_percentage REAL,
                   geography_type TEXT, geography_code TEXT, retrieved_at TEXT, raw_json TEXT,
                   other_indicators_json TEXT, notes TEXT, error_message TEXT,
                   created_at TEXT NOT NULL, updated_at TEXT NOT NULL, updated_by INTEGER REFERENCES users(id)
                )"""
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_schools_suburb ON schools(suburb_id, suburb_data_status)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_schools_state_region ON schools(state, region, school_name)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_school_suburbs_lookup ON school_suburbs(state, postcode, normalized_suburb_name)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_school_suburb_data_suburb ON school_suburb_data(suburb_id, reviewed_status, data_year)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_school_suburb_sources_enabled ON school_suburb_data_sources(enabled, source_type)")
            conn.execute("UPDATE schools SET school_gender_type='Coeducational' WHERE COALESCE(school_gender_type,'')=''")
            conn.execute("UPDATE schools SET suburb_data_status='Not Started' WHERE COALESCE(suburb_data_status,'')=''")
            conn.execute("UPDATE schools SET accommodation_type='Unknown' WHERE COALESCE(accommodation_type,'')=''")
            conn.execute("UPDATE schools SET international_year_level_min='Unknown' WHERE COALESCE(international_year_level_min,'')=''")
            conn.execute("UPDATE schools SET international_year_level_max='Unknown' WHERE COALESCE(international_year_level_max,'')=''")
            conn.execute("UPDATE schools SET accepted_international_year_levels='[]' WHERE COALESCE(accepted_international_year_levels,'')=''")
            conn.execute("UPDATE schools SET accepted_international_year_level_pattern='Unknown' WHERE COALESCE(accepted_international_year_level_pattern,'')=''")
            conn.execute("UPDATE schools SET has_on_campus_language_program='Unknown' WHERE COALESCE(has_on_campus_language_program,'')=''")
            conn.execute("UPDATE schools SET language_program_type='Unknown' WHERE COALESCE(language_program_type,'')=''")
            conn.execute("UPDATE schools SET state_senior_certificate='Unknown' WHERE COALESCE(state_senior_certificate,'')=''")
            conn.execute("UPDATE schools SET offers_ib='No' WHERE COALESCE(offers_ib,'')=''")
            conn.execute("UPDATE schools SET aeas_required='No' WHERE COALESCE(aeas_required,'')=''")
            conn.execute("UPDATE schools SET idat_required='No' WHERE COALESCE(idat_required,'')=''")
            conn.execute("UPDATE schools SET ielts_required='No' WHERE COALESCE(ielts_required,'')=''")
            conn.execute("UPDATE schools SET interview_required='Unknown' WHERE COALESCE(interview_required,'')=''")
            conn.execute("UPDATE schools SET available_intake_terms='[]' WHERE COALESCE(available_intake_terms,'')=''")
            conn.execute("UPDATE schools SET international_tuition_fee_currency='AUD' WHERE COALESCE(international_tuition_fee_currency,'')=''")
            conn.execute("UPDATE schools SET international_application_fee_currency='AUD' WHERE COALESCE(international_application_fee_currency,'')=''")
            conn.execute("UPDATE schools SET icsea_average='1000' WHERE COALESCE(icsea_average,'')='' AND COALESCE(icsea_value,'')<>''")
            conn.execute("UPDATE schools SET ib_programmes='[]' WHERE COALESCE(ib_programmes,'')=''")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_contracts_uid ON contracts(contract_uid)")
            feature_flag_values = {
                row["key"]: row["value"]
                for row in conn.execute(
                    """SELECT key,value FROM system_settings
                       WHERE key IN (
                         'STUDENT_CASES_READ_ENABLED',
                         'TRANSFER_CASES_WRITE_ENABLED',
                         'PROGRESSION_CASES_WRITE_ENABLED',
                         'EXTERNAL_TRANSFER_AUTO_CREATE_ENABLED',
                         'CASE_COMPLETION_ENABLED'
                       )"""
                ).fetchall()
            }
            feature_flags = read_student_case_feature_flags(
                lambda key: feature_flag_values.get(key)
            )
            for name, permissions in ROLE_PERMISSIONS.items():
                existing_role = conn.execute(
                    "SELECT id,permissions_json FROM roles WHERE name=?",
                    (name,),
                ).fetchone()
                if existing_role is None:
                    conn.execute(
                        """INSERT INTO roles(name,permissions_json)
                           VALUES(?,?)""",
                        (name, json.dumps(permissions)),
                    )
                    print(f"Role permissions initialized: {name}")
                    continue
                # Validate existing authority, but defaults never regrant a revoked role permission.
                reconciled_role_permissions(
                    name,
                    existing_role["permissions_json"],
                    permissions,
                    feature_flags=feature_flags,
                )
            for existing_role in conn.execute(
                "SELECT name,permissions_json FROM roles ORDER BY id"
            ).fetchall():
                if existing_role["name"] in ROLE_PERMISSIONS:
                    continue
                reconciled_role_permissions(
                    existing_role["name"],
                    existing_role["permissions_json"],
                    [],
                    feature_flags=feature_flags,
                )
            for module_key, module_name_en, module_name_zh, route_path, sidebar_order, _category in MODULE_DEFINITIONS:
                conn.execute(
                    """INSERT INTO modules(module_key,module_name_en,module_name_zh,route_path,sidebar_order,is_active)
                       VALUES(?,?,?,?,?,1)
                       ON CONFLICT(module_key) DO NOTHING""",
                    (module_key, module_name_en, module_name_zh, route_path, sidebar_order),
                )
            admin = conn.execute("SELECT id FROM users WHERE username='admin'").fetchone()
            if not admin:
                role = conn.execute("SELECT id FROM roles WHERE name='Admin'").fetchone()
                conn.execute(
                    """INSERT INTO users(username, display_name, password_hash, role_id, is_active,
                       must_change_password, created_at) VALUES(?,?,?,?,1,1,?)""",
                    ("admin", "System Admin", password_hash("ChangeMe!2026"), role["id"], now_iso()),
                )
            new_permission_ids = self.ensure_default_module_permissions(conn)
            self.ensure_permission_consistency(conn, permission_ids=new_permission_ids)
            self.ensure_workflow_action_permissions(conn, permission_ids=new_permission_ids)
            self.ensure_file_manager_action_permissions(conn, permission_ids=new_permission_ids)
            self.ensure_reminder_action_permissions(conn, permission_ids=new_permission_ids)
            self.ensure_school_action_permissions(conn, permission_ids=new_permission_ids)
            self.ensure_school_plan_report_action_permissions(conn, permission_ids=new_permission_ids)
            self.ensure_school_library_seed(conn)
            self.ensure_school_feature_tag_seed(conn)
            self.ensure_school_suburb_seed(conn)
            self.ensure_data_update_centre_seed(conn)
            self.ensure_state_fee_schedule_seed(conn)
            defaults = {
                "mode": "test",
                "test_root": "/Volumes/Server/Students/CRM_TEST",
                "production_root": "/Volumes/Server/Students/2026",
                "production_enabled": "false",
                "mvp_production_locked": "true",
                "PARENT_FORM_PUBLIC_BASE_URL": "http://127.0.0.1:8781",
                "CRM_PUBLIC_BASE_URL": "https://crm.goabroad.com.au",
                "CRM_ADMIN_LAN_URL": "http://192.168.1.190:8780",
                "CRM_ADMIN_BIND_HOST": "0.0.0.0",
                "auto_backup_enabled": "true",
                "auto_backup_time": "23:30",
                "backup_retention_count": "30",
                "auto_cleanup_enabled": "false",
                "last_auto_backup_date": "",
                "contract_signature_page": "last",
                "contract_signature_x": "220",
                "contract_signature_y": "250",
                "contract_signature_width": "130",
                "contract_stamp_page": "last",
                "contract_stamp_x": "260",
                "contract_stamp_y": "228",
                "contract_stamp_width": "90",
                "contract_company_date_page": "last",
                "contract_company_date_x": "185",
                "contract_company_date_y": "185",
                "contract_company_date_font_size": "10",
                "contract_service_fee_StudyConsultingAndVisaService": "",
                "contract_service_fee_StudyConsultingService": "",
                "contract_service_fee_VisaConsultingAndDocumentGuidance": "",
                "contract_service_fee_OtherConsultingService": "",
            }
            for key, value in defaults.items():
                conn.execute(
                    "INSERT OR IGNORE INTO system_settings(key,value,updated_at) VALUES(?,?,?)",
                    (key, value, now_iso()),
                )
            reminder_defaults = {
                "reminder_contract_enabled": "true",
                "reminder_contract_days_after_sent": "3",
                "reminder_contract_repeat_every_days": "2",
                "reminder_offer_enabled": "true",
                "reminder_offer_days_before_expiry": "14,7,3,1",
                "reminder_oshc_enabled": "true",
                "reminder_oshc_days_before_expiry": "30",
                "reminder_visa_enabled": "true",
                "reminder_visa_days_before_expiry": "180,90,30,14",
                "reminder_year_transition_enabled": "true",
                "reminder_year_transition_level": "Year 6",
                "reminder_year_transition_months_before": "6,3",
                "reminder_calculation_start_at": "",
                "reminder_calculation_start_date": "",
            }
            for key, value in reminder_defaults.items():
                conn.execute(
                    "INSERT OR IGNORE INTO system_settings(key,value,updated_at) VALUES(?,?,?)",
                    (key, value, now_iso()),
                )
            default_refund = (
                "签证拒签后不再继续申请退款规则",
                "visa_refusal_no_further_application",
                "如乙方签证申请被拒签，且乙方明确表示不再继续申请，甲方可从乙方已支付的留学咨询及签证服务费中扣除 AUD$ {{refusal_deduction_amount}}，并向乙方退还剩余部分。",
                "AUD", "900", "AUD", "", "", now_iso(), now_iso(),
            )
            conn.execute(
                """INSERT OR IGNORE INTO contract_refund_templates(
                   template_name,refund_policy_type,refund_policy_text,refusal_deduction_currency,
                   refusal_deduction_amount,refusal_refund_currency,refusal_refund_amount,refund_notes,
                   created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                default_refund,
            )
            school_failure_refund = (
                "申请学校失败规则",
                "school_application_failure",
                "如经双方同意的申请学校方案中，学生没有被任何一所学校录取，费用全退。",
                "AUD", "", "AUD", "", "", now_iso(), now_iso(),
            )
            conn.execute(
                """INSERT OR IGNORE INTO contract_refund_templates(
                   template_name,refund_policy_type,refund_policy_text,refusal_deduction_currency,
                   refusal_deduction_amount,refusal_refund_currency,refusal_refund_amount,refund_notes,
                   created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                school_failure_refund,
            )

    def ensure_state_fee_schedule_seed(self, conn):
        stamp = now_iso()
        conn.execute(
            """CREATE TABLE IF NOT EXISTS state_fee_schedules (
              id INTEGER PRIMARY KEY, state TEXT NOT NULL, sector TEXT NOT NULL,
              schedule_name TEXT NOT NULL, source_name TEXT, source_url TEXT,
              effective_year INTEGER NOT NULL, effective_from_date TEXT, effective_to_date TEXT,
              is_active INTEGER NOT NULL DEFAULT 0, notes TEXT,
              created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
              UNIQUE(state,sector,effective_year,schedule_name)
            )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS state_fee_items (
              id INTEGER PRIMARY KEY, schedule_id INTEGER NOT NULL REFERENCES state_fee_schedules(id) ON DELETE CASCADE,
              category TEXT NOT NULL, fee_name TEXT NOT NULL, school_level TEXT,
              year_level_from TEXT, year_level_to TEXT,
              amount REAL, amount_min REAL, amount_max REAL, currency TEXT NOT NULL DEFAULT 'AUD',
              billing_frequency TEXT, is_mandatory INTEGER NOT NULL DEFAULT 0,
              notes TEXT, sort_order INTEGER NOT NULL DEFAULT 0,
              created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            )"""
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_state_fee_schedules_lookup ON state_fee_schedules(state, sector, effective_year, is_active)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_state_fee_items_schedule ON state_fee_items(schedule_id, category, sort_order)")
        def seed_schedule(state, schedule_name, source_name, notes, items):
            existing = conn.execute(
                "SELECT id FROM state_fee_schedules WHERE state=? AND sector='Government' AND effective_year=2026 AND schedule_name=?",
                (state, schedule_name),
            ).fetchone()
            if existing:
                return
            conn.execute(
                """INSERT INTO state_fee_schedules(
                   state,sector,schedule_name,source_name,source_url,effective_year,
                   effective_from_date,effective_to_date,is_active,notes,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(state,sector,effective_year,schedule_name) DO NOTHING""",
                (
                    state,
                    "Government",
                    schedule_name,
                    source_name,
                    "",
                    2026,
                    "2026-01-01",
                    "2026-12-31",
                    1,
                    notes,
                    stamp,
                    stamp,
                ),
            )
            schedule = conn.execute(
                "SELECT id FROM state_fee_schedules WHERE state=? AND sector='Government' AND effective_year=2026 AND schedule_name=?",
                (state, schedule_name),
            ).fetchone()
            if not schedule:
                return
            schedule_id = schedule["id"]
            active_count = conn.execute(
                "SELECT COUNT(*) AS c FROM state_fee_schedules WHERE state=? AND sector='Government' AND is_active=1",
                (state,),
            ).fetchone()["c"]
            if not active_count:
                conn.execute("UPDATE state_fee_schedules SET is_active=1,updated_at=? WHERE id=?", (stamp, schedule_id))
            item_count = conn.execute("SELECT COUNT(*) AS c FROM state_fee_items WHERE schedule_id=?", (schedule_id,)).fetchone()["c"]
            if item_count:
                return
            conn.executemany(
                """INSERT INTO state_fee_items(
                   schedule_id,category,fee_name,school_level,year_level_from,year_level_to,
                   amount,amount_min,amount_max,currency,billing_frequency,is_mandatory,notes,sort_order,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                [(schedule_id, *item, stamp, stamp) for item in items],
            )

        vic_items = [
            ("tuition", "Primary Tuition", "Primary", "Prep", "Grade 6", 16000, None, None, "AUD", "per annum", 1, "", 10),
            ("tuition", "Junior Secondary Tuition", "Junior Secondary", "7", "10", 19600, None, None, "AUD", "per annum", 1, "", 20),
            ("tuition", "Senior Secondary Tuition", "Senior Secondary", "11", "12", 22000, None, None, "AUD", "per annum", 1, "", 30),
            ("special_school", "VCA Junior Secondary Tuition", "VCA Junior Secondary", "7", "10", 26600, None, None, "AUD", "per annum", 0, "Victorian College of the Arts Secondary School special fee.", 40),
            ("special_school", "VCA Senior Secondary Tuition", "VCA Senior Secondary", "11", "12", 29850, None, None, "AUD", "per annum", 0, "Victorian College of the Arts Secondary School special fee.", 50),
            ("admin", "Application Fee", "All", "", "", 300, None, None, "AUD", "one-off", 1, "", 100),
            ("admin", "Arrival Support Fee", "All", "", "", 220, None, None, "AUD", "one-off", 0, "", 110),
            ("admin", "Accommodation Placement Fee", "All", "", "", 352, None, None, "AUD", "one-off", 0, "", 120),
            ("admin", "School Transfer Fee", "All", "", "", 750, None, None, "AUD", "one-off", 0, "", 130),
            ("admin", "Enrolment Amendment Fee", "All", "", "", 300, None, None, "AUD", "one-off", 0, "", 140),
            ("admin", "Default Administration Fee", "All", "", "", 500, None, None, "AUD", "one-off", 0, "", 150),
            ("admin", "Refund Administration Fee", "All", "", "", 500, None, None, "AUD", "one-off", 0, "", 160),
            ("accommodation", "Homestay Fee", "All", "", "", None, 370, 500, "AUD", "per week", 0, "", 200),
            ("extra", "VET-related material fees", "All", "", "", None, 60, 950, "AUD", "per subject", 0, "", 300),
            ("extra", "School uniforms", "All", "", "", None, 600, 1200, "AUD", "every 1-2 years", 0, "", 310),
            ("extra", "School camps", "All", "", "", None, 400, 1000, "AUD", "per annum", 0, "", 320),
            ("extra", "School excursions", "All", "", "", None, 50, 250, "AUD", "per annum", 0, "", 330),
            ("extra", "Textbooks / stationery", "All", "", "", None, 400, 700, "AUD", "per annum", 0, "", 340),
            ("extra", "Electronic device costs", "All", "", "", None, 500, 1500, "AUD", "every 2-3 years", 0, "", 350),
            ("extra", "VCE and VCE VM exam fees", "All", "", "", None, 80, 500, "AUD", "per annum", 0, "", 360),
        ]
        seed_schedule(
            "VIC",
            "VIC Government School Fees 2026",
            "Victorian Government Schools International Student Program",
            "Seeded MVP state fee schedule. Schools with State=VIC and sector=Government/Public reference this active template instead of copying fees per school.",
            vic_items,
        )
        nsw_items = [
            ("admin", "Application Fee", "All", "", "", 300, None, None, "AUD", "one-off", 1, "Required with submission of an application and non-refundable.", 10),
            ("tuition", "Primary Tuition", "Primary", "K", "6", 17000, None, None, "AUD", "per annum", 1, "Tuition fees are calculated on a 40-week school year and charged on a weekly basis.", 100),
            ("tuition", "Junior Secondary Tuition", "Junior Secondary", "7", "10", 18000, None, None, "AUD", "per annum", 1, "Tuition fees are calculated on a 40-week school year and charged on a weekly basis.", 110),
            ("tuition", "Senior Secondary Tuition", "Senior Secondary", "11", "12", 20500, None, None, "AUD", "per annum", 1, "Tuition fees are calculated on a 40-week school year and charged on a weekly basis.", 120),
            ("tuition", "Intensive English Centre", "Intensive English Centre", "", "", 9000, None, None, "AUD", "two terms", 0, "Based on a 10-week term. Fees are charged on a weekly basis.", 130),
            ("tuition_discount", "Sibling/Regional Discounted Primary Tuition", "Primary", "K", "6", 15300, None, None, "AUD", "per annum", 0, "Discounted tuition from NSW fee schedule screenshot.", 200),
            ("tuition_discount", "Sibling/Regional Discounted Junior Secondary Tuition", "Junior Secondary", "7", "10", 16200, None, None, "AUD", "per annum", 0, "Discounted tuition from NSW fee schedule screenshot.", 210),
            ("tuition_discount", "Sibling/Regional Discounted Senior Secondary Tuition", "Senior Secondary", "11", "12", 18450, None, None, "AUD", "per annum", 0, "Discounted tuition from NSW fee schedule screenshot.", 220),
            ("tuition_discount", "Sibling/Regional Discounted Intensive English Centre", "Intensive English Centre", "", "", 8100, None, None, "AUD", "two terms", 0, "Based on a 10-week term. Fees are charged on a weekly basis.", 230),
            ("extra", "School uniforms", "All", "", "", None, 350, 450, "AUD", "estimated", 0, "", 300),
            ("admin", "School transfer", "All", "", "", 850, None, None, "AUD", "one-off", 0, "", 310),
            ("extra", "Overseas Student Health Cover", "All", "", "", 769, None, None, "AUD", "12 months", 0, "Optional.", 320),
            ("accommodation", "Parent nomination for homestay", "All", "", "", 480, None, None, "AUD", "one-off plus monitoring", 0, "A$480 inspection fee plus A$40 per week monitoring fee.", 400),
            ("accommodation", "Homestay placement", "All", "", "", 480, None, None, "AUD", "one-off", 0, "", 410),
            ("accommodation", "Homestay fee", "All", "", "", 520, None, None, "AUD", "per week", 0, "See A$3,500 tuition fee discount offer for homestay students.", 420),
            ("accommodation", "Special dietary requirement", "All", "", "", 50, None, None, "AUD", "per week", 0, "Gluten free, vegan, Targeted Sports Program; may be charged to cover additional costs to the host family.", 430),
            ("airport", "Sydney airport transfer to homestay - Greater Sydney Metropolitan Area", "All", "", "", 235, None, None, "AUD", "one-off", 0, "", 500),
            ("airport", "Sydney airport transfer to homestay - Wollongong", "All", "", "", 325, None, None, "AUD", "one-off", 0, "", 510),
            ("airport", "Sydney airport transfer to homestay - Central Coast", "All", "", "", 325, None, None, "AUD", "one-off", 0, "", 520),
            ("airport", "Sydney airport transfer to homestay - Newcastle", "All", "", "", 390, None, None, "AUD", "one-off", 0, "", 530),
        ]
        seed_schedule(
            "NSW",
            "NSW Government School Fees 2026",
            "NSW Government Schools International Student Program",
            "Seeded MVP state fee schedule from user-provided screenshots. Schools with State=NSW and sector=Government/Public reference this active template instead of copying fees per school.",
            nsw_items,
        )
        sa_items = [
            ("tuition", "Primary Tuition", "Primary", "Reception", "6", 12960, None, None, "AUD", "per annum", 1, "Primary School Program (PSP).", 100),
            ("tuition", "Junior High School Tuition", "Junior High School", "7", "10", 15880, None, None, "AUD", "per annum", 1, "High School Graduate Program (HSGP). May include 1-4 terms of Intensive Secondary English Course (ISEC).", 110),
            ("tuition", "Senior High School Tuition", "Senior High School", "11", "12", 17440, None, None, "AUD", "per annum", 1, "High School Graduate Program (HSGP).", 120),
            ("extra", "Overseas Student Health Cover", "All", "", "", 862, None, None, "AUD", "12 months", 0, "Single cover for 12 months. As at 29 September 2025.", 200),
            ("admin", "Administration and Support Service Fee", "All", "", "", 720, None, None, "AUD", "first year only", 1, "", 210),
            ("admin", "Annual Support Service Fee", "All", "", "", 370, None, None, "AUD", "subsequent years only", 0, "", 220),
            ("accommodation", "Accommodation Services Fee", "HSGP", "", "", 200, None, None, "AUD", "one-off", 0, "Shown in HSGP other fees and charges.", 230),
            ("accommodation", "Homestay Placement Fee", "PSP", "", "", 200, None, None, "AUD", "one-off", 0, "For nominated homestays.", 240),
            ("admin", "Extension Fee", "All", "", "", 200, None, None, "AUD", "one-off", 0, "", 250),
            ("accommodation", "Homestay Changeover Fee", "All", "", "", 200, None, None, "AUD", "one-off", 0, "", 260),
            ("accommodation", "Homestay Fee", "All", "", "", None, 300, 360, "AUD", "per week", 0, "Shared Room AUD 300 per week; Single Room AUD 360 per week.", 300),
            ("extra", "Stationery", "All", "", "", 150, None, None, "AUD", "per year", 0, "", 400),
            ("extra", "School and Sports Uniform", "All", "", "", None, 250, 600, "AUD", "estimated", 0, "Refer to individual school websites.", 410),
            ("extra", "School Excursions", "All", "", "", None, 50, 100, "AUD", "per year", 0, "", 420),
            ("extra", "School Camp", "All", "", "", None, 100, 300, "AUD", "per year", 0, "If offered.", 430),
            ("extra", "Scientific Calculator", "Years 10-11", "10", "11", 50, None, None, "AUD", "one-off", 0, "", 440),
            ("extra", "Graphics Calculator", "Year 12", "12", "12", 200, None, None, "AUD", "one-off", 0, "", 450),
            ("extra", "Laptop / External Hard Drive", "All", "", "", None, None, None, "AUD", "refer to school", 0, "Some schools only; refer to individual school websites for specification details.", 460),
            ("extra", "Special Interest Program Fees", "All", "", "", None, None, None, "AUD", "contact provider", 0, "Contact education.isp@sa.gov.au for further details.", 470),
            ("extra", "SACE Stage 1 Student Fee", "Stage 1", "10", "12", 517, None, None, "AUD", "annual non-refundable", 0, "South Australian Certificate of Education (SACE) student fee. Current as at July 2025 and subject to change.", 500),
            ("extra", "SACE Stage 2 Student Fee", "Stage 2", "10", "12", 1138, None, None, "AUD", "annual non-refundable", 0, "South Australian Certificate of Education (SACE) student fee. Current as at July 2025 and subject to change.", 510),
            ("special_school", "International Baccalaureate Diploma Program Fees", "IBDP", "", "", None, None, None, "AUD", "set by school", 0, "Fees for the IBDP are set by individual schools; contact education.isp@sa.gov.au for further details.", 520),
            ("extra", "Mobile Phone", "All", "", "", None, 30, 60, "AUD", "per month", 0, "Cost of living estimate.", 600),
            ("extra", "Personal expenses, clothing, travel and entertainment", "All", "", "", None, 350, 450, "AUD", "per month", 0, "Cost of living estimate.", 610),
            ("extra", "Local transport to and from school", "All", "", "", 15, None, None, "AUD", "per week", 0, "Cost of living estimate. Student concession.", 620),
            ("accommodation", "Homestay Holding Fee", "All", "", "", None, None, None, "AUD", "contact provider", 0, "If absent from homestay accommodation on holidays; contact education.isp@sa.gov.au for further details.", 630),
        ]
        seed_schedule(
            "SA",
            "SA Government School Fees 2026",
            "South Australian Government Schools International Student Program",
            "Seeded MVP state fee schedule from user-provided screenshots. Schools with State=SA and sector=Government/Public reference this active template instead of copying fees per school.",
            sa_items,
        )
        act_items = [
            ("tuition", "Preschool Tuition", "Preschool", "Preschool", "Preschool", 4200, None, None, "AUD", "per annum", 1, "Annual fee from user-provided ACT Government school fees screenshot. Fees may change.", 100),
            ("tuition", "Primary School Tuition", "Primary", "Kindergarten", "6", 11100, None, None, "AUD", "per annum", 1, "Kindergarten to Year 6. Annual fee from user-provided ACT Government school fees screenshot. Fees may change.", 110),
            ("tuition", "High School Tuition", "High School", "7", "10", 14500, None, None, "AUD", "per annum", 1, "Years 7 to 10. Annual fee from user-provided ACT Government school fees screenshot. Fees may change.", 120),
            ("tuition", "College Tuition", "College", "11", "12", 16200, None, None, "AUD", "per annum", 1, "Years 11 to 12. Annual fee from user-provided ACT Government school fees screenshot. Fees may change.", 130),
        ]
        seed_schedule(
            "ACT",
            "ACT Government School Fees 2026",
            "ACT Government Education International",
            "Seeded MVP state fee schedule from user-provided screenshot. Only visible annual tuition fees were entered; other fees can be added later in School Information Collection Center.",
            act_items,
        )
        qld_items = [
            ("tuition", "Primary School Program - Per Year", "Primary", "Prep", "6", 15176, None, None, "AUD", "per annum", 1, "Primary school program. CRICOS code 082519A; Years Prep to Year 6. 2026 fee from user-provided Queensland Government schools screenshot.", 100),
            ("tuition", "Primary School Program - 3 Terms", "Primary", "Prep", "6", 11382, None, None, "AUD", "3 terms", 0, "Primary school program 3-term option. 2026 fee from user-provided Queensland Government schools screenshot.", 101),
            ("tuition", "Primary School Program - 2 Terms", "Primary", "Prep", "6", 7588, None, None, "AUD", "2 terms", 0, "Primary school program 2-term option. 2026 fee from user-provided Queensland Government schools screenshot.", 102),
            ("tuition", "Junior High School Program", "Junior High School", "7", "10", 16612, None, None, "AUD", "per annum", 1, "CRICOS code 082520G; Years 7 to 10. 2026 fee from user-provided Queensland Government schools screenshot.", 110),
            ("tuition", "Senior High School Program", "Senior High School", "11", "12", 18776, None, None, "AUD", "per annum", 1, "CRICOS code 082521G; Years 11 and 12. 2026 fee from user-provided Queensland Government schools screenshot.", 120),
            ("tuition", "High School Preparation Program", "High School Preparation", "", "", 507, None, None, "AUD", "per week", 0, "CRICOS code 087993A; Years N/A. 2026 fee from user-provided Queensland Government schools screenshot.", 130),
            ("special_school", "International Baccalaureate Diploma", "IB Diploma", "11", "12", 21458, None, None, "AUD", "per annum", 0, "CRICOS code 079324E; Years 11 and 12. 2026 fee from user-provided Queensland Government schools screenshot.", 200),
            ("special_school", "International Baccalaureate Diploma - 2 Terms", "IB Diploma", "11", "12", 11197, None, None, "AUD", "2 terms", 0, "International Baccalaureate Diploma 2-term option. 2026 fee from user-provided Queensland Government schools screenshot.", 201),
            ("special_school", "Queensland Academies International Baccalaureate", "Queensland Academies IB", "10", "12", 26574, None, None, "AUD", "per annum", 0, "CRICOS code 082520G (Year 10) and 079324E (Years 11 to 12). 2026 fee from user-provided Queensland Government schools screenshot.", 210),
        ]
        seed_schedule(
            "QLD",
            "QLD Government School Fees 2026",
            "Education Queensland International",
            "Seeded MVP state fee schedule from user-provided screenshots. Schools with State=QLD and sector=Government/Public reference this active template instead of copying fees per school. 2026 values were entered; visible 2027/2028 values can be added as future schedules later if needed.",
            qld_items,
        )
        tas_items = [
            ("tuition", "Primary Tuition", "Primary", "Prep", "6", 14400, None, None, "AUD", "per annum", 1, "Primary Prep-Year 6. 2026 annual fee from user-provided Tasmania Government schools screenshot.", 100),
            ("tuition", "Primary Tuition Weekly", "Primary", "Prep", "6", 390, None, None, "AUD", "per week", 0, "Primary Prep-Year 6. 2026 weekly fee from user-provided Tasmania Government schools screenshot.", 101),
            ("tuition", "Junior Secondary Tuition", "Junior Secondary", "7", "10", 14300, None, None, "AUD", "per annum", 1, "Junior Secondary Year 7-10. 2026 annual fee from user-provided Tasmania Government schools screenshot.", 110),
            ("tuition", "Junior Secondary Tuition Weekly", "Junior Secondary", "7", "10", 380, None, None, "AUD", "per week", 0, "Junior Secondary Year 7-10. 2026 weekly fee from user-provided Tasmania Government schools screenshot.", 111),
            ("tuition", "Senior Secondary Tuition", "Senior Secondary", "11", "12", 15400, None, None, "AUD", "per annum", 1, "Senior Secondary Year 11-12. 2026 annual fee from user-provided Tasmania Government schools screenshot.", 120),
            ("tuition", "Senior Secondary Tuition Weekly", "Senior Secondary", "11", "12", 440, None, None, "AUD", "per week", 0, "Senior Secondary Year 11-12. 2026 weekly fee from user-provided Tasmania Government schools screenshot.", 121),
            ("admin", "Administration Fee", "All", "", "", 300, None, None, "AUD", "maximum per year", 1, "Service fee. AUD 75 per term; maximum AUD 300 per year.", 200),
            ("admin", "Arrival Support Fee", "All", "", "", 150, None, None, "AUD", "one-off", 0, "Once-off arrival support fee.", 210),
            ("accommodation", "Accommodation Visit Fee", "All", "", "", 200, None, None, "AUD", "one-off", 0, "", 300),
            ("accommodation", "Homestay Management Fee", "All", "", "", 200, None, None, "AUD", "per annum", 0, "", 310),
            ("accommodation", "Homestay Fee", "Homestay", "9", "12", 360, None, None, "AUD", "per week", 0, "Homestay fee for Year 9-12.", 320),
        ]
        seed_schedule(
            "TAS",
            "TAS Government School Fees 2026",
            "Tasmanian Government Schools International",
            "Seeded MVP state fee schedule from user-provided screenshots. Schools with State=TAS and sector=Government/Public reference this active template instead of copying fees per school. 2026 values were entered; visible 2025/2027 values can be added as separate historical/future schedules later if needed.",
            tas_items,
        )
        wa_items = [
            ("tuition", "Pre-primary Tuition", "Pre-primary", "Pre-primary", "Pre-primary", 14575, None, None, "AUD", "per annum", 1, "Annual tuition fee. Intensive English Centre Program and EALD not applicable. 2026 fee from user-provided WA Government schools screenshot.", 100),
            ("tuition", "Primary Tuition", "Primary", "1", "6", 14575, None, None, "AUD", "per annum", 1, "Primary Years 1 to 6 annual tuition fee from user-provided WA Government schools screenshot.", 110),
            ("tuition", "Primary with Intensive English Centre Program", "Primary + IEC", "1", "6", 17575, None, None, "AUD", "per annum", 0, "Primary Years 1 to 6 with Intensive English Centre Program (IEC).", 111),
            ("tuition", "Primary with EALD", "Primary + EALD", "1", "6", 16075, None, None, "AUD", "per annum", 0, "Primary Years 1 to 6 with English as an additional language or dialect (EALD).", 112),
            ("tuition", "Lower Secondary Tuition", "Lower Secondary", "7", "10", 17286, None, None, "AUD", "per annum", 1, "Lower secondary Years 7 to 10 annual tuition fee from user-provided WA Government schools screenshot.", 120),
            ("tuition", "Lower Secondary with Intensive English Centre Program", "Lower Secondary + IEC", "7", "10", 20286, None, None, "AUD", "per annum", 0, "Lower secondary Years 7 to 10 with Intensive English Centre Program (IEC).", 121),
            ("tuition", "Lower Secondary with EALD", "Lower Secondary + EALD", "7", "10", 18786, None, None, "AUD", "per annum", 0, "Lower secondary Years 7 to 10 with English as an additional language or dialect (EALD).", 122),
            ("tuition", "Upper Secondary Tuition", "Upper Secondary", "11", "12", 18980, None, None, "AUD", "per annum", 1, "Upper secondary Years 11 and 12 annual tuition fee from user-provided WA Government schools screenshot.", 130),
            ("tuition", "Upper Secondary with Intensive English Centre Program", "Upper Secondary + IEC", "11", "12", 21980, None, None, "AUD", "per annum", 0, "Upper secondary Years 11 and 12 with Intensive English Centre Program (IEC).", 131),
            ("tuition", "Upper Secondary with EALD", "Upper Secondary + EALD", "11", "12", 20480, None, None, "AUD", "per annum", 0, "Upper secondary Years 11 and 12 with English as an additional language or dialect (EALD).", 132),
        ]
        seed_schedule(
            "WA",
            "WA Government School Fees 2026",
            "Western Australian Government Schools International",
            "Seeded MVP state fee schedule from user-provided screenshot. Schools with State=WA and sector=Government/Public reference this active template instead of copying fees per school. Only visible tuition fees were entered; other WA fees can be added later in School Information Collection Center.",
            wa_items,
        )

    def ensure_school_library_seed(self, conn):
        stamp = now_iso()
        cities = [
            ("Melbourne", "VIC", 10),
            ("Sydney", "NSW", 20),
            ("Brisbane", "QLD", 30),
            ("Adelaide", "SA", 40),
            ("Perth", "WA", 50),
        ]
        for city_name, state, order in cities:
            conn.execute(
                """INSERT INTO school_cities(city_name,state,country,sort_order,is_active)
                   VALUES(?,?,?,?,1)
                   ON CONFLICT(city_name,state) DO NOTHING""",
                (city_name, state, "Australia", order),
            )
        sectors = [
            ("public", "公立", "Public", 10),
            ("private", "私立", "Private", 20),
        ]
        for key, zh, en, order in sectors:
            conn.execute(
                """INSERT INTO school_sectors(sector_key,sector_name_zh,sector_name_en,sort_order,is_active)
                   VALUES(?,?,?,?,1)
                   ON CONFLICT(sector_key) DO NOTHING""",
                (key, zh, en, order),
            )
        tags = [
            ("boarding", "Boarding / 寄宿", "住宿", "#0f766e"),
            ("coed", "男女混校", "学生组成", "#2563eb"),
            ("boys", "男校", "学生组成", "#475569"),
            ("girls", "女校", "学生组成", "#db2777"),
            ("ib", "IB 课程", "课程", "#7c3aed"),
            ("vce", "VCE", "课程", "#0891b2"),
            ("eal", "EAL 支持", "支持", "#16a34a"),
            ("selective", "精英/选拔", "录取", "#d97706"),
            ("music", "音乐特色", "特色", "#9333ea"),
            ("sport", "体育特色", "特色", "#ea580c"),
            ("stem", "STEM", "特色", "#0284c7"),
            ("international", "国际学生", "支持", "#be123c"),
        ]
        for key, name, group, color in tags:
            conn.execute(
                """INSERT INTO school_feature_tags(tag_key,tag_name_zh,tag_group,color,is_dynamic,is_active)
                   VALUES(?,?,?,?,1,1)
                   ON CONFLICT(tag_key) DO NOTHING""",
                (key, name, group, color),
            )
        city_ids = {
            (row["city_name"], row["state"]): row["id"]
            for row in conn.execute("SELECT id,city_name,state FROM school_cities")
        }
        library_structure = {
            "sections": ["Overview", "Admissions", "Fees", "Zone", "Academic", "International Students", "Files"],
            "folders": ["Brochure", "Application", "Offer", "Fees", "Zone Map", "Logo", "Notes"],
        }
        sample_schools = [
            {
                "school_name": "Melbourne High School",
                "city": ("Melbourne", "VIC"), "sector_key": "public", "school_type": "公立",
                "suburb": "South Yarra", "postcode": "3141", "year_levels": "Year 9 - Year 12",
                "gender": "Boys", "boarding": 0, "international": 1, "curriculum": "VCE",
                "tags": ["public", "boys", "selective", "vce", "stem"],
                "zone": {"zone_required": "No standard residential zone in sample", "zone_notes": "Selective entry sample; verify before use."},
                "academic": {"curriculum": "VCE", "academic_notes": "Selective entry public school sample data."},
            },
            {
                "school_name": "Balwyn High School",
                "city": ("Melbourne", "VIC"), "sector_key": "public", "school_type": "公立",
                "suburb": "Balwyn North", "postcode": "3104", "year_levels": "Year 7 - Year 12",
                "gender": "Co-ed", "boarding": 0, "international": 1, "curriculum": "VCE",
                "tags": ["public", "coed", "vce", "eal", "international"],
                "zone": {"zone_required": "Yes", "zone_notes": "School zone sample field; verify official zone map before advising."},
                "academic": {"curriculum": "VCE", "academic_notes": "Sample academic profile only."},
            },
            {
                "school_name": "Caulfield Grammar School",
                "city": ("Melbourne", "VIC"), "sector_key": "private", "school_type": "私立",
                "suburb": "Caulfield", "postcode": "3162", "year_levels": "ELC - Year 12",
                "gender": "Co-ed", "boarding": 1, "international": 1, "curriculum": "VCE",
                "tags": ["private", "coed", "boarding", "vce", "international"],
                "zone": {"zone_required": "No", "zone_notes": "Private school; residential zone generally not the main criterion."},
                "academic": {"curriculum": "VCE", "academic_notes": "Sample private school academic notes."},
            },
            {
                "school_name": "Presbyterian Ladies' College",
                "city": ("Melbourne", "VIC"), "sector_key": "private", "school_type": "私立",
                "suburb": "Burwood", "postcode": "3125", "year_levels": "ELC - Year 12",
                "gender": "Girls", "boarding": 1, "international": 1, "curriculum": "VCE / IB",
                "tags": ["private", "girls", "boarding", "ib", "vce", "international"],
                "zone": {"zone_required": "No", "zone_notes": "Private school sample."},
                "academic": {"curriculum": "VCE / IB", "academic_notes": "Sample only; verify latest program details."},
            },
            {
                "school_name": "Melbourne Grammar School",
                "city": ("Melbourne", "VIC"), "sector_key": "private", "school_type": "私立",
                "suburb": "South Yarra", "postcode": "3141", "year_levels": "Prep - Year 12",
                "gender": "Boys", "boarding": 1, "international": 1, "curriculum": "VCE",
                "tags": ["private", "boys", "boarding", "vce", "sport", "music"],
                "zone": {"zone_required": "No", "zone_notes": "Private school sample."},
                "academic": {"curriculum": "VCE", "academic_notes": "Sample only; not a ranking claim."},
            },
        ]
        for school in sample_schools:
            city_id = city_ids.get(school["city"])
            school_uid = "SCH-" + "".join(ch for ch in school["school_name"].upper() if ch.isalnum())[:18]
            basic_info = {
                "city": school["city"][0],
                "state": school["city"][1],
                "sector": school["sector_key"],
                "school_type": school["school_type"],
                "data_note": "Phase 1 sample record only; verify before advising parents.",
            }
            conn.execute(
                """INSERT INTO schools(
                   school_id,school_name,school_name_english,school_name_display,city_id,sector_key,school_type,suburb,state,postcode,year_levels,gender,
                   boarding_available,international_student_available,curriculum,basic_info_json,
                   library_structure_json,dynamic_tags_json,zone_data_json,academic_info_json,
                   data_quality_status,notes,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(state, school_name) DO NOTHING""",
                (
                    school_uid, school["school_name"], school["school_name"], school["school_name"], city_id, school["sector_key"], school["school_type"],
                    school["suburb"], school["city"][1], school["postcode"], school["year_levels"],
                    school["gender"], school["boarding"], school["international"], school["curriculum"],
                    json.dumps(basic_info, ensure_ascii=False),
                    json.dumps(library_structure, ensure_ascii=False),
                    json.dumps(school["tags"], ensure_ascii=False),
                    json.dumps(school["zone"], ensure_ascii=False),
                    json.dumps(school["academic"], ensure_ascii=False),
                    "Sample", "Phase 1 testing data only.", stamp, stamp,
                ),
            )
        def geocode_key(value):
            return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())

        geocode_rows = [
            ("Melbourne CBD", "melbournecbd", "cbd", "VIC", "Melbourne CBD VIC", "Melbourne", "3000", -37.8136, 144.9631, "seed"),
            ("Sydney CBD", "sydneycbd", "cbd", "NSW", "Sydney CBD NSW", "Sydney", "2000", -33.8688, 151.2093, "seed"),
            ("Brisbane CBD", "brisbanecbd", "cbd", "QLD", "Brisbane CBD QLD", "Brisbane", "4000", -27.4698, 153.0251, "seed"),
            ("Adelaide CBD", "adelaidecbd", "cbd", "SA", "Adelaide CBD SA", "Adelaide", "5000", -34.9285, 138.6007, "seed"),
            ("Perth CBD", "perthcbd", "cbd", "WA", "Perth CBD WA", "Perth", "6000", -31.9523, 115.8613, "seed"),
            ("Canberra CBD", "canberracbd", "cbd", "ACT", "Canberra CBD ACT", "Canberra", "2601", -35.2809, 149.1300, "seed"),
            ("Hobart CBD", "hobartcbd", "cbd", "TAS", "Hobart CBD TAS", "Hobart", "7000", -42.8821, 147.3272, "seed"),
            ("Darwin CBD", "darwincbd", "cbd", "NT", "Darwin CBD NT", "Darwin", "0800", -12.4634, 130.8456, "seed"),
            ("Doncaster", geocode_key("Doncaster"), "suburb", "VIC", "Doncaster suburb centre VIC", "Doncaster", "3108", -37.7870, 145.1230, "seed"),
            ("Box Hill", geocode_key("Box Hill"), "suburb", "VIC", "Box Hill suburb centre VIC", "Box Hill", "3128", -37.8190, 145.1250, "seed"),
            ("Glen Waverley", geocode_key("Glen Waverley"), "suburb", "VIC", "Glen Waverley suburb centre VIC", "Glen Waverley", "3150", -37.8786, 145.1648, "seed"),
            ("Burwood", geocode_key("Burwood"), "suburb", "VIC", "Burwood suburb centre VIC", "Burwood", "3125", -37.8490, 145.1110, "seed"),
            ("Epping", geocode_key("Epping"), "suburb", "VIC", "Epping suburb centre VIC", "Epping", "3076", -37.6530, 145.0210, "seed"),
            ("Balwyn North", geocode_key("Balwyn North"), "suburb", "VIC", "Balwyn North suburb centre VIC", "Balwyn North", "3104", -37.7920, 145.0860, "seed"),
            ("South Yarra", geocode_key("South Yarra"), "suburb", "VIC", "South Yarra suburb centre VIC", "South Yarra", "3141", -37.8390, 144.9910, "seed"),
            ("Caulfield", geocode_key("Caulfield"), "suburb", "VIC", "Caulfield suburb centre VIC", "Caulfield", "3162", -37.8820, 145.0220, "seed"),
            ("Mosman", geocode_key("Mosman"), "suburb", "NSW", "Mosman suburb centre NSW", "Mosman", "2088", -33.8280, 151.2440, "seed"),
            ("Burwood", geocode_key("Burwood"), "suburb", "NSW", "Burwood suburb centre NSW", "Burwood", "2134", -33.8772, 151.1049, "seed"),
            ("36 Somerville Street, Doncaster VIC", geocode_key("36 Somerville Street, Doncaster VIC"), "address", "VIC", "36 Somerville Street, Doncaster VIC", "Doncaster", "3108", -37.7829, 145.1255, "seed"),
            ("32 Hanover Road, Vermont South VIC", geocode_key("32 Hanover Road, Vermont South VIC"), "address", "VIC", "32 Hanover Road, Vermont South VIC", "Vermont South", "3133", -37.8575, 145.1834, "seed"),
            ("100 Queen Street, Melbourne VIC", geocode_key("100 Queen Street, Melbourne VIC"), "address", "VIC", "100 Queen Street, Melbourne VIC", "Melbourne", "3000", -37.8152, 144.9615, "seed"),
            ("1 Main Road, Box Hill VIC", geocode_key("1 Main Road, Box Hill VIC"), "address", "VIC", "1 Main Road, Box Hill VIC", "Box Hill", "3128", -37.8198, 145.1248, "seed"),
            ("100 Queen Street, Sydney NSW", geocode_key("100 Queen Street, Sydney NSW"), "address", "NSW", "100 Queen Street, Sydney NSW", "Sydney", "2000", -33.8720, 151.2110, "seed"),
            ("Main Road Box Hill VIC", geocode_key("Main Road Box Hill VIC"), "address", "VIC", "Main Road, Box Hill VIC", "Box Hill", "3128", -37.8198, 145.1248, "seed"),
            ("Main Road Epping VIC", geocode_key("Main Road Epping VIC"), "address", "VIC", "Main Road, Epping VIC", "Epping", "3076", -37.6535, 145.0200, "seed"),
        ]
        for raw, normalized, input_type, state, formatted, suburb, postcode, lat, lng, source in geocode_rows:
            conn.execute(
                """INSERT INTO school_geocode_cache(
                   raw_input_text,normalized_query,input_type,state,formatted_address,suburb,postcode,
                   latitude,longitude,geocode_source,geocoded_at,geocode_status,candidates_json,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(normalized_query,state,input_type) DO NOTHING""",
                (raw, normalized, input_type, state, formatted, suburb, postcode, lat, lng, source, stamp, "Resolved", "[]", stamp, stamp),
            )
        school_coordinates = {
            "Melbourne High School": (-37.8355, 144.9825),
            "Balwyn High School": (-37.7909, 145.0827),
            "Caulfield Grammar School": (-37.8824, 145.0245),
            "Presbyterian Ladies' College": (-37.8478, 145.1150),
            "Melbourne Grammar School": (-37.8344, 144.9820),
        }
        for school_name, (lat, lng) in school_coordinates.items():
            conn.execute(
                """UPDATE schools
                   SET latitude=COALESCE(latitude,?),longitude=COALESCE(longitude,?),
                       geocode_status=COALESCE(NULLIF(geocode_status,''),'Seeded'),geocoded_at=COALESCE(geocoded_at,?)
                   WHERE school_name=?""",
                (lat, lng, stamp, school_name),
            )

    def ensure_default_module_permissions(self, conn):
        new_permission_ids = set()
        roles = {row["id"]: row["name"] for row in conn.execute("SELECT id,name FROM roles")}
        all_keys = [key for key, *_ in MODULE_DEFINITIONS]
        for user in conn.execute("SELECT id,role_id FROM users"):
            existing_keys = {
                row["module_key"]
                for row in conn.execute(
                    "SELECT module_key FROM user_module_permissions WHERE user_id=?",
                    (user["id"],),
                )
            }
            legacy_empty_user = not existing_keys
            if not legacy_empty_user:
                missing_keys = [key for key in all_keys if key not in existing_keys]
            else:
                missing_keys = all_keys
            if not missing_keys:
                continue
            role_name = roles.get(user["role_id"], "View Only")
            allowed_keys = set(ROLE_MODULE_PRESETS.get(role_name, ROLE_MODULE_PRESETS["View Only"]))
            role_permissions = set(ROLE_PERMISSIONS.get(role_name, []))
            stamp = now_iso()
            for key in missing_keys:
                can_access = 1 if key in allowed_keys else 0
                can_edit = 1 if can_access and (role_name == "Admin" or (key == "students" and "students.edit" in role_permissions)) else 0
                cursor = conn.execute(
                    """INSERT INTO user_module_permissions(
                       user_id,module_key,can_access,can_view,can_edit,can_open,can_download,can_upload,can_mark_delete,
                       can_mark_move,can_mark_rename,can_cancel_file_marker,can_mark_file_marker_processed,
                       can_restore,created_at,updated_at,updated_by
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        user["id"], key, can_access, can_access, can_edit,
                        0,
                        1 if can_access and key == "file_manager" and role_name in {"Admin", "Manager"} else 0,
                        1 if can_access and key == "file_manager" and role_name in {"Admin", "Manager"} else 0,
                        1 if can_access and key == "file_manager" and role_name in {"Admin", "Manager"} else 0,
                        1 if can_access and key == "file_manager" and role_name in {"Admin", "Manager"} else 0,
                        1 if can_access and key == "file_manager" and role_name in {"Admin", "Manager"} else 0,
                        1 if can_access and key == "file_manager" and role_name in {"Admin", "Manager"} else 0,
                        1 if can_access and key == "file_manager" and role_name in {"Admin", "Manager"} else 0,
                        1 if can_access and key == "file_manager" and role_name == "Admin" else 0,
                        stamp, stamp, None,
                    ),
                )
                new_permission_ids.add(cursor.lastrowid)
        return new_permission_ids

    def disable_removed_xiaohongshu_integration(self, conn):
        conn.execute("UPDATE modules SET is_active=0 WHERE module_key='xiaohongshu_system'")
        conn.execute("DELETE FROM system_settings WHERE key='XIAOHONGSHU_SYSTEM_URL'")
        conn.execute(
            """UPDATE user_module_permissions
               SET can_access=0,can_view=0,can_open=0,updated_at=?
               WHERE module_key='xiaohongshu_system'
                 AND (can_access<>0 OR can_view<>0 OR can_open<>0)""",
            (now_iso(),),
        )

    def ensure_permission_consistency(self, conn, *, permission_ids=None):
        stamp = now_iso()
        permission_filter = ""
        permission_params = ()
        if permission_ids is not None:
            permission_params = tuple(sorted(permission_ids))
            permission_filter = (
                " AND id IN (" + ",".join("?" for _ in permission_params) + ")"
                if permission_params else " AND 0"
            )
        action_columns = (
            "can_create", "can_edit", "can_delete", "can_upload", "can_download",
            "can_restore", "can_apply_signature_stamp", "can_mark_delete",
            "can_mark_move", "can_mark_rename", "can_cancel_file_marker",
            "can_mark_file_marker_processed", "can_complete", "can_dismiss",
            "can_reassign", "can_manage_rules", "can_confirm_upload_review",
            "can_open",
        )
        action_predicate = " OR ".join(f"COALESCE({column},0)=1" for column in action_columns)
        conn.execute(
            f"""UPDATE user_module_permissions
                SET can_access=1,can_view=1,updated_at=?
                WHERE module_key NOT IN ('content_center','xiaohongshu_system')
                  AND ({action_predicate})
                  AND (can_access<>1 OR can_view<>1){permission_filter}""",
            (stamp, *permission_params),
        )
        conn.execute(
            """UPDATE user_module_permissions
               SET can_access=0,can_view=0,can_open=0,
                   can_view_dashboard=0,can_view_topics=0,can_create_topics=0,
                   can_edit_topics=0,can_generate_scripts=0,can_edit_scripts=0,
                   can_review_scripts=0,can_approve_scripts=0,can_schedule_posts=0,
                   can_mark_published=0,can_enter_performance_data=0,
                   can_view_performance=0,can_manage_material_library=0,
                   can_import_old_data=0,updated_at=?
               WHERE module_key='content_center'
                 AND user_id NOT IN (
                   SELECT id FROM users WHERE lower(username) IN ('admin','sonnyz')
                 )
                 AND (
                   can_access<>0 OR can_view<>0 OR can_open<>0 OR
                   can_view_dashboard<>0 OR can_view_topics<>0 OR can_create_topics<>0 OR
                   can_edit_topics<>0 OR can_generate_scripts<>0 OR can_edit_scripts<>0 OR
                   can_review_scripts<>0 OR can_approve_scripts<>0 OR can_schedule_posts<>0 OR
                   can_mark_published<>0 OR can_enter_performance_data<>0 OR
                   can_view_performance<>0 OR can_manage_material_library<>0 OR
                   can_import_old_data<>0
                 )""",
            (stamp,),
        )

    def ensure_workflow_action_permissions(self, conn, *, permission_ids=None):
        editable_roles = {"Admin", "Manager", "Consultant", "Visa Officer", "Finance"}
        upload_modules = {"applications", "offer", "coe_oshc", "visa", "payments"}
        review_modules = {"applications", "offer", "coe_oshc", "payments"}
        for row in conn.execute(
            """SELECT ump.id,r.name role_name,ump.module_key,ump.can_access
               FROM user_module_permissions ump
               JOIN users u ON u.id=ump.user_id
               JOIN roles r ON r.id=u.role_id
               WHERE ump.module_key IN ('applications','offer','coe_oshc','visa','payments')"""
        ):
            if permission_ids is not None and row["id"] not in permission_ids:
                continue
            if not row["can_access"] or row["role_name"] not in editable_roles:
                continue
            conn.execute(
                """UPDATE user_module_permissions
                   SET can_view=1,can_create=?,can_upload=?,can_confirm_upload_review=?
                   WHERE id=?""",
                (
                    1 if row["module_key"] in upload_modules else 0,
                    1 if row["module_key"] in upload_modules else 0,
                    1 if row["module_key"] in review_modules else 0,
                    row["id"],
                ),
            )

    def ensure_file_manager_action_permissions(self, conn, *, permission_ids=None):
        for row in conn.execute(
            "SELECT ump.id,u.id user_id,r.name role_name,ump.module_key,ump.can_access FROM user_module_permissions ump JOIN users u ON u.id=ump.user_id JOIN roles r ON r.id=u.role_id WHERE ump.module_key='file_manager'"
        ):
            if permission_ids is not None and row["id"] not in permission_ids:
                continue
            if not row["can_access"] or row["role_name"] not in {"Admin", "Manager"}:
                continue
            conn.execute(
                """UPDATE user_module_permissions SET can_download=1,can_upload=1,can_mark_delete=1,
                   can_mark_move=1,can_mark_rename=1,can_cancel_file_marker=1,
                   can_mark_file_marker_processed=1,
                   can_restore=CASE WHEN ?='Admin' THEN 1 ELSE can_restore END
                   WHERE id=?""",
                (row["role_name"], row["id"]),
            )

    def ensure_reminder_action_permissions(self, conn, *, permission_ids=None):
        for row in conn.execute(
            "SELECT ump.id,u.id user_id,r.name role_name,ump.module_key,ump.can_access FROM user_module_permissions ump JOIN users u ON u.id=ump.user_id JOIN roles r ON r.id=u.role_id WHERE ump.module_key='reminders'"
        ):
            if permission_ids is not None and row["id"] not in permission_ids:
                continue
            if not row["can_access"]:
                continue
            if row["role_name"] == "Admin":
                conn.execute(
                    """UPDATE user_module_permissions SET can_view=1,can_create=1,can_edit=1,
                       can_complete=1,can_dismiss=1,can_reassign=1,can_manage_rules=1 WHERE id=?""",
                    (row["id"],),
                )
            elif row["role_name"] == "Manager":
                conn.execute(
                    """UPDATE user_module_permissions SET can_view=1,can_create=1,can_edit=1,
                       can_complete=1,can_dismiss=1,can_reassign=1 WHERE id=?""",
                    (row["id"],),
                )
            elif row["role_name"] in {"Consultant", "Visa Officer", "Finance"}:
                conn.execute(
                    "UPDATE user_module_permissions SET can_view=1,can_create=1,can_complete=1,can_dismiss=1 WHERE id=?",
                    (row["id"],),
                )

    def ensure_school_feature_tag_seed(self, conn):
        stamp = now_iso()
        category_data = [
            ("sports", "体育", "Sports", "体育项目、校队、运动设施与奖学金", 10, ["体育", "综合体育", "精英运动员项目", "体育奖学金", "体育学院 / Sports Academy", "校队体系成熟", "校际比赛", "体育设施", "游泳", "网球", "篮球", "足球", "澳式足球", "橄榄球", "板球", "曲棍球", "排球", "羽毛球", "高尔夫", "赛艇", "帆船", "马术", "田径", "越野跑", "体操", "户外运动", "水上运动"]),
            ("music", "音乐", "Music", "音乐课程、乐团、演出和奖学金", 20, ["音乐", "古典音乐", "现代音乐", "管弦乐团", "交响乐团", "弦乐团", "管乐团", "爵士乐团", "合唱团", "声乐", "钢琴", "小提琴", "大提琴", "打击乐", "吉他 / 现代乐器", "作曲课程", "音乐制作", "录音棚", "音乐奖学金", "AMEB支持", "校内音乐会多"]),
            ("visual_arts", "艺术", "Visual Arts", "视觉艺术、设计、作品集和展览机会", 30, ["艺术", "视觉艺术", "绘画", "素描", "油画", "水彩", "雕塑", "陶艺", "摄影", "数字艺术", "平面设计", "产品设计", "视觉传达设计", "建筑设计基础", "时尚设计", "纺织设计", "动漫 / 插画", "媒体艺术", "艺术作品集支持", "艺术展览机会多", "校内艺术工作室", "艺术奖学金"]),
            ("performing_arts", "表演", "Performing Arts", "戏剧、舞蹈、音乐剧和舞台机会", 40, ["表演艺术", "Drama", "Theatre", "舞台剧", "音乐剧", "舞蹈", "芭蕾", "现代舞", "街舞", "戏剧表演", "公众演讲", "辩论", "舞台管理", "灯光音响", "影视表演", "导演基础", "编剧", "校内剧院", "年度大型演出", "表演艺术奖学金"]),
            ("academic", "学术", "Academic", "学术表现、竞赛、升学和学习支持", 50, ["学术", "高ATAR表现", "大学升学", "精英学术项目", "高潜力学生项目", "Gifted Program", "Extension Program", "加速课程", "学术竞赛", "数学", "英语", "科学", "人文", "商科", "经济", "法律预备", "医学方向支持", "工程方向支持", "小班教学", "学习支持", "学术奖学金"]),
            ("stem", "STEM / 科技", "STEM / Technology", "STEM、机器人、编程、工程和科技设施", 60, ["STEM", "科学", "数学", "工程", "机器人", "编程", "AI / 人工智能", "数据科学", "信息技术", "网络安全", "电子工程基础", "3D打印", "设计与技术", "创客空间 / Makerspace", "科学实验室", "STEM竞赛", "机器人竞赛", "数学竞赛", "科学奥赛", "工程项目制学习", "数字科技"]),
            ("language_international_support", "语言与国际学生支持", "Language & International Support", "EAL、国际学生办公室、低龄适应和家长沟通", 70, ["国际学生支持", "EAL支持", "ESL支持", "校内语言班", "强化英语课程", "英语过渡课程", "国际学生适应课程", "新生入学支持", "学习支持老师", "中文支持", "国际学生办公室", "国际学生导师", "Buddy Program", "Homestay支持", "CAAW支持", "低龄学生照顾", "家长沟通频繁"]),
            ("curriculum", "课程体系", "Curriculum", "州证书、IB、Cambridge 和多路径课程", 80, ["VCE", "VCE VM", "HSC", "QCE", "WACE", "SACE", "TCE", "ACT Senior Secondary Certificate", "NTCET", "IB PYP", "IB MYP", "IB DP", "IB CP", "Cambridge Curriculum", "Montessori", "Reggio Emilia", "Vocational Pathway", "TAFE Pathway", "University Pathway", "Foundation Pathway", "多路径升学"]),
            ("school_type_philosophy", "学校类型与教育理念", "School Type & Philosophy", "学校类型、管理风格、教育理念和文化", 90, ["私校传统", "公校资源", "精英公校", "教会学校", "天主教学校", "基督教学校", "非宗教学校", "男校", "女校", "混校", "小班制", "全人教育", "精英教育", "个性化教育", "严格管理", "自由开放型", "注重纪律", "注重领导力", "注重品格教育", "注重社区服务", "注重心理健康", "注重户外教育", "注重实践学习", "项目制学习", "创新教育", "国际化", "多元文化"]),
            ("accommodation_welfare", "住宿与福利", "Accommodation & Welfare", "住校、寄宿家庭、CAAW 和低龄福利", 100, ["住校", "寄宿家庭", "住校和寄宿家庭均可", "只接受父母陪读", "低龄福利支持", "Boarding", "Boarding设施好", "Boarding名额紧张", "Homestay由学校安排", "Homestay由第三方安排", "CAAW支持", "监护安排清晰", "宿舍管理严格", "周末活动丰富", "住宿安全性高", "适合低龄学生", "适合独立性强学生"]),
            ("location_lifestyle", "地理与生活环境", "Location & Lifestyle", "城市区域、交通、环境和生活便利度", 110, ["墨尔本东区", "墨尔本西区", "墨尔本北区", "墨尔本南区", "悉尼北区", "悉尼东区", "布里斯班", "阿德莱德", "珀斯", "霍巴特", "堪培拉", "区域城市", "大城市学校", "小城市学校", "交通便利", "靠近火车站", "靠近大学", "靠近华人区", "生活便利", "环境安静", "安全性高", "校园面积大", "校园设施新", "自然环境好", "海边学校", "山区学校", "市中心学校", "郊区学校"]),
            ("application_admission", "申请与录取特点", "Application & Admission", "申请难度、入学要求、年级和录取特点", 120, ["申请竞争激烈", "名额紧张", "常年满位", "提前一年申请", "提前两年申请", "接受滚动申请", "接受插班", "接受低龄国际生", "接受小学国际生", "接受Year7以上", "接受Year9以上", "接受Year10以上", "接受Year11", "不建议Year11插班", "需要AEAS", "需要IELTS", "需要IDAT", "需要面试", "需要笔试", "需要成绩优秀", "看重英文能力", "看重行为记录", "看重推荐信", "Offer速度快", "Offer审核严格", "适合英文较弱学生", "适合英文较强学生", "适合成绩一般学生", "适合学术强学生"]),
            ("university_pathways", "大学与升学方向", "University Pathways", "大学方向、职业方向和升学辅导", 130, ["八大升学", "墨大方向", "莫纳什方向", "悉尼大学方向", "UNSW方向", "昆士兰大学方向", "阿德莱德大学方向", "西澳大学方向", "塔大方向", "医学方向", "法律方向", "商科方向", "工程方向", "IT方向", "教育方向", "艺术设计方向", "音乐方向", "体育方向", "护理方向", "心理学方向", "建筑方向", "传媒方向", "升学辅导", "职业规划支持", "大学申请支持", "校友资源"]),
            ("leadership_activities", "领导力与课外活动", "Leadership & Activities", "领导力、社团、公益和竞赛活动", 140, ["领导力项目", "学生会", "House System", "社团丰富", "社区服务", "公益活动多", "国际交流项目", "海外游学", "户外教育营", "Duke of Edinburgh", "Cadets", "Public Speaking", "Debating", "Model United Nations", "创业项目", "商业竞赛", "金融社团", "科学社团", "环保社团", "学生导师制度"]),
            ("wellbeing_student_support", "心理与学生支持", "Wellbeing & Student Support", "心理支持、Pastoral Care、反霸凌和家校沟通", 150, ["心理支持", "Wellbeing体系成熟", "Pastoral Care", "导师制度", "班主任跟进", "低龄适应支持", "新生融入支持", "反霸凌机制", "学生行为管理", "家校沟通", "学习困难支持", "特殊学习需求支持", "Counselling服务", "国际学生心理支持", "家长反馈机制"]),
        ]
        final_tag_occurrence = {}
        for category_key, _name_cn, _name_en, _description, _sort_order, tags in category_data:
            for index, tag_name in enumerate(tags, start=1):
                tag_key = f"{category_key}_{re.sub(r'[^0-9a-zA-Z]+', '_', tag_name).strip('_').lower() or index}"
                final_tag_occurrence[tag_key] = (category_key, index)
        for category_key, name_cn, name_en, description, sort_order, tags in category_data:
            conn.execute(
                """INSERT INTO school_feature_categories(
                   category_key,category_name_cn,category_name_en,description,sort_order,is_active,created_at,updated_at
                ) VALUES(?,?,?,?,?,1,?,?)
                ON CONFLICT(category_key) DO NOTHING""",
                (category_key, name_cn, name_en, description, sort_order, stamp, stamp),
            )
            category_id = conn.execute("SELECT id FROM school_feature_categories WHERE category_key=?", (category_key,)).fetchone()["id"]
            for index, tag_name in enumerate(tags, start=1):
                tag_key = f"{category_key}_{re.sub(r'[^0-9a-zA-Z]+', '_', tag_name).strip('_').lower() or index}"
                if final_tag_occurrence[tag_key] != (category_key, index):
                    continue
                keywords = json.dumps([tag_name, tag_name.replace("", ""), name_cn, name_en], ensure_ascii=False)
                conn.execute(
                    """INSERT INTO school_feature_tags(
                       tag_key,tag_name_zh,tag_group,color,is_dynamic,is_active,
                       category_id,tag_name_cn,tag_name_en,description,keywords,sort_order,created_at,updated_at
                    ) VALUES(?,?,?,?,1,1,?,?,?,?,?,?,?,?)
                    ON CONFLICT(tag_key) DO NOTHING""",
                    (tag_key, tag_name, name_cn, "#2563eb", category_id, tag_name, tag_name, description, keywords, index, stamp, stamp),
                )
        legacy_group_map = {
            "住宿": "accommodation_welfare",
            "学生组成": "school_type_philosophy",
            "课程": "curriculum",
            "支持": "language_international_support",
            "录取": "application_admission",
            "特色": "leadership_activities",
        }
        for legacy_group, category_key in legacy_group_map.items():
            category = conn.execute(
                "SELECT id,category_name_cn,category_name_en FROM school_feature_categories WHERE category_key=?",
                (category_key,),
            ).fetchone()
            if not category:
                continue
            conn.execute(
                """UPDATE school_feature_tags
                   SET category_id=?,
                       tag_name_cn=COALESCE(NULLIF(tag_name_cn,''), tag_name_zh, tag_name_en, tag_key),
                       tag_name_en=COALESCE(NULLIF(tag_name_en,''), tag_name_zh, tag_name_cn, tag_key),
                       tag_group=COALESCE(NULLIF(tag_group,''),?),
                       updated_at=?
                   WHERE (category_id IS NULL OR category_id='')
                     AND tag_group=?""",
                (category["id"], category["category_name_cn"], stamp, legacy_group),
            )
        fallback_category = conn.execute(
            "SELECT id,category_name_cn FROM school_feature_categories WHERE category_key='school_type_philosophy'"
        ).fetchone()
        if fallback_category:
            conn.execute(
                """UPDATE school_feature_tags
                   SET category_id=?,
                       tag_name_cn=COALESCE(NULLIF(tag_name_cn,''), tag_name_zh, tag_name_en, tag_key),
                       tag_name_en=COALESCE(NULLIF(tag_name_en,''), tag_name_zh, tag_name_cn, tag_key),
                       tag_group=COALESCE(NULLIF(tag_group,''),?),
                       updated_at=?
                   WHERE category_id IS NULL OR category_id=''""",
                (fallback_category["id"], fallback_category["category_name_cn"], stamp),
            )

    def ensure_data_update_centre_seed(self, conn):
        stamp = now_iso()
        sources = [
            ("School basic data", "school_base", "VIC", "School Locations data", "annual manual check", 0, 1, "Manual Only"),
            ("School radius area links", "school_radius_area_links", "VIC", "ABS ASGS / SA2 / LGA / suburb boundary", "manual trigger after geography or school coordinate changes", 0, 1, "Manual Only"),
            ("VIC Rental Report quarterly median rents", "rental_market", "VIC", "https://discover.data.vic.gov.au/dataset/rental-report-quarterly-data-tables", "quarterly", 1, 1, "Ready"),
            ("Victorian Property Sales Report", "property_market", "VIC", "https://www.land.vic.gov.au/valuations/resources-and-reports/property-sales-statistics", "quarterly", 1, 1, "Ready"),
            ("Victorian Government School Zones", "official_school_zones", "VIC", "https://www.findmyschool.vic.gov.au/", "annual reminder", 0, 1, "Manual Only"),
            ("Crime Statistics Agency Victoria", "crime_statistics", "VIC", "https://www.crimestatistics.vic.gov.au/crime-statistics/latest-crime-data-by-area", "quarterly or half-yearly", 1, 1, "Ready"),
            ("School surrounding living cost summary", "school_radius_market_summary", "VIC", "rental_market + property_market + geo_areas", "after source data update", 0, 1, "Ready"),
            ("School surrounding crime data summary", "school_area_crime_summary", "VIC", "crime_statistics + school coordinates + geo_areas", "after crime or geography update", 0, 1, "Ready"),
            ("ACARA School Profile Data", "school_profile_data", "ALL", "https://www.acara.edu.au/contact-us/acara-data-access", "annual automatic check", 1, 1, "Ready"),
            ("Private School International Student Fees", "private_international_fees", "ALL", "Official school websites and official linked fee documents", "annual full private school batch", 0, 1, "Manual Only"),
        ]
        for source in sources:
            conn.execute(
                """INSERT INTO data_sources(
                   source_name,source_type,state,source_url,update_frequency,
                   auto_check_enabled,requires_admin_confirmation,status,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(source_name,state) DO NOTHING""",
                (*source, stamp, stamp),
            )
        academic_sources = [
            ("VCAA SSCAI", "academic_results", "VIC", "", "Yearly", 1, 1, "Ready"),
            ("NESA HSC Merit Lists", "academic_results", "NSW", "", "Yearly", 1, 1, "Ready"),
            ("SCSA Student Achievement Data by School", "academic_results", "WA", "", "Yearly", 1, 1, "Ready"),
            ("QLD senior academic results", "academic_results", "QLD", "", "Yearly", 1, 1, "Ready"),
            ("SA senior academic results", "academic_results", "SA", "", "Yearly", 1, 1, "Ready"),
            ("TAS senior academic results", "academic_results", "TAS", "", "Yearly", 1, 1, "Ready"),
            ("ACT senior academic results", "academic_results", "ACT", "", "Yearly", 1, 1, "Ready"),
            ("NT senior academic results", "academic_results", "NT", "", "Manual only", 0, 1, "Manual Only"),
        ]
        for source in academic_sources:
            conn.execute(
                """INSERT INTO data_sources(
                   source_name,source_type,state,source_url,update_frequency,
                   auto_check_enabled,requires_admin_confirmation,status,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(source_name,state) DO NOTHING""",
                (*source, stamp, stamp),
            )
        self.ensure_academic_results_update_rule_seed(conn)

    def ensure_academic_results_update_schema(self, conn):
        conn.execute(
            """CREATE TABLE IF NOT EXISTS academic_results_update_rules (
               id INTEGER PRIMARY KEY,
               state TEXT NOT NULL UNIQUE,
               enabled INTEGER NOT NULL DEFAULT 0,
               data_source TEXT NOT NULL,
               source_url TEXT,
               update_frequency TEXT NOT NULL DEFAULT 'Manual only',
               preferred_update_month TEXT,
               notification_email TEXT,
               auto_import_after_successful_fetch INTEGER NOT NULL DEFAULT 0,
               require_manual_review_before_saving INTEGER NOT NULL DEFAULT 1,
               last_status TEXT NOT NULL DEFAULT 'Auto update disabled',
               last_checked_at TEXT,
               last_update_job_id INTEGER REFERENCES data_update_jobs(id) ON DELETE SET NULL,
               notes TEXT,
               created_at TEXT NOT NULL,
               updated_at TEXT NOT NULL
            )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS academic_results_versions (
               id INTEGER PRIMARY KEY,
               state TEXT NOT NULL,
               data_year TEXT NOT NULL,
               source_name TEXT NOT NULL,
               version_key TEXT NOT NULL UNIQUE,
               version_number INTEGER NOT NULL,
               update_job_id INTEGER REFERENCES data_update_jobs(id) ON DELETE SET NULL,
               import_id INTEGER REFERENCES school_ranking_imports(id) ON DELETE SET NULL,
               status TEXT NOT NULL DEFAULT 'Review required',
               is_verified INTEGER NOT NULL DEFAULT 0,
               verified_by INTEGER REFERENCES users(id),
               verified_at TEXT,
               created_by INTEGER REFERENCES users(id),
               created_at TEXT NOT NULL,
               updated_at TEXT NOT NULL,
               notes TEXT,
               UNIQUE(state,data_year,source_name,version_number)
            )"""
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_academic_results_versions_lookup ON academic_results_versions(state,data_year,source_name,is_verified,version_number)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_academic_results_update_rules_state ON academic_results_update_rules(state,enabled,last_status)")

    def ensure_academic_results_update_rule_seed(self, conn):
        stamp = now_iso()
        defaults = [
            ("VIC", 1, "VCAA SSCAI", "https://www.vcaa.vic.edu.au/administration/school-administration/performance-senior-secondary/senior-secondary-completion-and-achievement-information", "Yearly", "December", "", 1, 0, "Auto update enabled"),
            ("NSW", 1, "NESA HSC Merit Lists", "https://www.nsw.gov.au/education-and-training/nesa/awards-and-events/hsc-merit-lists", "Yearly", "December", "", 1, 0, "Auto update enabled"),
            ("WA", 1, "SCSA Student Achievement Data by School", "https://senior-secondary.scsa.wa.edu.au/certification/student-achievement-data-by-school", "Yearly", "December", "", 1, 0, "Auto update enabled"),
            ("QLD", 1, "QLD senior academic results", "https://www.qcaa.qld.edu.au/news-data/statistics/statistics-from-2020/year-12-certification-data", "Yearly", "December", "", 1, 0, "Auto update enabled"),
            ("SA", 1, "SACE public data + school website results", "https://www.sace.sa.edu.au/web/sace-data", "Yearly", "December", "", 1, 0, "Auto update enabled"),
            ("TAS", 1, "TAS senior academic results", "https://www.tasc.tas.gov.au/about/data/attainment-profiles-direct-continuation-data/", "Yearly", "December", "", 1, 0, "Auto update enabled"),
            ("ACT", 1, "ACT senior academic results", "https://www.bsss.act.edu.au/act_senior_secondary_system/certification-data/certification-data", "Yearly", "December", "", 1, 0, "Auto update enabled"),
            ("NT", 0, "NT senior academic results", "", "Manual only", "December", "", 0, 1, "Auto update disabled"),
        ]
        for row in defaults:
            conn.execute(
                """INSERT INTO academic_results_update_rules(
                   state,enabled,data_source,source_url,update_frequency,preferred_update_month,
                   notification_email,auto_import_after_successful_fetch,
                   require_manual_review_before_saving,last_status,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(state) DO NOTHING""",
                (*row, stamp, stamp),
            )

    def ensure_school_action_permissions(self, conn, *, permission_ids=None):
        for row in conn.execute(
            "SELECT ump.id,r.name role_name,ump.can_access FROM user_module_permissions ump JOIN users u ON u.id=ump.user_id JOIN roles r ON r.id=u.role_id WHERE ump.module_key='school_library'"
        ):
            if permission_ids is not None and row["id"] not in permission_ids:
                continue
            if not row["can_access"]:
                continue
            if row["role_name"] in {"Admin", "Manager"}:
                conn.execute(
                    """UPDATE user_module_permissions SET can_view=1,can_create=1,can_edit=1,can_upload=1,
                       can_download=1,can_upload_documents=1,can_review_features=1,can_edit_zone_data=1,
                       can_edit_academic_info=1,can_upload_logo=1,can_run_scraper=1,can_view_reports=1,can_generate_reports=1,
                       can_view_feature_tags=1,can_manage_feature_categories=1,can_manage_feature_tags=1,
                       can_collect_feature_tags=1,can_review_feature_suggestions=1,can_confirm_feature_tags=1,
                       can_reject_feature_tags=1,can_add_manual_feature_tags=1,
                       can_view_suburb_data=1,can_fetch_suburb_data=1,can_edit_suburb_data=1,
                       can_review_suburb_data=1,can_manage_suburb_sources=1,
                       can_view_myschool_data=1,can_fetch_myschool_data=1,can_edit_myschool_data=1,
                       can_review_myschool_data=1,can_resolve_school_data_conflicts=1,
                       can_upload_ranking_source=1,can_import_rankings=1,can_review_ranking_matches=1,
                       can_view_rankings=1,can_edit_ranking_data=1,
                       can_view_collection_center=1,can_fetch_school_website=1,
                       can_fetch_education_department=1,can_upload_bettereducation_file=1,
                       can_fetch_goodschools=1,can_fetch_abs_data=1,
                       can_review_collected_data=1,can_approve_collected_data=1,
                       can_manage_source_urls=1
                       WHERE id=?""",
                    (row["id"],),
                )
            elif row["role_name"] in {"Consultant", "View Only"}:
                conn.execute(
                    """UPDATE user_module_permissions SET can_view=1,
                       can_view_feature_tags=1,can_collect_feature_tags=1,can_review_feature_suggestions=1,
                       can_confirm_feature_tags=1,
                       can_view_suburb_data=1,
                       can_view_myschool_data=1,can_view_rankings=1,
                       can_view_collection_center=1,can_review_collected_data=1
                       WHERE id=?""",
                    (row["id"],),
                )
                if row["role_name"] == "Consultant":
                    conn.execute(
                        """UPDATE user_module_permissions SET can_create=1,can_edit=1,can_upload_documents=1,
                           can_review_features=1,can_edit_zone_data=1,can_edit_academic_info=1,can_upload_logo=1,
                           can_add_manual_feature_tags=1,can_edit_suburb_data=1,
                           can_manage_source_urls=1
                           WHERE id=?""",
                        (row["id"],),
                    )
            else:
                conn.execute("UPDATE user_module_permissions SET can_view=1,can_view_suburb_data=1,can_view_myschool_data=1 WHERE id=?", (row["id"],))

    def ensure_school_plan_report_action_permissions(self, conn, *, permission_ids=None):
        for row in conn.execute(
            "SELECT ump.id,r.name role_name,ump.can_access FROM user_module_permissions ump JOIN users u ON u.id=ump.user_id JOIN roles r ON r.id=u.role_id WHERE ump.module_key='school_plan_reports'"
        ):
            if permission_ids is not None and row["id"] not in permission_ids:
                continue
            if not row["can_access"]:
                continue
            if row["role_name"] in {"Admin", "Manager", "Consultant"}:
                conn.execute(
                    """UPDATE user_module_permissions
                       SET can_view=1,can_create=1,can_edit=1,can_open=1,
                           can_download=CASE WHEN ? IN ('Admin','Manager') THEN 1 ELSE can_download END,
                           can_view_reports=1,can_generate_reports=1
                       WHERE id=?""",
                    (row["role_name"], row["id"]),
                )
            else:
                conn.execute("UPDATE user_module_permissions SET can_view=1,can_open=1 WHERE id=?", (row["id"],))

    def ensure_school_suburb_seed(self, conn):
        stamp = now_iso()
        sources = [
            ("Australian Bureau of Statistics", "https://www.abs.gov.au/", "ABS", "Official suburb-level demographic source; configure exact dataset later."),
            ("Manual AAIE Entry", "", "Manual", "Manual fallback for staff-entered suburb data."),
            ("User Provided Source", "", "User Provided", "Placeholder for future confirmed websites or uploaded data."),
        ]
        for source_name, source_url, source_type, notes in sources:
            existing = conn.execute(
                """SELECT id,source_url,source_type,enabled,notes
                     FROM school_suburb_data_sources WHERE source_name=?""",
                (source_name,),
            ).fetchone()
            if not existing:
                conn.execute(
                    """INSERT INTO school_suburb_data_sources(source_name,source_url,source_type,enabled,notes,created_at,updated_at)
                       VALUES(?,?,?,1,?,?,?)""",
                    (source_name, source_url, source_type, notes, stamp, stamp),
                )

        sample_data = {
            ("South Yarra", "VIC", "3141"): {
                "population_total": 25400, "population_male": 12300, "population_female": 13100, "median_age": 35,
                "family_count": 5600, "couple_family_count": 4200, "single_parent_family_count": 820,
                "average_household_size": 2.0, "household_count": 12500,
                "median_personal_income_weekly": 1250, "median_family_income_weekly": 3100, "median_household_income_weekly": 2300,
                "english_only_percentage": 63.0, "non_english_language_percentage": 31.0,
                "top_languages_json": json.dumps(["Mandarin", "Greek", "Spanish"], ensure_ascii=False),
                "bachelor_or_above_percentage": 52.0, "year_12_completion_percentage": 86.0,
                "education_summary": "Sample data for UI testing only.",
                "born_overseas_percentage": 37.0,
                "top_birth_countries_json": json.dumps(["China", "India", "United Kingdom"], ensure_ascii=False),
                "ancestry_summary": "Sample cultural profile; verify before parent reports.",
                "separate_house_percentage": 18.0, "apartment_percentage": 62.0, "rented_percentage": 51.0, "owned_percentage": 39.0,
            },
            ("Balwyn North", "VIC", "3104"): {
                "population_total": 21500, "population_male": 10400, "population_female": 11100, "median_age": 42,
                "family_count": 6200, "couple_family_count": 5000, "single_parent_family_count": 720,
                "average_household_size": 2.8, "household_count": 7600,
                "median_personal_income_weekly": 980, "median_family_income_weekly": 2950, "median_household_income_weekly": 2400,
                "english_only_percentage": 52.0, "non_english_language_percentage": 43.0,
                "top_languages_json": json.dumps(["Mandarin", "Cantonese", "Greek"], ensure_ascii=False),
                "bachelor_or_above_percentage": 48.0, "year_12_completion_percentage": 88.0,
                "education_summary": "Sample data for UI testing only.",
                "born_overseas_percentage": 42.0,
                "top_birth_countries_json": json.dumps(["China", "Malaysia", "India"], ensure_ascii=False),
                "ancestry_summary": "Sample cultural profile; verify before parent reports.",
                "separate_house_percentage": 74.0, "apartment_percentage": 9.0, "rented_percentage": 18.0, "owned_percentage": 76.0,
            },
            ("Caulfield", "VIC", "3162"): {
                "population_total": 6100, "population_male": 2950, "population_female": 3150, "median_age": 38,
                "family_count": 1500, "couple_family_count": 1180, "single_parent_family_count": 210,
                "average_household_size": 2.2, "household_count": 2800,
                "median_personal_income_weekly": 1080, "median_family_income_weekly": 2880, "median_household_income_weekly": 2100,
                "english_only_percentage": 57.0, "non_english_language_percentage": 38.0,
                "top_languages_json": json.dumps(["Mandarin", "Russian", "Hebrew"], ensure_ascii=False),
                "bachelor_or_above_percentage": 49.0, "year_12_completion_percentage": 84.0,
                "education_summary": "Sample data for UI testing only.",
                "born_overseas_percentage": 39.0,
                "top_birth_countries_json": json.dumps(["China", "South Africa", "India"], ensure_ascii=False),
                "ancestry_summary": "Sample cultural profile; verify before parent reports.",
                "separate_house_percentage": 34.0, "apartment_percentage": 44.0, "rented_percentage": 42.0, "owned_percentage": 49.0,
            },
            ("Burwood", "VIC", "3125"): {
                "population_total": 16000, "population_male": 7800, "population_female": 8200, "median_age": 34,
                "family_count": 3900, "couple_family_count": 2950, "single_parent_family_count": 520,
                "average_household_size": 2.5, "household_count": 6500,
                "median_personal_income_weekly": 860, "median_family_income_weekly": 2380, "median_household_income_weekly": 1800,
                "english_only_percentage": 49.0, "non_english_language_percentage": 47.0,
                "top_languages_json": json.dumps(["Mandarin", "Cantonese", "Hindi"], ensure_ascii=False),
                "bachelor_or_above_percentage": 43.0, "year_12_completion_percentage": 82.0,
                "education_summary": "Sample data for UI testing only.",
                "born_overseas_percentage": 45.0,
                "top_birth_countries_json": json.dumps(["China", "India", "Malaysia"], ensure_ascii=False),
                "ancestry_summary": "Sample cultural profile; verify before parent reports.",
                "separate_house_percentage": 48.0, "apartment_percentage": 24.0, "rented_percentage": 39.0, "owned_percentage": 55.0,
            },
        }
        for (suburb, state, postcode), data in sample_data.items():
            normalized = re.sub(r"\s+", " ", suburb.strip()).lower()
            conn.execute(
                """INSERT INTO school_suburbs(suburb,state,postcode,country,normalized_suburb_name,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?)
                   ON CONFLICT(normalized_suburb_name,state,postcode) DO NOTHING""",
                (suburb, state, postcode, "Australia", normalized, stamp, stamp),
            )
            suburb_id = conn.execute(
                "SELECT id FROM school_suburbs WHERE normalized_suburb_name=? AND state=? AND postcode=?",
                (normalized, state, postcode),
            ).fetchone()["id"]
            insert_data = dict(data)
            insert_data.update({
                "suburb_id": suburb_id, "data_year": "2026", "source_name": "Manual AAIE Entry",
                "source_url": "", "source_type": "Sample", "fetched_at": stamp,
                "reviewed_status": "Needs Review", "confidence_score": 60,
                "other_indicators_json": json.dumps({"sample_record": True}, ensure_ascii=False),
                "notes": "Phase 1.2 sample suburb data only; verify with ABS or confirmed source before using in parent reports.",
                "created_at": stamp, "updated_at": stamp,
            })
            existing = conn.execute(
                "SELECT * FROM school_suburb_data WHERE suburb_id=? AND data_year='2026' AND source_type='Sample'",
                (suburb_id,),
            ).fetchone()
            columns = list(insert_data.keys())
            if not existing:
                conn.execute(
                    f"INSERT INTO school_suburb_data({','.join(columns)}) VALUES({','.join('?' for _ in columns)})",
                    tuple(insert_data[column] for column in columns),
                )
            conn.execute(
                """UPDATE schools
                   SET suburb_id=?,
                       suburb_data_status=COALESCE(NULLIF(suburb_data_status,''),'Needs Review'),
                       suburb_data_last_updated=COALESCE(suburb_data_last_updated, ?)
                   WHERE suburb_id IS NULL
                     AND lower(COALESCE(suburb,''))=? AND upper(COALESCE(state,''))=? AND COALESCE(postcode,'')=?""",
                (suburb_id, stamp, normalized, state, postcode),
            )

    def ensure_nullable_contract_student_id(self, conn):
        legacy = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='contracts_legacy_notnull'").fetchone()
        current = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='contracts'").fetchone()
        if legacy and current:
            current_count = conn.execute("SELECT COUNT(*) AS c FROM contracts").fetchone()["c"]
            if current_count == 0:
                old_names = {row["name"] for row in conn.execute("PRAGMA table_info(contracts_legacy_notnull)")}
                new_names = [row["name"] for row in conn.execute("PRAGMA table_info(contracts)").fetchall()]
                common = [name for name in new_names if name in old_names]
                conn.execute(
                    f"INSERT INTO contracts({','.join(common)}) SELECT {','.join(common)} FROM contracts_legacy_notnull"
                )
            return
        table = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='contracts'").fetchone()
        if not table:
            return
        columns = conn.execute("PRAGMA table_info(contracts)").fetchall()
        student_col = next((row for row in columns if row["name"] == "student_id"), None)
        if not student_col or int(student_col["notnull"] or 0) == 0:
            return
        conn.execute("ALTER TABLE contracts RENAME TO contracts_legacy_notnull")
        conn.execute(
            """CREATE TABLE contracts (
              id INTEGER PRIMARY KEY, student_id TEXT REFERENCES students(student_id),
              contract_uid TEXT UNIQUE, is_standalone_contract INTEGER NOT NULL DEFAULT 0,
              standalone_data_json TEXT,
              contract_category TEXT NOT NULL, contract_type TEXT NOT NULL, contract_title TEXT NOT NULL,
              contract_status TEXT NOT NULL DEFAULT 'Draft', contract_date TEXT, service_type TEXT,
              service_scope_json TEXT NOT NULL DEFAULT '[]', other_service_scope_text TEXT,
              service_fee_currency TEXT, consulting_and_visa_service_fee TEXT,
              discount_amount TEXT, final_fee_amount TEXT, payment_due_date TEXT,
              refund_policy_type TEXT, refund_policy_text TEXT, refusal_deduction_currency TEXT,
              refusal_deduction_amount TEXT, refusal_refund_currency TEXT, refusal_refund_amount TEXT,
              refund_notes TEXT, special_terms TEXT, parent_signer_name TEXT,
              parent_phone TEXT, parent_email TEXT, parent_address TEXT, notes TEXT, contract_file_path TEXT,
              signed_file_path TEXT, final_signed_file_path TEXT, template_version TEXT, generated_by INTEGER REFERENCES users(id),
              generated_at TEXT, signed_at TEXT, fully_executed_at TEXT, updated_at TEXT NOT NULL
            )"""
        )
        old_names = {row["name"] for row in columns}
        new_names = [row["name"] for row in conn.execute("PRAGMA table_info(contracts)").fetchall()]
        common = [name for name in new_names if name in old_names]
        conn.execute(
            f"INSERT INTO contracts({','.join(common)}) SELECT {','.join(common)} FROM contracts_legacy_notnull"
        )

    def ensure_manual_placements_contract_fk(self, conn):
        table = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='contract_manual_placements'").fetchone()
        if not table:
            return
        foreign_tables = {row["table"] for row in conn.execute("PRAGMA foreign_key_list(contract_manual_placements)").fetchall()}
        student_col = next((row for row in conn.execute("PRAGMA table_info(contract_manual_placements)") if row["name"] == "student_id"), None)
        if "contracts_legacy_notnull" not in foreign_tables and (not student_col or int(student_col["notnull"] or 0) == 0):
            return
        rows = conn.execute("SELECT * FROM contract_manual_placements").fetchall()
        conn.execute("ALTER TABLE contract_manual_placements RENAME TO contract_manual_placements_legacy")
        conn.execute(
            """CREATE TABLE contract_manual_placements (
              id INTEGER PRIMARY KEY, contract_id INTEGER NOT NULL REFERENCES contracts(id),
              student_id TEXT REFERENCES students(student_id),
              source_file_path TEXT NOT NULL, output_file_path TEXT NOT NULL,
              page_number INTEGER NOT NULL, page_rotation INTEGER NOT NULL DEFAULT 0,
              stamp_x REAL NOT NULL, stamp_y REAL NOT NULL, stamp_width REAL NOT NULL, stamp_height REAL NOT NULL,
              signature_x REAL NOT NULL, signature_y REAL NOT NULL, signature_width REAL NOT NULL, signature_height REAL NOT NULL,
              date_x REAL NOT NULL, date_y REAL NOT NULL, date_font_size REAL NOT NULL,
              created_by INTEGER REFERENCES users(id), created_at TEXT NOT NULL
            )"""
        )
        if rows:
            old_names = {row["name"] for row in conn.execute("PRAGMA table_info(contract_manual_placements_legacy)")}
            new_names = [row["name"] for row in conn.execute("PRAGMA table_info(contract_manual_placements)").fetchall()]
            common = [name for name in new_names if name in old_names]
            conn.execute(
                f"INSERT INTO contract_manual_placements({','.join(common)}) SELECT {','.join(common)} FROM contract_manual_placements_legacy"
            )

    def ensure_visa_schema_v1_compatibility(self, conn):
        self.ensure_columns(conn, "visa_cases", {
            "application_id": "TEXT",
            "visa_case_name": "TEXT",
            "visa_type": "TEXT",
            "applicant_name": "TEXT",
            "relationship_to_student": "TEXT",
            "application_type": "TEXT",
            "responsible_consultant": "TEXT",
            "main_applicant": "TEXT",
            "guardian_applicant": "TEXT",
            "applicant_count": "INTEGER DEFAULT 1",
            "case_owner": "TEXT",
            "target_lodgement_date": "TEXT",
            "actual_lodgement_date": "TEXT",
            "result_date": "TEXT",
            "result_status": "TEXT DEFAULT 'Pending'",
            "case_type": "TEXT DEFAULT 'Student Visa'",
            "status": "TEXT DEFAULT 'Draft'",
            "visa_subclass": "TEXT",
            "lodgement_date": "TEXT",
            "grant_date": "TEXT",
            "expiry_date": "TEXT",
            "refusal_date": "TEXT",
            "s56_due_date": "TEXT",
            "decision_status": "TEXT DEFAULT 'Pending'",
            "notes": "TEXT",
            "created_at": "TEXT",
            "updated_at": "TEXT",
            "archived_at": "TEXT",
        })
        self.ensure_columns(conn, "visa_case_document_links", {
            "student_document_id": "INTEGER",
            "document_role": "TEXT",
            "status": "TEXT DEFAULT 'Linked'",
            "notes": "TEXT",
            "created_at": "TEXT",
            "updated_at": "TEXT",
        })
        self.ensure_columns(conn, "visa_checklist_items", {
            "item_key": "TEXT",
            "title": "TEXT",
            "status": "TEXT DEFAULT 'Missing'",
            "required": "INTEGER DEFAULT 1",
            "due_date": "TEXT",
            "completed_at": "TEXT",
            "notes": "TEXT",
            "created_at": "TEXT",
            "updated_at": "TEXT",
        })
        self.ensure_columns(conn, "visa_evidence_profiles", {
            "visa_case_id": "INTEGER",
            "total_funds_amount": "TEXT",
            "total_funds_currency": "TEXT",
            "funds_holder": "TEXT",
            "funds_country": "TEXT",
            "funds_bank_name": "TEXT",
            "funds_source_explanation": "TEXT",
            "has_large_recent_transfer": "TEXT",
            "large_transfer_explanation": "TEXT",
            "can_provide_deposit_certificate": "TEXT",
            "can_provide_bank_statement": "TEXT",
            "father_employer": "TEXT",
            "father_position": "TEXT",
            "father_annual_income": "TEXT",
            "father_monthly_income": "TEXT",
            "father_income_source": "TEXT",
            "father_can_provide_income_certificate": "TEXT",
            "father_can_provide_salary_statement": "TEXT",
            "father_can_provide_tax_record": "TEXT",
            "mother_employer": "TEXT",
            "mother_position": "TEXT",
            "mother_annual_income": "TEXT",
            "mother_monthly_income": "TEXT",
            "mother_income_source": "TEXT",
            "mother_can_provide_income_certificate": "TEXT",
            "mother_can_provide_salary_statement": "TEXT",
            "mother_can_provide_tax_record": "TEXT",
            "family_annual_income_total": "TEXT",
            "family_monthly_income_total": "TEXT",
            "rental_income_total": "TEXT",
            "investment_income_total": "TEXT",
            "business_income_total": "TEXT",
            "income_stability_notes": "TEXT",
            "income_funds_match_notes": "TEXT",
            "has_property": "TEXT",
            "property_summary": "TEXT",
            "property_estimated_total_value": "TEXT",
            "property_mortgage_total": "TEXT",
            "has_company_or_business": "TEXT",
            "company_business_summary": "TEXT",
            "has_investment_assets": "TEXT",
            "investment_assets_summary": "TEXT",
            "total_asset_notes": "TEXT",
            "has_property_in_home_country": "TEXT",
            "has_company_in_home_country": "TEXT",
            "has_job_in_home_country": "TEXT",
            "has_elderly_family_members_in_home_country": "TEXT",
            "elderly_family_details": "TEXT",
            "has_other_children": "TEXT",
            "other_children_details": "TEXT",
            "close_relatives_in_home_country": "TEXT",
            "family_responsibility_notes": "TEXT",
            "return_home_plan": "TEXT",
            "home_country_ties_summary": "TEXT",
            "why_study_in_australia": "TEXT",
            "why_this_school": "TEXT",
            "why_this_city": "TEXT",
            "why_this_year_level": "TEXT",
            "why_not_continue_in_home_country": "TEXT",
            "student_english_level": "TEXT",
            "needs_english_program": "TEXT",
            "future_study_plan": "TEXT",
            "future_career_plan": "TEXT",
            "plan_after_completion": "TEXT",
            "family_support_plan": "TEXT",
            "has_previous_visa_refusal": "TEXT",
            "refusal_details": "TEXT",
            "has_overstay_history": "TEXT",
            "has_visa_cancellation_history": "TEXT",
            "parents_divorced_or_separated": "TEXT",
            "single_parent_application": "TEXT",
            "non_parent_funds_provider": "TEXT",
            "unusual_funds_notes": "TEXT",
            "study_gap_notes": "TEXT",
            "age_or_year_level_risk_notes": "TEXT",
            "other_risk_notes": "TEXT",
            "created_at": "TEXT",
            "updated_at": "TEXT",
        })
        self.ensure_columns(conn, "visa_evidence_financial_items", {
            "visa_case_id": "INTEGER",
            "item_type": "TEXT",
            "provider": "TEXT",
            "amount": "TEXT",
            "currency": "TEXT",
            "frequency": "TEXT",
            "annualized_amount": "TEXT",
            "source_country": "TEXT",
            "evidence_available": "TEXT",
            "evidence_notes": "TEXT",
            "notes": "TEXT",
            "is_archived": "INTEGER DEFAULT 0",
            "created_at": "TEXT",
            "updated_at": "TEXT",
        })
        self.ensure_columns(conn, "visa_evidence_asset_items", {
            "visa_case_id": "INTEGER",
            "asset_type": "TEXT",
            "owner": "TEXT",
            "asset_name": "TEXT",
            "estimated_value": "TEXT",
            "currency": "TEXT",
            "country_or_region": "TEXT",
            "city": "TEXT",
            "evidence_available": "TEXT",
            "evidence_notes": "TEXT",
            "liquidity_status": "TEXT",
            "notes": "TEXT",
            "is_archived": "INTEGER DEFAULT 0",
            "created_at": "TEXT",
            "updated_at": "TEXT",
        })
        self.ensure_columns(conn, "visa_information_collection_packages", {
            "visa_case_id": "INTEGER",
            "status": "TEXT DEFAULT 'Not Started'",
            "notes": "TEXT",
            "created_by": "INTEGER",
            "created_at": "TEXT",
            "updated_at": "TEXT",
        })
        self.ensure_columns(conn, "visa_information_collection_respondents", {
            "package_id": "INTEGER",
            "visa_case_id": "INTEGER",
            "respondent_type": "TEXT",
            "respondent_name": "TEXT",
            "token": "TEXT",
            "token_created_at": "TEXT",
            "token_status": "TEXT DEFAULT 'Active'",
            "status": "TEXT DEFAULT 'Not Started'",
            "progress_percent": "INTEGER DEFAULT 0",
            "last_saved_at": "TEXT",
            "submitted_at": "TEXT",
            "expires_at": "TEXT",
            "invalidated_at": "TEXT",
            "bound_wechat_openid": "TEXT",
            "bound_wechat_unionid": "TEXT",
            "bound_wechat_nickname": "TEXT",
            "bound_wechat_avatar_url": "TEXT",
            "bound_wechat_at": "TEXT",
            "wechat_unbound_at": "TEXT",
            "last_verified_at": "TEXT",
            "created_at": "TEXT",
            "updated_at": "TEXT",
        })
        self.ensure_columns(conn, "visa_information_collection_access_logs", {
            "respondent_id": "INTEGER",
            "visa_case_id": "INTEGER",
            "event_type": "TEXT",
            "event_at": "TEXT",
            "ip_address": "TEXT",
            "user_agent": "TEXT",
            "device_type": "TEXT",
            "wechat_openid": "TEXT",
            "wechat_unionid": "TEXT",
            "wechat_nickname": "TEXT",
            "metadata_json": "TEXT",
        })
        self.ensure_columns(conn, "visa_form_assignments", {
            "package_id": "INTEGER",
            "respondent_id": "INTEGER",
            "visa_case_id": "INTEGER",
            "form_type": "TEXT",
            "template_version": "TEXT",
            "status": "TEXT DEFAULT 'Not Started'",
            "progress_percent": "INTEGER DEFAULT 0",
            "last_saved_at": "TEXT",
            "submitted_at": "TEXT",
            "created_at": "TEXT",
            "updated_at": "TEXT",
        })
        self.ensure_columns(conn, "visa_form_drafts", {
            "assignment_id": "INTEGER",
            "respondent_id": "INTEGER",
            "visa_case_id": "INTEGER",
            "draft_json": "TEXT DEFAULT '{}'",
            "status": "TEXT DEFAULT 'Draft'",
            "progress_percent": "INTEGER DEFAULT 0",
            "last_saved_at": "TEXT",
            "submitted_at": "TEXT",
            "created_at": "TEXT",
            "updated_at": "TEXT",
        })
        self.ensure_visa_schema_v1_indexes(conn)

    def ensure_visa_schema_v1_indexes(self, conn):
        index_specs = [
            ("idx_visa_cases_student", "visa_cases", ["student_id"]),
            ("idx_visa_cases_application", "visa_cases", ["application_id"]),
            ("idx_visa_cases_status", "visa_cases", ["status"]),
            ("idx_visa_cases_expiry", "visa_cases", ["expiry_date"]),
            ("idx_visa_case_document_links_case", "visa_case_document_links", ["visa_case_id"]),
            ("idx_visa_case_document_links_document", "visa_case_document_links", ["student_document_id"]),
            ("idx_visa_checklist_items_case", "visa_checklist_items", ["visa_case_id"]),
            ("idx_visa_checklist_items_key", "visa_checklist_items", ["item_key"]),
            ("idx_visa_evidence_profiles_case", "visa_evidence_profiles", ["visa_case_id"]),
            ("idx_visa_evidence_financial_items_case", "visa_evidence_financial_items", ["visa_case_id"]),
            ("idx_visa_evidence_asset_items_case", "visa_evidence_asset_items", ["visa_case_id"]),
            ("idx_visa_information_collection_packages_case", "visa_information_collection_packages", ["visa_case_id"]),
            ("idx_visa_information_collection_respondents_package", "visa_information_collection_respondents", ["package_id"]),
            ("idx_visa_information_collection_respondents_token", "visa_information_collection_respondents", ["token"]),
            ("idx_visa_information_collection_access_logs_respondent", "visa_information_collection_access_logs", ["respondent_id"]),
            ("idx_visa_information_collection_access_logs_case", "visa_information_collection_access_logs", ["visa_case_id"]),
            ("idx_visa_form_assignments_respondent", "visa_form_assignments", ["respondent_id"]),
            ("idx_visa_form_assignments_case", "visa_form_assignments", ["visa_case_id"]),
            ("idx_visa_form_drafts_assignment", "visa_form_drafts", ["assignment_id"]),
        ]
        for index_name, table_name, columns in index_specs:
            existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table_name})")}
            if all(column in existing for column in columns):
                conn.execute(
                    f"CREATE INDEX IF NOT EXISTS {index_name} "
                    f"ON {table_name}({','.join(columns)})"
                )

    def ensure_columns(self, conn, table, columns):
        existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        for name, definition in columns.items():
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")

    def one(self, sql, params=()):
        start = time.perf_counter()
        error = None
        conn = self.connect()
        try:
            return conn.execute(sql, params).fetchone()
        except sqlite3.OperationalError as exc:
            error = exc
            if "locked" in str(exc).lower():
                _db_perf_log("db_locked", operation="one", elapsed_ms=round((time.perf_counter() - start) * 1000, 2), sql=_db_sql_summary(sql))
            raise
        finally:
            self._release_connection(conn)
            elapsed = (time.perf_counter() - start) * 1000
            if error is None and elapsed >= _db_slow_threshold_ms():
                _db_perf_log("db_slow_query", operation="one", elapsed_ms=round(elapsed, 2), sql=_db_sql_summary(sql))

    def all(self, sql, params=()):
        start = time.perf_counter()
        error = None
        conn = self.connect()
        try:
            return conn.execute(sql, params).fetchall()
        except sqlite3.OperationalError as exc:
            error = exc
            if "locked" in str(exc).lower():
                _db_perf_log("db_locked", operation="all", elapsed_ms=round((time.perf_counter() - start) * 1000, 2), sql=_db_sql_summary(sql))
            raise
        finally:
            self._release_connection(conn)
            elapsed = (time.perf_counter() - start) * 1000
            if error is None and elapsed >= _db_slow_threshold_ms():
                _db_perf_log("db_slow_query", operation="all", elapsed_ms=round(elapsed, 2), sql=_db_sql_summary(sql))

    def execute(self, sql, params=()):
        start = time.perf_counter()
        error = None
        try:
            with self.transaction() as conn:
                cur = conn.execute(sql, params)
                return cur.lastrowid
        except sqlite3.OperationalError as exc:
            error = exc
            if "locked" in str(exc).lower():
                _db_perf_log("db_locked", operation="execute", elapsed_ms=round((time.perf_counter() - start) * 1000, 2), sql=_db_sql_summary(sql))
            raise
        finally:
            elapsed = (time.perf_counter() - start) * 1000
            if error is None and elapsed >= _db_slow_threshold_ms():
                _db_perf_log("db_slow_query", operation="execute", elapsed_ms=round(elapsed, 2), sql=_db_sql_summary(sql))

    def setting(self, key, default=None):
        row = self.one("SELECT value FROM system_settings WHERE key=?", (key,))
        return row["value"] if row else default

    def set_setting(self, key, value, user_id=None):
        self.execute(
            """INSERT INTO system_settings(key,value,updated_by,updated_at) VALUES(?,?,?,?)
               ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_by=excluded.updated_by,updated_at=excluded.updated_at""",
            (key, str(value), user_id, now_iso()),
        )

    def audit(self, user, action, module, record_id=None, file_path=None, old_value=None,
              new_value=None, ip=None, device=None, status="success", error_message=None,
              student_id=None, contract_id=None):
        # Only optional read telemetry is omitted during the bounded release window.
        # Authentication events remain authoritative and are preserved on rollback.
        conn = self.connect()
        try:
            gate = getattr(conn, "write_gate", None)
            if gate is not None and gate.status()["state"] != "OPEN":
                if gate.current_kind() == "read" and action == "download":
                    return
                if gate.current_kind() == "auth":
                    if (module != "auth" or action not in ("login_success", "login_failed", "logout")
                            or any((file_path, old_value, new_value, student_id, contract_id))):
                        from business_write_gate import MaintenanceError
                        raise MaintenanceError("auth_audit_scope_invalid")
        finally:
            self._release_connection(conn)
        self.execute(
            """INSERT INTO audit_logs(user_id,username,action_type,module_name,module_key,record_id,file_path,
               old_value,new_value,student_id,contract_id,ip_address,user_agent,device_info,status,error_message,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                user["id"] if user else None,
                user["username"] if user else "public",
                action, module, module, str(record_id or ""), str(file_path or ""),
                old_value, new_value, str(student_id or ""), str(contract_id or ""),
                ip, device, device, status, error_message, now_iso(),
            ),
        )
