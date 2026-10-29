-- Additive document-location evidence. Original document rows/triggers remain intact.
-- Ordinary runtime connections deliberately do not register the authorization UDF.
CREATE TABLE revenue_document_locations (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 operation_id TEXT NOT NULL UNIQUE CHECK(length(trim(operation_id))>0),
 document_id INTEGER NOT NULL REFERENCES revenue_documents(id) ON DELETE RESTRICT,
 document_uid TEXT NOT NULL,
 sequence INTEGER NOT NULL CHECK(typeof(sequence)='integer' AND sequence>=1),
 previous_event_id INTEGER REFERENCES revenue_document_locations(id) ON DELETE RESTRICT,
 old_path TEXT NOT NULL CHECK(substr(old_path,1,1)='/'),
 new_path TEXT NOT NULL CHECK(substr(new_path,1,1)='/' AND new_path!=old_path),
 sha256 TEXT NOT NULL CHECK(length(sha256)=64),
 size_bytes INTEGER NOT NULL CHECK(typeof(size_bytes)='integer' AND size_bytes>0),
 document_fingerprint TEXT NOT NULL CHECK(length(document_fingerprint)=64),
 occurred_at TEXT NOT NULL CHECK(length(trim(occurred_at))>0),
 authority TEXT NOT NULL CHECK(length(trim(authority))>0),
 reason TEXT NOT NULL CHECK(length(trim(reason))>0),
 kind TEXT NOT NULL CHECK(kind IN ('relocate','restore')),
 reverses_event_id INTEGER REFERENCES revenue_document_locations(id) ON DELETE RESTRICT,
 verification_result TEXT NOT NULL CHECK(verification_result='PASS'),
 manifest_sha256 TEXT NOT NULL CHECK(length(manifest_sha256)=64),
 source_sha256 TEXT CHECK(source_sha256 IS NULL OR length(source_sha256)=64),
 destination_sha256 TEXT NOT NULL CHECK(length(destination_sha256)=64 AND destination_sha256=sha256),
 source_verification TEXT NOT NULL CHECK(source_verification IN ('MATCH','UNAVAILABLE','MISMATCH')),
 UNIQUE(document_id,sequence),
 CHECK((kind='relocate' AND reverses_event_id IS NULL AND source_verification='MATCH' AND source_sha256 IS NOT NULL AND source_sha256=sha256) OR (kind='restore' AND reverses_event_id IS NOT NULL)),
 CHECK((source_verification='MATCH' AND source_sha256 IS NOT NULL AND source_sha256=sha256) OR (source_verification='UNAVAILABLE' AND source_sha256 IS NULL) OR (source_verification='MISMATCH' AND source_sha256 IS NOT NULL AND source_sha256!=sha256))
);

CREATE TRIGGER revenue_document_locations_validate
BEFORE INSERT ON revenue_document_locations
BEGIN
 SELECT CASE WHEN revenue_relocation_authorize(
  NEW.operation_id,NEW.document_id,NEW.document_uid,NEW.sequence,NEW.previous_event_id,
  NEW.old_path,NEW.new_path,NEW.sha256,NEW.size_bytes,NEW.document_fingerprint,
  NEW.occurred_at,NEW.authority,NEW.reason,NEW.kind,NEW.reverses_event_id,
  NEW.verification_result,NEW.manifest_sha256,NEW.source_sha256,
  NEW.destination_sha256,NEW.source_verification
 ) IS NOT 1 THEN RAISE(ABORT,'Revenue document relocation authorization required') END;
 -- Explicit collision rejection also prevents INSERT OR REPLACE from deleting history.
 SELECT CASE WHEN EXISTS(SELECT 1 FROM revenue_document_locations e
  WHERE e.id=NEW.id OR e.operation_id=NEW.operation_id
     OR (e.document_id=NEW.document_id AND e.sequence=NEW.sequence))
  THEN RAISE(ABORT,'Revenue document location replacement forbidden') END;
 SELECT CASE WHEN NOT EXISTS(SELECT 1 FROM revenue_documents d
  WHERE d.id=NEW.document_id AND d.uid=NEW.document_uid
    AND d.sha256=NEW.sha256 AND d.size_bytes=NEW.size_bytes)
  THEN RAISE(ABORT,'Revenue document location identity mismatch') END;
 SELECT CASE WHEN NOT (
  (NEW.sequence=1 AND NEW.previous_event_id IS NULL
   AND NOT EXISTS(SELECT 1 FROM revenue_document_locations e WHERE e.document_id=NEW.document_id)
   AND NEW.old_path=(SELECT storage_path FROM revenue_documents WHERE id=NEW.document_id))
  OR (NEW.sequence>1 AND NEW.previous_event_id IS NOT NULL
   AND EXISTS(SELECT 1 FROM revenue_document_locations p
    WHERE p.id=NEW.previous_event_id AND p.document_id=NEW.document_id
      AND p.document_uid=NEW.document_uid AND p.sequence=NEW.sequence-1
      AND p.new_path=NEW.old_path)
   AND NEW.sequence=(SELECT MAX(sequence)+1 FROM revenue_document_locations WHERE document_id=NEW.document_id))
 ) THEN RAISE(ABORT,'Revenue document location chain conflict') END;
 SELECT CASE WHEN NEW.kind='restore' AND NOT EXISTS(
  SELECT 1 FROM revenue_document_locations p WHERE p.id=NEW.reverses_event_id
   AND p.id=NEW.previous_event_id AND p.document_id=NEW.document_id
   AND p.kind='relocate' AND p.new_path=NEW.old_path AND p.old_path=NEW.new_path
 ) THEN RAISE(ABORT,'Revenue document location restore target mismatch') END;
END;

CREATE TRIGGER revenue_document_locations_no_update
BEFORE UPDATE ON revenue_document_locations
BEGIN SELECT RAISE(ABORT,'Revenue document locations are immutable'); END;

CREATE TRIGGER revenue_document_locations_no_delete
BEFORE DELETE ON revenue_document_locations
BEGIN SELECT RAISE(ABORT,'Revenue document locations are immutable'); END;
