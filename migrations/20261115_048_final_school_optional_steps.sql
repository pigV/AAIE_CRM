CREATE VIEW final_school_optional_steps AS
SELECT acceptance.id AS acceptance_id, audit.old_value AS step
  FROM cycle_final_acceptances acceptance
  JOIN audit_logs audit ON audit.record_id=CAST(acceptance.id AS TEXT)
       AND audit.student_id=acceptance.student_id
 WHERE audit.module_name='offers'
   AND audit.action_type='final_school_step_requirement'
   AND audit.status='success'
   AND audit.old_value IN ('signed','payment','return')
   AND audit.id=(
       SELECT max(latest.id) FROM audit_logs latest
        WHERE latest.module_name='offers'
          AND latest.action_type='final_school_step_requirement'
          AND latest.status='success'
          AND latest.record_id=audit.record_id AND latest.student_id=audit.student_id
          AND latest.old_value=audit.old_value
   )
   AND CASE WHEN json_valid(audit.new_value)
            THEN json_extract(audit.new_value,'$.decision')='optional'
                 AND length(trim(coalesce(json_extract(audit.new_value,'$.reason'),'')))>0
            ELSE 0 END;

DROP TRIGGER final_school_submission_ownership_insert;

CREATE TRIGGER final_school_submission_ownership_insert
BEFORE INSERT ON final_school_submission_events
WHEN NOT EXISTS (
    SELECT 1 FROM cycle_final_acceptances acceptance
     WHERE acceptance.id=NEW.final_acceptance_id
       AND acceptance.application_cycle_id=NEW.application_cycle_id
       AND acceptance.acceptance_status='active'
       AND (acceptance.signed_offer_completed_at IS NOT NULL OR EXISTS (
           SELECT 1 FROM final_school_optional_steps
            WHERE acceptance_id=acceptance.id AND step='signed'
       ))
       AND (acceptance.payment_completed_at IS NOT NULL OR EXISTS (
           SELECT 1 FROM final_school_optional_steps
            WHERE acceptance_id=acceptance.id AND step='payment'
       ))
)
BEGIN
    SELECT RAISE(ABORT, 'final school submission ownership or evidence mismatch');
END;
