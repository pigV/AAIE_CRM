SELECT CASE WHEN
  EXISTS(SELECT 1 FROM pragma_table_info('parent_form_tokens') WHERE name='intake_type')
  AND EXISTS(SELECT 1 FROM sqlite_master WHERE type='table' AND name='parent_form_intake_type_change_events')
  AND EXISTS(SELECT 1 FROM pragma_table_info('transfer_school_applications') WHERE name='removed_at')
  AND EXISTS(SELECT 1 FROM pragma_table_info('transfer_school_applications') WHERE name='removed_by')
  AND EXISTS(SELECT 1 FROM sqlite_master WHERE type='table' AND name='transfer_payment_confirmations')
  AND EXISTS(SELECT 1 FROM sqlite_master WHERE type='trigger' AND name='parent_form_intake_change_no_update_035')
  AND EXISTS(SELECT 1 FROM sqlite_master WHERE type='trigger' AND name='parent_form_intake_change_no_delete_035')
  AND EXISTS(SELECT 1 FROM sqlite_master WHERE type='trigger' AND name='transfer_payment_confirmations_no_update_035')
  AND EXISTS(SELECT 1 FROM sqlite_master WHERE type='trigger' AND name='transfer_payment_confirmations_no_delete_035')
THEN 1 ELSE 0 END;
