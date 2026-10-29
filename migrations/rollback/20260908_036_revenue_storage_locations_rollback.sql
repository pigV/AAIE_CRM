-- Only empty-table schema rollback. Restored locations still have immutable history.
-- Execute standalone; caller rolls back on any error. Do not wrap in another transaction.
BEGIN IMMEDIATE;
CREATE TEMP TABLE revenue_036_rollback_guard(value INTEGER);
CREATE TEMP TRIGGER revenue_036_rollback_guard_nonempty
BEFORE INSERT ON revenue_036_rollback_guard
WHEN EXISTS(SELECT 1 FROM revenue_document_locations)
BEGIN SELECT RAISE(ABORT,'Migration 036 rollback blocked: location audit history exists'); END;
INSERT INTO revenue_036_rollback_guard(value) VALUES(1);
DROP TABLE revenue_036_rollback_guard;
DROP TABLE revenue_document_locations;
COMMIT;
