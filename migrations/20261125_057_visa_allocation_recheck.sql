-- Close stale-link gap without changing applied migration056.
DROP TRIGGER revenue_client_payment_allocations_validate;
CREATE TRIGGER revenue_client_payment_allocations_validate BEFORE INSERT ON revenue_client_payment_allocations
WHEN NOT EXISTS (
 SELECT 1 FROM revenue_payments p
 JOIN revenue_payment_methods m ON m.id=p.payment_method_id
 JOIN revenue_payment_method_rule_versions r ON r.id=NEW.payment_method_rule_version_id AND r.payment_method_id=m.id
 JOIN revenue_fee_items fee ON fee.id=NEW.fee_item_id AND fee.billing_account_id=p.billing_account_id
 JOIN revenue_commercial_cases cc ON cc.id=fee.commercial_case_id AND cc.student_id=fee.student_id
 WHERE p.id=NEW.payment_id AND p.payment_direction='received' AND p.counterparty_kind='client'
 AND p.status='confirmed' AND fee.status<>'cancelled'
 AND m.method_code=NEW.method_code_snapshot AND r.gst_rate_basis_points=NEW.gst_rate_basis_points
 AND (EXISTS(SELECT 1 FROM revenue_fee_item_contract_links link JOIN contracts k ON k.id=link.contract_id
             WHERE link.fee_item_id=fee.id AND link.commercial_case_id=cc.id AND k.student_id=cc.student_id
             AND k.application_cycle_id=cc.application_cycle_id AND k.contract_status='FullyExecuted')
 OR EXISTS(SELECT 1 FROM revenue_fee_item_visa_links link WHERE link.fee_item_id=fee.id
 AND EXISTS (
 SELECT 1 FROM revenue_fee_items f
 JOIN revenue_commercial_cases c ON c.id=link.commercial_case_id AND c.id=f.commercial_case_id
 JOIN visa_cases v ON v.id=link.visa_case_id AND v.student_id=c.student_id AND v.application_cycle_id=c.application_cycle_id
 JOIN revenue_rule_snapshots s ON s.id=f.rule_snapshot_id
 JOIN json_each(s.canonical_json,'$.items') i
 WHERE f.id=link.fee_item_id AND f.student_id=c.student_id AND f.status<>'cancelled'
 AND json_array_length(s.canonical_json,'$.items')=1
 AND json_extract(i.value,'$.contract_id') IS NULL
 AND json_extract(i.value,'$.visa_case_id')=v.id
 AND json_extract(i.value,'$.application_cycle_id')=c.application_cycle_id
 AND json_extract(i.value,'$.milestone_code')=f.milestone_code
 AND json_extract(i.value,'$.net_cents')=f.net_cents
 AND coalesce(json_extract(i.value,'$.gst_cents'),0)=f.gst_cents
 AND f.milestone_code IN ('standalone-student-visa','student-visa-renewal','guardian-590','guardian-590-renewal','visitor-600')
 AND v.archived_at IS NULL
 )))
)
BEGIN SELECT RAISE(ABORT,'Revenue client allocation authority mismatch'); END;
