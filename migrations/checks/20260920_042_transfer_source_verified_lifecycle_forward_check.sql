SELECT count(*)=1 FROM schema_migrations
 WHERE version='20260920_041' AND name='transfer_source_authority'
   AND checksum_sha256='9294a3d6db269649723b3ebb391d0684a34c63c44a849bdea00166fca4c54e88';
SELECT count(*)=17 FROM pragma_table_info('transfer_source_authorities');
SELECT instr((SELECT sql FROM sqlite_master WHERE type='table' AND name='transfer_source_authorities'),'''verified''')>0;
SELECT count(*)=0 FROM transfer_source_authorities
 WHERE lifecycle_state NOT IN ('pending','verified','consumed');
SELECT count(*)=0 FROM transfer_source_authorities
 WHERE lifecycle_state='pending' AND (source_classification IS NOT NULL OR application_cycle_id IS NOT NULL
   OR verified_by IS NOT NULL OR verified_at IS NOT NULL);
SELECT count(*)=0 FROM transfer_source_authorities
 WHERE lifecycle_state='verified' AND (source_classification IS NULL OR application_cycle_id IS NOT NULL
   OR verified_by IS NULL OR verified_at IS NULL);
SELECT count(*)=0 FROM transfer_source_authorities
 WHERE lifecycle_state='consumed' AND (source_classification IS NULL OR application_cycle_id IS NULL
   OR verified_by IS NULL OR verified_at IS NULL);
SELECT count(*)=1 FROM sqlite_master WHERE type='trigger' AND name='transfer_source_authority_verify_041';
SELECT count(*)=1 FROM sqlite_master WHERE type='trigger' AND name='transfer_source_authority_consume_041';
SELECT count(*)=1 FROM sqlite_master WHERE type='index' AND name='idx_transfer_source_authority_student';
SELECT count(*)=1 FROM sqlite_master WHERE type='index' AND name='idx_transfer_source_authority_intake';
SELECT count(*)=0 FROM pragma_foreign_key_check;
