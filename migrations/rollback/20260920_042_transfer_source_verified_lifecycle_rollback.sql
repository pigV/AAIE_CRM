-- SQL downgrade is intentionally unavailable because verified authority is an audit fact.
-- Recovery authority is the independently verified pre-migration SQLite backup.
CREATE TEMP TABLE transfer_source_verified_restore_only(value INTEGER CHECK(value=0));
INSERT INTO transfer_source_verified_restore_only VALUES(1);
