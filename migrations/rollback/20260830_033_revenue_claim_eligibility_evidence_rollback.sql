-- Disposable pre-Production rehearsal only. Production rollback requires separate authority.
DROP TRIGGER IF EXISTS revenue_claim_evidence_links_no_delete;
DROP TRIGGER IF EXISTS revenue_claim_evidence_links_no_update;
DROP TRIGGER IF EXISTS revenue_claim_eligibility_evaluations_no_delete;
DROP TRIGGER IF EXISTS revenue_claim_eligibility_evaluations_no_update;
DROP TABLE IF EXISTS revenue_claim_evidence_links;
DROP TABLE IF EXISTS revenue_claim_eligibility_evaluations;
