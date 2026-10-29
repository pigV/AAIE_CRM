SELECT count(*) = 1
FROM sqlite_master
WHERE type='table' AND name='revenue_claim_eligibility_evaluations';

SELECT count(*) = 1
FROM sqlite_master
WHERE type='table' AND name='revenue_claim_evidence_links';

SELECT count(*) = 4
FROM sqlite_master
WHERE type='trigger' AND name IN (
  'revenue_claim_eligibility_evaluations_no_update',
  'revenue_claim_eligibility_evaluations_no_delete',
  'revenue_claim_evidence_links_no_update',
  'revenue_claim_evidence_links_no_delete'
);

SELECT count(*) = 0 FROM revenue_claim_eligibility_evaluations;
SELECT count(*) = 0 FROM revenue_claim_evidence_links;
SELECT count(*) = 0 FROM pragma_foreign_key_check;
SELECT quick_check = 'ok' FROM pragma_quick_check;
