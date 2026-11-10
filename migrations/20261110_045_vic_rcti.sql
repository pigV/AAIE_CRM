CREATE TABLE revenue_vic_rcti (
    uid TEXT PRIMARY KEY NOT NULL,
    contract_uid TEXT NOT NULL REFERENCES revenue_rule_snapshots(uid) ON DELETE RESTRICT,
    simulation INTEGER NOT NULL CHECK(simulation IN (0,1)),
    pdf_id INTEGER NOT NULL UNIQUE REFERENCES revenue_documents(id) ON DELETE RESTRICT,
    pdf_sha TEXT NOT NULL,
    csv_id INTEGER UNIQUE REFERENCES revenue_documents(id) ON DELETE RESTRICT,
    issuer_key TEXT,
    number_key TEXT,
    review_json TEXT NOT NULL CHECK(json_valid(review_json)),
    confirmed_json TEXT CHECK(confirmed_json IS NULL OR json_valid(confirmed_json)),
    details_confirmed INTEGER NOT NULL DEFAULT 0 CHECK(details_confirmed IN (0,1)),
    created_at TEXT NOT NULL,
    UNIQUE(simulation,pdf_sha),
    UNIQUE(simulation,issuer_key,number_key)
);
CREATE TABLE revenue_vic_rcti_student (
    rcti_uid TEXT NOT NULL REFERENCES revenue_vic_rcti(uid) ON DELETE RESTRICT,
    simulation INTEGER NOT NULL CHECK(simulation IN (0,1)),
    component_uid TEXT NOT NULL,
    period TEXT NOT NULL,
    PRIMARY KEY(simulation,component_uid,period)
);
CREATE TABLE revenue_vic_rcti_bank (
    uid TEXT PRIMARY KEY NOT NULL,
    rcti_uid TEXT NOT NULL REFERENCES revenue_vic_rcti(uid) ON DELETE RESTRICT,
    simulation INTEGER NOT NULL CHECK(simulation IN (0,1)),
    account TEXT NOT NULL,
    transaction_ref TEXT NOT NULL,
    received_on TEXT NOT NULL,
    cents INTEGER NOT NULL CHECK(cents>0),
    actor INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    confirmed_at TEXT NOT NULL,
    UNIQUE(simulation,account,transaction_ref)
);
CREATE INDEX idx_vic_rcti_bank_statement ON revenue_vic_rcti_bank(rcti_uid);
