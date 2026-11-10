CREATE TABLE revenue_authority_payers (
    state_code TEXT PRIMARY KEY NOT NULL CHECK(state_code IN ('ACT','NSW','NT','QLD','SA','TAS','VIC','WA')),
    revenue_entity_id INTEGER NOT NULL UNIQUE REFERENCES revenue_entities(id) ON DELETE RESTRICT
);

CREATE TABLE revenue_authority_invoices (
    draft_uid TEXT PRIMARY KEY NOT NULL,
    invoice_id INTEGER NOT NULL UNIQUE REFERENCES revenue_invoices(id) ON DELETE RESTRICT,
    state_code TEXT NOT NULL REFERENCES revenue_authority_payers(state_code) ON DELETE RESTRICT,
    contract_draft_uid TEXT NOT NULL REFERENCES revenue_rule_snapshots(uid) ON DELETE RESTRICT,
    terms_revision TEXT NOT NULL,
    payment_days INTEGER NOT NULL CHECK(payment_days BETWEEN 0 AND 365)
);

CREATE UNIQUE INDEX uq_revenue_authority_invoice_line
    ON revenue_invoice_lines(source_uid) WHERE source_kind='authority_draft_line';
