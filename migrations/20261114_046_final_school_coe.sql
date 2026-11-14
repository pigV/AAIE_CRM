CREATE TABLE final_school_coe_confirmations (
    final_acceptance_id INTEGER NOT NULL REFERENCES cycle_final_acceptances(id),
    coe_id INTEGER NOT NULL REFERENCES coe_oshc(id),
    evidence_snapshot TEXT NOT NULL,
    confirmed_by INTEGER NOT NULL REFERENCES users(id),
    confirmed_at TEXT NOT NULL,
    PRIMARY KEY (final_acceptance_id, coe_id)
);
