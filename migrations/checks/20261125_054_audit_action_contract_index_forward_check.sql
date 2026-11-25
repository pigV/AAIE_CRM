SELECT
 (SELECT COUNT(*)=2 FROM sqlite_master WHERE type='table' AND name IN
  ('revenue_historical_receipts','revenue_historical_receipt_evidence'))
 AND (SELECT COUNT(*)=4 FROM sqlite_master WHERE type='trigger' AND name IN
  ('historical_receipt_confirmed_no_update','historical_receipt_no_delete',
   'historical_receipt_evidence_no_update','historical_receipt_evidence_no_delete'))
 AND
 (SELECT count(*) FROM pragma_index_list('audit_logs')
  WHERE name='idx_audit_action_contract' AND "unique"=0 AND partial=0)=1
 AND (SELECT count(*) FROM pragma_index_info('idx_audit_action_contract'))=2
 AND (SELECT count(*) FROM pragma_index_info('idx_audit_action_contract')
      WHERE seqno=0 AND name='action_type')=1
 AND (SELECT count(*) FROM pragma_index_info('idx_audit_action_contract')
      WHERE seqno=1 AND name='contract_id')=1;
