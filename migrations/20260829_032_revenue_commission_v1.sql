CREATE TABLE revenue_module_state (
  id INTEGER PRIMARY KEY CHECK(id=1),
  status TEXT NOT NULL CHECK(status IN ('disabled','active','read_only')),
  cutover_at TEXT,
  activated_by INTEGER REFERENCES users(id) ON DELETE RESTRICT,
  updated_at TEXT NOT NULL
);

INSERT INTO revenue_module_state(id,status,cutover_at,activated_by,updated_at)
VALUES(1,'disabled',NULL,NULL,'2026-08-29T00:00:00Z');

CREATE TABLE revenue_number_sequences (
  id INTEGER PRIMARY KEY,
  uid TEXT NOT NULL UNIQUE,
  document_kind TEXT NOT NULL,
  financial_year INTEGER NOT NULL CHECK(financial_year BETWEEN 2000 AND 2200),
  next_number INTEGER NOT NULL CHECK(next_number>=1),
  updated_at TEXT NOT NULL,
  UNIQUE(document_kind,financial_year)
);

CREATE TABLE revenue_idempotency_receipts (
  id INTEGER PRIMARY KEY,
  uid TEXT NOT NULL UNIQUE,
  idempotency_key TEXT NOT NULL UNIQUE,
  command_type TEXT NOT NULL,
  aggregate_type TEXT NOT NULL,
  aggregate_uid TEXT,
  request_hash TEXT NOT NULL,
  result_json TEXT,
  actor_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
  occurred_at TEXT NOT NULL
);

CREATE TABLE revenue_outbox_events (
  id INTEGER PRIMARY KEY,
  uid TEXT NOT NULL UNIQUE,
  aggregate_type TEXT NOT NULL,
  aggregate_uid TEXT NOT NULL,
  event_type TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  dispatched_at TEXT,
  attempt_count INTEGER NOT NULL DEFAULT 0 CHECK(attempt_count>=0),
  last_error_code TEXT
);

CREATE TABLE revenue_audit_events (
  id INTEGER PRIMARY KEY,
  uid TEXT NOT NULL UNIQUE,
  action TEXT NOT NULL,
  aggregate_type TEXT NOT NULL,
  aggregate_uid TEXT NOT NULL,
  before_json TEXT,
  after_json TEXT,
  actor_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
  occurred_at TEXT NOT NULL
);

CREATE TABLE revenue_exceptions (
  id INTEGER PRIMARY KEY,
  uid TEXT NOT NULL UNIQUE,
  exception_code TEXT NOT NULL,
  severity TEXT NOT NULL CHECK(severity IN ('info','warning','blocking')),
  aggregate_type TEXT NOT NULL,
  aggregate_uid TEXT NOT NULL,
  message TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','resolved','dismissed')),
  opened_at TEXT NOT NULL,
  resolved_at TEXT,
  resolved_by INTEGER REFERENCES users(id) ON DELETE RESTRICT,
  resolution_note TEXT
);

CREATE TABLE revenue_documents (
  id INTEGER PRIMARY KEY,
  uid TEXT NOT NULL UNIQUE,
  document_kind TEXT NOT NULL,
  version_no INTEGER NOT NULL DEFAULT 1 CHECK(version_no>=1),
  storage_path TEXT NOT NULL,
  sha256 TEXT NOT NULL CHECK(length(sha256)=64),
  mime_type TEXT NOT NULL,
  size_bytes INTEGER NOT NULL CHECK(size_bytes>=0),
  source_kind TEXT NOT NULL CHECK(source_kind IN ('upload','generated','imported')),
  created_by INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
  created_at TEXT NOT NULL,
  UNIQUE(document_kind,storage_path,version_no)
);

CREATE TABLE revenue_commercial_cases (
  id INTEGER PRIMARY KEY,
  uid TEXT NOT NULL UNIQUE,
  application_cycle_id INTEGER NOT NULL UNIQUE REFERENCES application_cycles(id) ON DELETE RESTRICT,
  student_id TEXT NOT NULL REFERENCES students(student_id) ON DELETE RESTRICT,
  status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','on_hold','closed')),
  existing_aaie_k12 INTEGER NOT NULL CHECK(existing_aaie_k12 IN (0,1)),
  classification_evidence_json TEXT NOT NULL,
  opened_by INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
  opened_at TEXT NOT NULL,
  closed_at TEXT
);

CREATE TABLE revenue_billing_accounts (
  id INTEGER PRIMARY KEY,
  uid TEXT NOT NULL UNIQUE,
  account_type TEXT NOT NULL CHECK(account_type IN ('individual','family','company')),
  display_name TEXT NOT NULL,
  currency_code TEXT NOT NULL DEFAULT 'AUD' CHECK(length(currency_code)=3),
  status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','inactive')),
  created_by INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
  created_at TEXT NOT NULL
);

CREATE TABLE revenue_billing_parties (
  id INTEGER PRIMARY KEY,
  uid TEXT NOT NULL UNIQUE,
  billing_account_id INTEGER NOT NULL REFERENCES revenue_billing_accounts(id) ON DELETE RESTRICT,
  party_role TEXT NOT NULL CHECK(party_role IN ('father','mother','guardian','company','student','other')),
  legal_name TEXT NOT NULL,
  email TEXT,
  phone TEXT,
  billing_address TEXT,
  abn TEXT,
  is_primary INTEGER NOT NULL DEFAULT 0 CHECK(is_primary IN (0,1)),
  created_at TEXT NOT NULL
);

CREATE UNIQUE INDEX uq_revenue_billing_primary_party
ON revenue_billing_parties(billing_account_id)
WHERE is_primary=1;

CREATE TABLE revenue_billing_account_students (
  id INTEGER PRIMARY KEY,
  uid TEXT NOT NULL UNIQUE,
  billing_account_id INTEGER NOT NULL REFERENCES revenue_billing_accounts(id) ON DELETE RESTRICT,
  student_id TEXT NOT NULL REFERENCES students(student_id) ON DELETE RESTRICT,
  effective_from TEXT NOT NULL,
  effective_to TEXT,
  created_at TEXT NOT NULL,
  UNIQUE(billing_account_id,student_id,effective_from)
);

CREATE TABLE revenue_entities (
  id INTEGER PRIMARY KEY,
  uid TEXT NOT NULL UNIQUE,
  entity_type TEXT NOT NULL CHECK(entity_type IN ('school','higher_education_provider','master_agent','other')),
  legal_name TEXT NOT NULL,
  trading_name TEXT,
  abn TEXT,
  currency_code TEXT NOT NULL DEFAULT 'AUD' CHECK(length(currency_code)=3),
  status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','inactive')),
  created_by INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
  created_at TEXT NOT NULL
);

CREATE TABLE revenue_entity_provider_links (
  id INTEGER PRIMARY KEY,
  uid TEXT NOT NULL UNIQUE,
  revenue_entity_id INTEGER NOT NULL REFERENCES revenue_entities(id) ON DELETE RESTRICT,
  school_id INTEGER REFERENCES schools(id) ON DELETE RESTRICT,
  higher_education_provider_id INTEGER REFERENCES higher_education_providers(id) ON DELETE RESTRICT,
  effective_from TEXT NOT NULL,
  effective_to TEXT,
  created_at TEXT NOT NULL,
  CHECK((school_id IS NOT NULL)+(higher_education_provider_id IS NOT NULL)=1)
);

CREATE TABLE revenue_entity_contacts (
  id INTEGER PRIMARY KEY,
  uid TEXT NOT NULL UNIQUE,
  revenue_entity_id INTEGER NOT NULL REFERENCES revenue_entities(id) ON DELETE RESTRICT,
  contact_role TEXT NOT NULL,
  contact_name TEXT NOT NULL,
  email TEXT,
  phone TEXT,
  is_default_claim_recipient INTEGER NOT NULL DEFAULT 0 CHECK(is_default_claim_recipient IN (0,1)),
  created_at TEXT NOT NULL
);

CREATE TABLE revenue_enrolment_components (
  id INTEGER PRIMARY KEY,
  uid TEXT NOT NULL UNIQUE,
  commercial_case_id INTEGER NOT NULL REFERENCES revenue_commercial_cases(id) ON DELETE RESTRICT,
  component_kind TEXT NOT NULL CHECK(component_kind IN ('school','higher_education','package_segment')),
  source_kind TEXT NOT NULL,
  source_id INTEGER NOT NULL,
  final_acceptance_id INTEGER REFERENCES cycle_final_acceptances(id) ON DELETE RESTRICT,
  school_enrolment_id INTEGER REFERENCES student_school_enrolments(id) ON DELETE RESTRICT,
  higher_education_application_id INTEGER REFERENCES higher_education_applications(id) ON DELETE RESTRICT,
  higher_education_package_segment_id INTEGER REFERENCES higher_education_package_segments(id) ON DELETE RESTRICT,
  coe_oshc_id INTEGER REFERENCES coe_oshc(id) ON DELETE RESTRICT,
  status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','deferred','withdrawn','completed')),
  created_at TEXT NOT NULL,
  UNIQUE(commercial_case_id,source_kind,source_id)
);

CREATE TABLE revenue_commencement_facts (
  id INTEGER PRIMARY KEY,
  uid TEXT NOT NULL UNIQUE,
  enrolment_component_id INTEGER NOT NULL REFERENCES revenue_enrolment_components(id) ON DELETE RESTRICT,
  actual_commencement_date TEXT NOT NULL,
  evidence_document_id INTEGER REFERENCES revenue_documents(id) ON DELETE RESTRICT,
  source_kind TEXT NOT NULL CHECK(source_kind IN ('institution','admin_confirmed','imported')),
  status TEXT NOT NULL CHECK(status IN ('proposed','confirmed','superseded')),
  supersedes_id INTEGER REFERENCES revenue_commencement_facts(id) ON DELETE RESTRICT,
  confirmed_by INTEGER REFERENCES users(id) ON DELETE RESTRICT,
  confirmed_at TEXT,
  created_at TEXT NOT NULL
);

CREATE UNIQUE INDEX uq_revenue_commencement_confirmed
ON revenue_commencement_facts(enrolment_component_id)
WHERE status='confirmed';

CREATE TABLE revenue_tuition_facts (
  id INTEGER PRIMARY KEY,
  uid TEXT NOT NULL UNIQUE,
  enrolment_component_id INTEGER NOT NULL REFERENCES revenue_enrolment_components(id) ON DELETE RESTRICT,
  academic_period TEXT NOT NULL,
  fee_component TEXT NOT NULL,
  tuition_cents INTEGER NOT NULL CHECK(tuition_cents>=0),
  currency_code TEXT NOT NULL DEFAULT 'AUD' CHECK(length(currency_code)=3),
  authority_level TEXT NOT NULL CHECK(authority_level IN ('official','manual','estimated')),
  evidence_document_id INTEGER REFERENCES revenue_documents(id) ON DELETE RESTRICT,
  status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','superseded')),
  supersedes_id INTEGER REFERENCES revenue_tuition_facts(id) ON DELETE RESTRICT,
  confirmed_by INTEGER REFERENCES users(id) ON DELETE RESTRICT,
  created_at TEXT NOT NULL
);

CREATE UNIQUE INDEX uq_revenue_tuition_active
ON revenue_tuition_facts(enrolment_component_id,academic_period,fee_component)
WHERE status='active';

CREATE TABLE revenue_tuition_adjustments (
  id INTEGER PRIMARY KEY,
  uid TEXT NOT NULL UNIQUE,
  tuition_fact_id INTEGER NOT NULL REFERENCES revenue_tuition_facts(id) ON DELETE RESTRICT,
  adjustment_kind TEXT NOT NULL CHECK(adjustment_kind IN ('scholarship','discount','waiver','correction')),
  amount_cents INTEGER NOT NULL CHECK(amount_cents>=0),
  commission_effect TEXT NOT NULL CHECK(commission_effect IN ('reduces_base','no_effect')),
  reason TEXT NOT NULL,
  approved_by INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
  approved_at TEXT NOT NULL
);

CREATE TABLE revenue_fee_rule_sets (
  id INTEGER PRIMARY KEY,
  uid TEXT NOT NULL UNIQUE,
  name TEXT NOT NULL UNIQUE,
  description TEXT,
  created_by INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
  created_at TEXT NOT NULL
);

CREATE TABLE revenue_fee_rule_versions (
  id INTEGER PRIMARY KEY,
  uid TEXT NOT NULL UNIQUE,
  rule_set_id INTEGER NOT NULL REFERENCES revenue_fee_rule_sets(id) ON DELETE RESTRICT,
  version_no INTEGER NOT NULL CHECK(version_no>=1),
  effective_from TEXT NOT NULL,
  effective_to TEXT,
  priority INTEGER NOT NULL DEFAULT 100,
  status TEXT NOT NULL CHECK(status IN ('draft','active','retired')),
  student_classification TEXT NOT NULL CHECK(student_classification IN ('any','new','existing')),
  education_stream TEXT NOT NULL CHECK(education_stream IN ('any','school_education','higher_education')),
  visa_scope TEXT NOT NULL DEFAULT 'any',
  created_by INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
  created_at TEXT NOT NULL,
  activated_by INTEGER REFERENCES users(id) ON DELETE RESTRICT,
  activated_at TEXT,
  UNIQUE(rule_set_id,version_no)
);

CREATE TABLE revenue_fee_rule_conditions (
  id INTEGER PRIMARY KEY,
  uid TEXT NOT NULL UNIQUE,
  rule_version_id INTEGER NOT NULL REFERENCES revenue_fee_rule_versions(id) ON DELETE RESTRICT,
  field_name TEXT NOT NULL,
  operator TEXT NOT NULL CHECK(operator IN ('eq','ne','in','not_in','gte','lte','exists')),
  comparison_value TEXT,
  sort_order INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
);

CREATE TABLE revenue_fee_rule_milestones (
  id INTEGER PRIMARY KEY,
  uid TEXT NOT NULL UNIQUE,
  rule_version_id INTEGER NOT NULL REFERENCES revenue_fee_rule_versions(id) ON DELETE RESTRICT,
  milestone_code TEXT NOT NULL,
  calculation_kind TEXT NOT NULL CHECK(calculation_kind IN ('fixed','percentage')),
  fixed_cents INTEGER CHECK(fixed_cents IS NULL OR fixed_cents>=0),
  rate_basis_points INTEGER CHECK(rate_basis_points IS NULL OR rate_basis_points BETWEEN 0 AND 10000),
  due_offset_days INTEGER NOT NULL DEFAULT 0,
  refundable INTEGER NOT NULL DEFAULT 0 CHECK(refundable IN (0,1)),
  sort_order INTEGER NOT NULL DEFAULT 0,
  UNIQUE(rule_version_id,milestone_code)
);

CREATE TABLE revenue_payment_methods (
  id INTEGER PRIMARY KEY,
  uid TEXT NOT NULL UNIQUE,
  method_code TEXT NOT NULL UNIQUE,
  display_name TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','inactive'))
);

CREATE TABLE revenue_payment_method_rule_versions (
  id INTEGER PRIMARY KEY,
  uid TEXT NOT NULL UNIQUE,
  payment_method_id INTEGER NOT NULL REFERENCES revenue_payment_methods(id) ON DELETE RESTRICT,
  effective_from TEXT NOT NULL,
  effective_to TEXT,
  gst_rate_basis_points INTEGER NOT NULL CHECK(gst_rate_basis_points BETWEEN 0 AND 10000),
  surcharge_basis_points INTEGER NOT NULL DEFAULT 0 CHECK(surcharge_basis_points BETWEEN 0 AND 10000),
  status TEXT NOT NULL CHECK(status IN ('draft','active','retired')),
  created_at TEXT NOT NULL
);

CREATE TABLE revenue_rule_snapshots (
  id INTEGER PRIMARY KEY,
  uid TEXT NOT NULL UNIQUE,
  rule_kind TEXT NOT NULL CHECK(rule_kind IN ('fee','commission','master_agent_share')),
  source_rule_uid TEXT NOT NULL,
  canonical_json TEXT NOT NULL,
  sha256 TEXT NOT NULL UNIQUE CHECK(length(sha256)=64),
  created_by INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
  created_at TEXT NOT NULL
);

CREATE TABLE revenue_fee_items (
  id INTEGER PRIMARY KEY,
  uid TEXT NOT NULL UNIQUE,
  commercial_case_id INTEGER NOT NULL REFERENCES revenue_commercial_cases(id) ON DELETE RESTRICT,
  billing_account_id INTEGER NOT NULL REFERENCES revenue_billing_accounts(id) ON DELETE RESTRICT,
  student_id TEXT NOT NULL REFERENCES students(student_id) ON DELETE RESTRICT,
  rule_snapshot_id INTEGER NOT NULL REFERENCES revenue_rule_snapshots(id) ON DELETE RESTRICT,
  milestone_code TEXT NOT NULL,
  description TEXT NOT NULL,
  net_cents INTEGER NOT NULL CHECK(net_cents>=0),
  gst_cents INTEGER NOT NULL CHECK(gst_cents>=0),
  gross_cents INTEGER NOT NULL CHECK(gross_cents=net_cents+gst_cents),
  currency_code TEXT NOT NULL DEFAULT 'AUD' CHECK(length(currency_code)=3),
  due_date TEXT,
  status TEXT NOT NULL DEFAULT 'unbilled' CHECK(status IN ('unbilled','invoiced','cancelled')),
  created_at TEXT NOT NULL
);

CREATE TABLE revenue_discounts (
  id INTEGER PRIMARY KEY,
  uid TEXT NOT NULL UNIQUE,
  billing_account_id INTEGER NOT NULL REFERENCES revenue_billing_accounts(id) ON DELETE RESTRICT,
  discount_kind TEXT NOT NULL CHECK(discount_kind IN ('fixed','percentage')),
  amount_cents INTEGER CHECK(amount_cents IS NULL OR amount_cents>=0),
  rate_basis_points INTEGER CHECK(rate_basis_points IS NULL OR rate_basis_points BETWEEN 0 AND 10000),
  reason TEXT NOT NULL,
  approved_by INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
  approved_at TEXT NOT NULL
);

CREATE TABLE revenue_discount_allocations (
  id INTEGER PRIMARY KEY,
  uid TEXT NOT NULL UNIQUE,
  discount_id INTEGER NOT NULL REFERENCES revenue_discounts(id) ON DELETE RESTRICT,
  fee_item_id INTEGER NOT NULL REFERENCES revenue_fee_items(id) ON DELETE RESTRICT,
  allocated_cents INTEGER NOT NULL CHECK(allocated_cents>0),
  created_at TEXT NOT NULL,
  UNIQUE(discount_id,fee_item_id)
);

CREATE TABLE revenue_refunds (
  id INTEGER PRIMARY KEY,
  uid TEXT NOT NULL UNIQUE,
  payment_uid TEXT NOT NULL,
  amount_cents INTEGER NOT NULL CHECK(amount_cents>0),
  reason TEXT NOT NULL,
  evidence_document_id INTEGER REFERENCES revenue_documents(id) ON DELETE RESTRICT,
  status TEXT NOT NULL CHECK(status IN ('requested','approved','paid','cancelled')),
  approved_by INTEGER REFERENCES users(id) ON DELETE RESTRICT,
  approved_at TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE revenue_contract_groups (
  id INTEGER PRIMARY KEY,
  uid TEXT NOT NULL UNIQUE,
  revenue_entity_id INTEGER NOT NULL REFERENCES revenue_entities(id) ON DELETE RESTRICT,
  contract_kind TEXT NOT NULL CHECK(contract_kind IN ('direct_commission','master_agent','other')),
  reference_code TEXT,
  created_by INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
  created_at TEXT NOT NULL
);

CREATE TABLE revenue_contract_versions (
  id INTEGER PRIMARY KEY,
  uid TEXT NOT NULL UNIQUE,
  contract_group_id INTEGER NOT NULL REFERENCES revenue_contract_groups(id) ON DELETE RESTRICT,
  version_no INTEGER NOT NULL CHECK(version_no>=1),
  document_id INTEGER NOT NULL UNIQUE REFERENCES revenue_documents(id) ON DELETE RESTRICT,
  effective_from TEXT NOT NULL,
  effective_to TEXT,
  signed_at TEXT,
  status TEXT NOT NULL CHECK(status IN ('draft','under_review','active','expired','superseded')),
  activated_by INTEGER REFERENCES users(id) ON DELETE RESTRICT,
  activated_at TEXT,
  created_at TEXT NOT NULL,
  UNIQUE(contract_group_id,version_no)
);

CREATE TABLE revenue_contract_relationships (
  id INTEGER PRIMARY KEY,
  uid TEXT NOT NULL UNIQUE,
  from_contract_version_id INTEGER NOT NULL REFERENCES revenue_contract_versions(id) ON DELETE RESTRICT,
  to_contract_version_id INTEGER NOT NULL REFERENCES revenue_contract_versions(id) ON DELETE RESTRICT,
  relationship_type TEXT NOT NULL CHECK(relationship_type IN ('renews','amends','supersedes')),
  created_at TEXT NOT NULL,
  UNIQUE(from_contract_version_id,to_contract_version_id,relationship_type)
);

CREATE TABLE revenue_contract_parse_runs (
  id INTEGER PRIMARY KEY,
  uid TEXT NOT NULL UNIQUE,
  contract_version_id INTEGER NOT NULL REFERENCES revenue_contract_versions(id) ON DELETE RESTRICT,
  provider_code TEXT NOT NULL,
  provider_model TEXT,
  status TEXT NOT NULL CHECK(status IN ('pending','suggested','failed','reviewed')),
  error_code TEXT,
  started_at TEXT NOT NULL,
  completed_at TEXT,
  created_by INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT
);

CREATE TABLE revenue_contract_extractions (
  id INTEGER PRIMARY KEY,
  uid TEXT NOT NULL UNIQUE,
  parse_run_id INTEGER NOT NULL REFERENCES revenue_contract_parse_runs(id) ON DELETE RESTRICT,
  field_name TEXT NOT NULL,
  suggested_value TEXT,
  page_number INTEGER CHECK(page_number IS NULL OR page_number>=1),
  clause_reference TEXT,
  evidence_text TEXT,
  confidence_basis_points INTEGER CHECK(confidence_basis_points IS NULL OR confidence_basis_points BETWEEN 0 AND 10000),
  review_status TEXT NOT NULL CHECK(review_status IN ('pending','confirmed','rejected','corrected')),
  confirmed_value TEXT,
  reviewed_by INTEGER REFERENCES users(id) ON DELETE RESTRICT,
  reviewed_at TEXT
);

CREATE TABLE revenue_commission_rule_versions (
  id INTEGER PRIMARY KEY,
  uid TEXT NOT NULL UNIQUE,
  contract_version_id INTEGER NOT NULL REFERENCES revenue_contract_versions(id) ON DELETE RESTRICT,
  version_no INTEGER NOT NULL CHECK(version_no>=1),
  effective_from TEXT NOT NULL,
  effective_to TEXT,
  priority INTEGER NOT NULL DEFAULT 100,
  status TEXT NOT NULL CHECK(status IN ('draft','active','retired')),
  rule_snapshot_id INTEGER REFERENCES revenue_rule_snapshots(id) ON DELETE RESTRICT,
  activated_by INTEGER REFERENCES users(id) ON DELETE RESTRICT,
  activated_at TEXT,
  created_at TEXT NOT NULL,
  UNIQUE(contract_version_id,version_no)
);

CREATE TABLE revenue_commission_rule_scopes (
  id INTEGER PRIMARY KEY,
  uid TEXT NOT NULL UNIQUE,
  commission_rule_version_id INTEGER NOT NULL REFERENCES revenue_commission_rule_versions(id) ON DELETE RESTRICT,
  scope_kind TEXT NOT NULL CHECK(scope_kind IN ('provider','school','program','student','all')),
  scope_value TEXT,
  priority INTEGER NOT NULL DEFAULT 100,
  created_at TEXT NOT NULL
);

CREATE TABLE revenue_commission_rule_fee_components (
  id INTEGER PRIMARY KEY,
  uid TEXT NOT NULL UNIQUE,
  commission_rule_version_id INTEGER NOT NULL REFERENCES revenue_commission_rule_versions(id) ON DELETE RESTRICT,
  fee_component TEXT NOT NULL,
  treatment TEXT NOT NULL CHECK(treatment IN ('included','excluded')),
  created_at TEXT NOT NULL,
  UNIQUE(commission_rule_version_id,fee_component)
);

CREATE TABLE revenue_claim_trigger_terms (
  id INTEGER PRIMARY KEY,
  uid TEXT NOT NULL UNIQUE,
  commission_rule_version_id INTEGER NOT NULL REFERENCES revenue_commission_rule_versions(id) ON DELETE RESTRICT,
  parent_term_id INTEGER REFERENCES revenue_claim_trigger_terms(id) ON DELETE RESTRICT,
  node_kind TEXT NOT NULL CHECK(node_kind IN ('group','condition')),
  boolean_operator TEXT CHECK(boolean_operator IS NULL OR boolean_operator IN ('and','or')),
  fact_name TEXT,
  comparison_operator TEXT,
  comparison_value TEXT,
  sort_order INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
);

CREATE TABLE revenue_claim_document_requirements (
  id INTEGER PRIMARY KEY,
  uid TEXT NOT NULL UNIQUE,
  commission_rule_version_id INTEGER NOT NULL REFERENCES revenue_commission_rule_versions(id) ON DELETE RESTRICT,
  document_kind TEXT NOT NULL,
  required INTEGER NOT NULL DEFAULT 1 CHECK(required IN (0,1)),
  created_at TEXT NOT NULL,
  UNIQUE(commission_rule_version_id,document_kind)
);

CREATE TABLE revenue_commission_rate_instalments (
  id INTEGER PRIMARY KEY,
  uid TEXT NOT NULL UNIQUE,
  commission_rule_version_id INTEGER NOT NULL REFERENCES revenue_commission_rule_versions(id) ON DELETE RESTRICT,
  instalment_no INTEGER NOT NULL CHECK(instalment_no>=1),
  academic_period TEXT NOT NULL,
  rate_basis_points INTEGER NOT NULL CHECK(rate_basis_points BETWEEN 0 AND 10000),
  fixed_cents INTEGER CHECK(fixed_cents IS NULL OR fixed_cents>=0),
  created_at TEXT NOT NULL,
  UNIQUE(commission_rule_version_id,instalment_no)
);

CREATE TABLE revenue_master_agent_share_rule_versions (
  id INTEGER PRIMARY KEY,
  uid TEXT NOT NULL UNIQUE,
  master_agent_entity_id INTEGER NOT NULL REFERENCES revenue_entities(id) ON DELETE RESTRICT,
  provider_entity_id INTEGER NOT NULL REFERENCES revenue_entities(id) ON DELETE RESTRICT,
  program_scope TEXT,
  effective_from TEXT NOT NULL,
  effective_to TEXT,
  share_basis_points INTEGER NOT NULL CHECK(share_basis_points BETWEEN 0 AND 10000),
  status TEXT NOT NULL CHECK(status IN ('draft','active','retired')),
  rule_snapshot_id INTEGER REFERENCES revenue_rule_snapshots(id) ON DELETE RESTRICT,
  activated_by INTEGER REFERENCES users(id) ON DELETE RESTRICT,
  activated_at TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE revenue_commission_lines (
  id INTEGER PRIMARY KEY,
  uid TEXT NOT NULL UNIQUE,
  commercial_case_id INTEGER NOT NULL REFERENCES revenue_commercial_cases(id) ON DELETE RESTRICT,
  enrolment_component_id INTEGER NOT NULL REFERENCES revenue_enrolment_components(id) ON DELETE RESTRICT,
  revenue_entity_id INTEGER NOT NULL REFERENCES revenue_entities(id) ON DELETE RESTRICT,
  commission_rule_version_id INTEGER NOT NULL REFERENCES revenue_commission_rule_versions(id) ON DELETE RESTRICT,
  rule_snapshot_id INTEGER NOT NULL REFERENCES revenue_rule_snapshots(id) ON DELETE RESTRICT,
  commissionable_cents INTEGER NOT NULL CHECK(commissionable_cents>=0),
  net_cents INTEGER NOT NULL CHECK(net_cents>=0),
  gst_cents INTEGER NOT NULL CHECK(gst_cents>=0),
  gross_cents INTEGER NOT NULL CHECK(gross_cents=net_cents+gst_cents),
  currency_code TEXT NOT NULL DEFAULT 'AUD' CHECK(length(currency_code)=3),
  status TEXT NOT NULL CHECK(status IN ('forecast','accrued','claimable','claimed','paid','closed')),
  calculated_at TEXT NOT NULL
);

CREATE TABLE revenue_commission_instalments (
  id INTEGER PRIMARY KEY,
  uid TEXT NOT NULL UNIQUE,
  commission_line_id INTEGER NOT NULL REFERENCES revenue_commission_lines(id) ON DELETE RESTRICT,
  instalment_no INTEGER NOT NULL CHECK(instalment_no>=1),
  academic_period TEXT NOT NULL,
  net_cents INTEGER NOT NULL CHECK(net_cents>=0),
  gst_cents INTEGER NOT NULL CHECK(gst_cents>=0),
  gross_cents INTEGER NOT NULL CHECK(gross_cents=net_cents+gst_cents),
  eligible_cents INTEGER NOT NULL DEFAULT 0 CHECK(eligible_cents>=0 AND eligible_cents<=gross_cents),
  claimed_cents INTEGER NOT NULL DEFAULT 0 CHECK(claimed_cents>=0 AND claimed_cents<=gross_cents),
  status TEXT NOT NULL CHECK(status IN ('forecast','accrued','eligible','partial','claimed','paid','closed')),
  created_at TEXT NOT NULL,
  UNIQUE(commission_line_id,instalment_no)
);

CREATE TABLE revenue_commission_adjustments (
  id INTEGER PRIMARY KEY,
  uid TEXT NOT NULL UNIQUE,
  commission_line_id INTEGER NOT NULL REFERENCES revenue_commission_lines(id) ON DELETE RESTRICT,
  adjustment_kind TEXT NOT NULL CHECK(adjustment_kind IN ('credit','debit','correction')),
  amount_cents INTEGER NOT NULL CHECK(amount_cents>0),
  reason TEXT NOT NULL,
  approved_by INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
  approved_at TEXT NOT NULL
);

CREATE TABLE revenue_clawbacks (
  id INTEGER PRIMARY KEY,
  uid TEXT NOT NULL UNIQUE,
  commission_line_id INTEGER NOT NULL REFERENCES revenue_commission_lines(id) ON DELETE RESTRICT,
  amount_cents INTEGER NOT NULL CHECK(amount_cents>0),
  reason_code TEXT NOT NULL,
  source_event_type TEXT NOT NULL,
  source_event_id INTEGER NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('open','invoiced','settled','waived')),
  approved_by INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
  approved_at TEXT NOT NULL
);

CREATE TABLE revenue_deferral_events (
  id INTEGER PRIMARY KEY,
  uid TEXT NOT NULL UNIQUE,
  commission_line_id INTEGER NOT NULL REFERENCES revenue_commission_lines(id) ON DELETE RESTRICT,
  previous_period TEXT NOT NULL,
  new_period TEXT NOT NULL,
  reason TEXT NOT NULL,
  actor_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
  occurred_at TEXT NOT NULL
);

CREATE TABLE revenue_forecast_runs (
  id INTEGER PRIMARY KEY,
  uid TEXT NOT NULL UNIQUE,
  as_of_date TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('running','completed','failed')),
  created_by INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
  created_at TEXT NOT NULL,
  completed_at TEXT
);

CREATE TABLE revenue_forecast_lines (
  id INTEGER PRIMARY KEY,
  uid TEXT NOT NULL UNIQUE,
  forecast_run_id INTEGER NOT NULL REFERENCES revenue_forecast_runs(id) ON DELETE RESTRICT,
  commercial_case_id INTEGER NOT NULL REFERENCES revenue_commercial_cases(id) ON DELETE RESTRICT,
  enrolment_component_id INTEGER NOT NULL REFERENCES revenue_enrolment_components(id) ON DELETE RESTRICT,
  tuition_authority_level TEXT NOT NULL CHECK(tuition_authority_level IN ('official','manual','estimated')),
  tuition_cents INTEGER NOT NULL CHECK(tuition_cents>=0),
  commission_cents INTEGER NOT NULL CHECK(commission_cents>=0),
  prior_commission_cents INTEGER,
  variance_cents INTEGER NOT NULL,
  expected_date TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE revenue_invoices (
  id INTEGER PRIMARY KEY,
  uid TEXT NOT NULL UNIQUE,
  document_kind TEXT NOT NULL CHECK(document_kind IN ('client_invoice','direct_commission_invoice','master_agent_invoice','credit_note','debit_note')),
  invoice_number TEXT NOT NULL UNIQUE,
  commercial_case_id INTEGER REFERENCES revenue_commercial_cases(id) ON DELETE RESTRICT,
  billing_account_id INTEGER REFERENCES revenue_billing_accounts(id) ON DELETE RESTRICT,
  revenue_entity_id INTEGER REFERENCES revenue_entities(id) ON DELETE RESTRICT,
  issue_date TEXT NOT NULL,
  due_date TEXT,
  currency_code TEXT NOT NULL DEFAULT 'AUD' CHECK(length(currency_code)=3),
  net_cents INTEGER NOT NULL CHECK(net_cents>=0),
  gst_cents INTEGER NOT NULL CHECK(gst_cents>=0),
  gross_cents INTEGER NOT NULL CHECK(gross_cents=net_cents+gst_cents),
  status TEXT NOT NULL CHECK(status IN ('draft','issued','cancelled')),
  correction_of_invoice_id INTEGER REFERENCES revenue_invoices(id) ON DELETE RESTRICT,
  document_id INTEGER REFERENCES revenue_documents(id) ON DELETE RESTRICT,
  issued_by INTEGER REFERENCES users(id) ON DELETE RESTRICT,
  issued_at TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE revenue_invoice_lines (
  id INTEGER PRIMARY KEY,
  uid TEXT NOT NULL UNIQUE,
  invoice_id INTEGER NOT NULL REFERENCES revenue_invoices(id) ON DELETE RESTRICT,
  source_kind TEXT NOT NULL,
  source_uid TEXT NOT NULL,
  student_id TEXT REFERENCES students(student_id) ON DELETE RESTRICT,
  description TEXT NOT NULL,
  quantity INTEGER NOT NULL DEFAULT 1 CHECK(quantity>0),
  net_cents INTEGER NOT NULL CHECK(net_cents>=0),
  gst_cents INTEGER NOT NULL CHECK(gst_cents>=0),
  gross_cents INTEGER NOT NULL CHECK(gross_cents=net_cents+gst_cents),
  sort_order INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE revenue_planned_payment_allocations (
  id INTEGER PRIMARY KEY,
  uid TEXT NOT NULL UNIQUE,
  invoice_id INTEGER NOT NULL REFERENCES revenue_invoices(id) ON DELETE RESTRICT,
  payment_method_id INTEGER NOT NULL REFERENCES revenue_payment_methods(id) ON DELETE RESTRICT,
  planned_net_cents INTEGER NOT NULL CHECK(planned_net_cents>=0),
  planned_gst_cents INTEGER NOT NULL CHECK(planned_gst_cents>=0),
  planned_gross_cents INTEGER NOT NULL CHECK(planned_gross_cents=planned_net_cents+planned_gst_cents),
  created_at TEXT NOT NULL
);

CREATE TABLE revenue_payments (
  id INTEGER PRIMARY KEY,
  uid TEXT NOT NULL UNIQUE,
  payment_direction TEXT NOT NULL CHECK(payment_direction IN ('received','paid')),
  counterparty_kind TEXT NOT NULL CHECK(counterparty_kind IN ('client','institution','master_agent')),
  billing_account_id INTEGER REFERENCES revenue_billing_accounts(id) ON DELETE RESTRICT,
  revenue_entity_id INTEGER REFERENCES revenue_entities(id) ON DELETE RESTRICT,
  payment_method_id INTEGER REFERENCES revenue_payment_methods(id) ON DELETE RESTRICT,
  external_reference TEXT,
  received_date TEXT NOT NULL,
  amount_cents INTEGER NOT NULL CHECK(amount_cents>0),
  allocated_cents INTEGER NOT NULL DEFAULT 0 CHECK(allocated_cents>=0 AND allocated_cents<=amount_cents),
  currency_code TEXT NOT NULL DEFAULT 'AUD' CHECK(length(currency_code)=3),
  status TEXT NOT NULL CHECK(status IN ('confirmed','reversed')),
  evidence_document_id INTEGER REFERENCES revenue_documents(id) ON DELETE RESTRICT,
  confirmed_by INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
  confirmed_at TEXT NOT NULL
);

CREATE TABLE revenue_payment_allocations (
  id INTEGER PRIMARY KEY,
  uid TEXT NOT NULL UNIQUE,
  payment_id INTEGER NOT NULL REFERENCES revenue_payments(id) ON DELETE RESTRICT,
  invoice_line_id INTEGER REFERENCES revenue_invoice_lines(id) ON DELETE RESTRICT,
  commission_line_id INTEGER REFERENCES revenue_commission_lines(id) ON DELETE RESTRICT,
  statement_item_uid TEXT,
  allocated_cents INTEGER NOT NULL CHECK(allocated_cents>0),
  confirmed_by INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
  confirmed_at TEXT NOT NULL,
  CHECK((invoice_line_id IS NOT NULL)+(commission_line_id IS NOT NULL)+(statement_item_uid IS NOT NULL)=1)
);

CREATE TABLE revenue_receipts (
  id INTEGER PRIMARY KEY,
  uid TEXT NOT NULL UNIQUE,
  receipt_number TEXT NOT NULL UNIQUE,
  payment_id INTEGER NOT NULL REFERENCES revenue_payments(id) ON DELETE RESTRICT,
  amount_cents INTEGER NOT NULL CHECK(amount_cents>0),
  currency_code TEXT NOT NULL DEFAULT 'AUD' CHECK(length(currency_code)=3),
  document_id INTEGER NOT NULL UNIQUE REFERENCES revenue_documents(id) ON DELETE RESTRICT,
  issued_by INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
  issued_at TEXT NOT NULL
);

CREATE TABLE revenue_ledger_entries (
  id INTEGER PRIMARY KEY,
  uid TEXT NOT NULL UNIQUE,
  account_code TEXT NOT NULL,
  event_type TEXT NOT NULL CHECK(event_type IN ('expected','invoiced','received','paid','refund','adjustment','clawback','reversal')),
  source_kind TEXT NOT NULL,
  source_uid TEXT NOT NULL,
  commercial_case_id INTEGER REFERENCES revenue_commercial_cases(id) ON DELETE RESTRICT,
  debit_cents INTEGER NOT NULL DEFAULT 0 CHECK(debit_cents>=0),
  credit_cents INTEGER NOT NULL DEFAULT 0 CHECK(credit_cents>=0),
  currency_code TEXT NOT NULL DEFAULT 'AUD' CHECK(length(currency_code)=3),
  occurred_at TEXT NOT NULL,
  created_by INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
  CHECK((debit_cents>0)+(credit_cents>0)=1)
);

CREATE TABLE revenue_claims (
  id INTEGER PRIMARY KEY,
  uid TEXT NOT NULL UNIQUE,
  claim_number TEXT NOT NULL UNIQUE,
  claim_kind TEXT NOT NULL CHECK(claim_kind IN ('direct','master_agent')),
  revenue_entity_id INTEGER NOT NULL REFERENCES revenue_entities(id) ON DELETE RESTRICT,
  claim_date TEXT NOT NULL,
  net_cents INTEGER NOT NULL CHECK(net_cents>=0),
  gst_cents INTEGER NOT NULL CHECK(gst_cents>=0),
  gross_cents INTEGER NOT NULL CHECK(gross_cents=net_cents+gst_cents),
  status TEXT NOT NULL CHECK(status IN ('draft','issued','cancelled')),
  document_id INTEGER REFERENCES revenue_documents(id) ON DELETE RESTRICT,
  issued_by INTEGER REFERENCES users(id) ON DELETE RESTRICT,
  issued_at TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE revenue_claim_allocations (
  id INTEGER PRIMARY KEY,
  uid TEXT NOT NULL UNIQUE,
  claim_id INTEGER NOT NULL REFERENCES revenue_claims(id) ON DELETE RESTRICT,
  commission_instalment_id INTEGER NOT NULL REFERENCES revenue_commission_instalments(id) ON DELETE RESTRICT,
  claimed_cents INTEGER NOT NULL CHECK(claimed_cents>0),
  created_at TEXT NOT NULL,
  UNIQUE(claim_id,commission_instalment_id)
);

CREATE TABLE revenue_claim_communications (
  id INTEGER PRIMARY KEY,
  uid TEXT NOT NULL UNIQUE,
  claim_id INTEGER NOT NULL REFERENCES revenue_claims(id) ON DELETE RESTRICT,
  communication_kind TEXT NOT NULL CHECK(communication_kind IN ('export','email','manual_send')),
  recipient TEXT,
  document_id INTEGER REFERENCES revenue_documents(id) ON DELETE RESTRICT,
  status TEXT NOT NULL CHECK(status IN ('recorded','sent','failed')),
  error_code TEXT,
  actor_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
  occurred_at TEXT NOT NULL
);

CREATE TABLE revenue_reconciliation_runs (
  id INTEGER PRIMARY KEY,
  uid TEXT NOT NULL UNIQUE,
  reconciliation_kind TEXT NOT NULL CHECK(reconciliation_kind IN ('payment','master_agent_statement')),
  source_document_id INTEGER REFERENCES revenue_documents(id) ON DELETE RESTRICT,
  status TEXT NOT NULL CHECK(status IN ('pending','suggested','confirmed','failed')),
  created_by INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
  created_at TEXT NOT NULL,
  confirmed_by INTEGER REFERENCES users(id) ON DELETE RESTRICT,
  confirmed_at TEXT
);

CREATE TABLE revenue_reconciliation_suggestions (
  id INTEGER PRIMARY KEY,
  uid TEXT NOT NULL UNIQUE,
  reconciliation_run_id INTEGER NOT NULL REFERENCES revenue_reconciliation_runs(id) ON DELETE RESTRICT,
  source_item_uid TEXT NOT NULL,
  suggested_target_kind TEXT NOT NULL,
  suggested_target_uid TEXT NOT NULL,
  suggested_amount_cents INTEGER NOT NULL CHECK(suggested_amount_cents>0),
  confidence_basis_points INTEGER NOT NULL CHECK(confidence_basis_points BETWEEN 0 AND 10000),
  evidence_json TEXT NOT NULL,
  review_status TEXT NOT NULL CHECK(review_status IN ('pending','confirmed','rejected')),
  reviewed_by INTEGER REFERENCES users(id) ON DELETE RESTRICT,
  reviewed_at TEXT
);

CREATE TABLE revenue_master_agent_statements (
  id INTEGER PRIMARY KEY,
  uid TEXT NOT NULL UNIQUE,
  master_agent_entity_id INTEGER NOT NULL REFERENCES revenue_entities(id) ON DELETE RESTRICT,
  statement_reference TEXT NOT NULL,
  period_start TEXT,
  period_end TEXT,
  currency_code TEXT NOT NULL DEFAULT 'AUD' CHECK(length(currency_code)=3),
  total_cents INTEGER NOT NULL CHECK(total_cents>=0),
  document_id INTEGER NOT NULL REFERENCES revenue_documents(id) ON DELETE RESTRICT,
  import_hash TEXT NOT NULL UNIQUE,
  status TEXT NOT NULL CHECK(status IN ('imported','reviewed','reconciled','superseded')),
  imported_by INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
  imported_at TEXT NOT NULL,
  UNIQUE(master_agent_entity_id,statement_reference)
);

CREATE TABLE revenue_master_agent_statement_items (
  id INTEGER PRIMARY KEY,
  uid TEXT NOT NULL UNIQUE,
  statement_id INTEGER NOT NULL REFERENCES revenue_master_agent_statements(id) ON DELETE RESTRICT,
  external_reference TEXT,
  student_reference TEXT,
  student_name_text TEXT,
  program_text TEXT,
  gross_commission_cents INTEGER NOT NULL CHECK(gross_commission_cents>=0),
  share_cents INTEGER NOT NULL CHECK(share_cents>=0),
  payable_cents INTEGER NOT NULL CHECK(payable_cents>=0),
  line_number INTEGER NOT NULL CHECK(line_number>=1),
  raw_evidence_json TEXT,
  UNIQUE(statement_id,line_number)
);

CREATE TABLE revenue_statement_matches (
  id INTEGER PRIMARY KEY,
  uid TEXT NOT NULL UNIQUE,
  statement_item_id INTEGER NOT NULL REFERENCES revenue_master_agent_statement_items(id) ON DELETE RESTRICT,
  commission_line_id INTEGER REFERENCES revenue_commission_lines(id) ON DELETE RESTRICT,
  match_status TEXT NOT NULL CHECK(match_status IN ('confirmed','excluded','disputed','unmatched')),
  matched_cents INTEGER NOT NULL DEFAULT 0 CHECK(matched_cents>=0),
  evidence_json TEXT,
  confirmed_by INTEGER REFERENCES users(id) ON DELETE RESTRICT,
  confirmed_at TEXT,
  UNIQUE(statement_item_id,commission_line_id)
);

CREATE TABLE revenue_disputes (
  id INTEGER PRIMARY KEY,
  uid TEXT NOT NULL UNIQUE,
  statement_item_id INTEGER REFERENCES revenue_master_agent_statement_items(id) ON DELETE RESTRICT,
  commission_line_id INTEGER REFERENCES revenue_commission_lines(id) ON DELETE RESTRICT,
  dispute_code TEXT NOT NULL,
  description TEXT NOT NULL,
  disputed_cents INTEGER NOT NULL CHECK(disputed_cents>0),
  status TEXT NOT NULL CHECK(status IN ('open','resolved','withdrawn')),
  opened_by INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
  opened_at TEXT NOT NULL,
  resolved_by INTEGER REFERENCES users(id) ON DELETE RESTRICT,
  resolved_at TEXT,
  resolution TEXT
);

CREATE INDEX idx_revenue_cases_student ON revenue_commercial_cases(student_id,status);
CREATE INDEX idx_revenue_components_case ON revenue_enrolment_components(commercial_case_id,status);
CREATE INDEX idx_revenue_tuition_component ON revenue_tuition_facts(enrolment_component_id,academic_period,status);
CREATE INDEX idx_revenue_commission_case ON revenue_commission_lines(commercial_case_id,status);
CREATE INDEX idx_revenue_invoice_account ON revenue_invoices(billing_account_id,status,issue_date);
CREATE INDEX idx_revenue_payment_reference ON revenue_payments(external_reference,received_date);
CREATE INDEX idx_revenue_ledger_case ON revenue_ledger_entries(commercial_case_id,occurred_at,id);
CREATE INDEX idx_revenue_statement_entity ON revenue_master_agent_statements(master_agent_entity_id,period_start,period_end);

CREATE TRIGGER revenue_audit_events_no_update
BEFORE UPDATE ON revenue_audit_events BEGIN
  SELECT RAISE(ABORT,'revenue audit append-only');
END;
CREATE TRIGGER revenue_audit_events_no_delete
BEFORE DELETE ON revenue_audit_events BEGIN
  SELECT RAISE(ABORT,'revenue audit append-only');
END;
CREATE TRIGGER revenue_rule_snapshots_no_update
BEFORE UPDATE ON revenue_rule_snapshots BEGIN
  SELECT RAISE(ABORT,'revenue rule snapshot append-only');
END;
CREATE TRIGGER revenue_rule_snapshots_no_delete
BEFORE DELETE ON revenue_rule_snapshots BEGIN
  SELECT RAISE(ABORT,'revenue rule snapshot append-only');
END;
CREATE TRIGGER revenue_ledger_entries_no_update
BEFORE UPDATE ON revenue_ledger_entries BEGIN
  SELECT RAISE(ABORT,'revenue ledger append-only');
END;
CREATE TRIGGER revenue_ledger_entries_no_delete
BEFORE DELETE ON revenue_ledger_entries BEGIN
  SELECT RAISE(ABORT,'revenue ledger append-only');
END;
CREATE TRIGGER revenue_receipts_no_update
BEFORE UPDATE ON revenue_receipts BEGIN
  SELECT RAISE(ABORT,'revenue receipt append-only');
END;
CREATE TRIGGER revenue_receipts_no_delete
BEFORE DELETE ON revenue_receipts BEGIN
  SELECT RAISE(ABORT,'revenue receipt append-only');
END;
CREATE TRIGGER revenue_invoices_issued_no_update
BEFORE UPDATE ON revenue_invoices WHEN OLD.status='issued' BEGIN
  SELECT RAISE(ABORT,'issued revenue invoice immutable');
END;
CREATE TRIGGER revenue_invoices_issued_no_delete
BEFORE DELETE ON revenue_invoices WHEN OLD.status='issued' BEGIN
  SELECT RAISE(ABORT,'issued revenue invoice immutable');
END;
CREATE TRIGGER revenue_claims_issued_no_update
BEFORE UPDATE ON revenue_claims WHEN OLD.status='issued' BEGIN
  SELECT RAISE(ABORT,'issued revenue claim immutable');
END;
CREATE TRIGGER revenue_claims_issued_no_delete
BEFORE DELETE ON revenue_claims WHEN OLD.status='issued' BEGIN
  SELECT RAISE(ABORT,'issued revenue claim immutable');
END;
CREATE TRIGGER revenue_contract_active_no_update
BEFORE UPDATE ON revenue_contract_versions WHEN OLD.status='active' BEGIN
  SELECT RAISE(ABORT,'active revenue contract immutable');
END;
CREATE TRIGGER revenue_commission_rule_active_no_update
BEFORE UPDATE ON revenue_commission_rule_versions WHEN OLD.status='active' BEGIN
  SELECT RAISE(ABORT,'active revenue rule immutable');
END;
