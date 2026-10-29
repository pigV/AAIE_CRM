"""Static59-column shape freshly read from Production; synthetic fixture construction only."""
PRODUCTION_VISA_DDL = "CREATE TABLE visa_cases (\n  id INTEGER PRIMARY KEY, student_id TEXT NOT NULL REFERENCES students(student_id),\n  application_id TEXT, visa_case_name TEXT NOT NULL, visa_type TEXT, applicant_name TEXT,\n  relationship_to_student TEXT, application_type TEXT, visa_status TEXT NOT NULL DEFAULT '准备中',\n  submitted_date TEXT, grant_date TEXT, refusal_date TEXT, visa_start_date TEXT, visa_expiry_date TEXT,\n  trn TEXT, hap_id TEXT, grant_number TEXT, visa_conditions TEXT,\n  coe_expiry_date TEXT, oshc_expiry_date TEXT, caaw_expiry_date TEXT, s56_deadline TEXT,\n  responsible_consultant_id INTEGER REFERENCES users(id), responsible_consultant_name TEXT,\n  school_name TEXT, state TEXT, internal_notes TEXT, risk_level TEXT,\n  final_result TEXT NOT NULL DEFAULT 'Pending', refusal_reason_category TEXT,\n  is_included_in_analytics INTEGER NOT NULL DEFAULT 1, file_completion INTEGER NOT NULL DEFAULT 0,\n  legacy_visa_id INTEGER REFERENCES visas(id), created_by INTEGER REFERENCES users(id),\n  created_at TEXT NOT NULL, updated_by INTEGER REFERENCES users(id), updated_at TEXT NOT NULL\n, case_type TEXT DEFAULT 'Student Visa', status TEXT DEFAULT 'Draft', visa_subclass TEXT, lodgement_date TEXT, expiry_date TEXT, s56_due_date TEXT, decision_status TEXT DEFAULT 'Pending', notes TEXT, archived_at TEXT, responsible_consultant TEXT, main_applicant TEXT, guardian_applicant TEXT, applicant_count INTEGER DEFAULT 1, case_owner TEXT, target_lodgement_date TEXT, actual_lodgement_date TEXT, result_date TEXT, result_status TEXT DEFAULT 'Pending', lodged_by_user_id INTEGER REFERENCES users(id), lodged_by_name TEXT, lodgement_recorded_at TEXT, application_cycle_id INTEGER REFERENCES application_cycles(id) ON DELETE RESTRICT)"
PRODUCTION_VISA_INDEXES = ['CREATE INDEX idx_visa_cases_application ON visa_cases(application_id)', 'CREATE INDEX idx_visa_cases_cycle\nON visa_cases(application_cycle_id)', 'CREATE INDEX idx_visa_cases_expiry ON visa_cases(visa_expiry_date, coe_expiry_date, oshc_expiry_date, caaw_expiry_date, s56_deadline)', 'CREATE INDEX idx_visa_cases_legacy ON visa_cases(legacy_visa_id)', 'CREATE INDEX idx_visa_cases_status ON visa_cases(visa_status, final_result, risk_level)', 'CREATE INDEX idx_visa_cases_student ON visa_cases(student_id, application_id, updated_at DESC)']

def prepare_production_visa_schema(conn):
    """Replace only an empty synthetic fixture table; never migrate real rows."""
    if conn.in_transaction:
        raise ValueError("Fixture must not have an active transaction")
    if conn.execute("SELECT COUNT(*) FROM visa_cases").fetchone()[0]:
        raise ValueError("Fixture construction requires empty visa_cases")
    conn.execute("PRAGMA foreign_keys=OFF")
    triggers=conn.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger' AND name='visa_decision_events_ownership_insert'").fetchall()
    for name,_ in triggers:conn.execute('DROP TRIGGER '+name)
    conn.execute("DROP TABLE visa_cases")
    conn.execute(PRODUCTION_VISA_DDL)
    for sql in PRODUCTION_VISA_INDEXES:conn.execute(sql)
    for _,sql in triggers:conn.execute(sql)
