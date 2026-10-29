-- Intentionally fail closed: result evidence and its effective projection must not be dropped.
-- Restore only into a separate stopped isolated target from a verified pre-migration online backup.
-- Production rollback requires its separate data-preserving operational gate.
SELECT * FROM visa_result_status_restore_from_verified_backup_required;
