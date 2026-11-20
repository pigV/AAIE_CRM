CREATE TABLE business_change_commands (
 command_key TEXT PRIMARY KEY NOT NULL CHECK(length(command_key)=64),
 command_type TEXT NOT NULL CHECK(length(command_type) BETWEEN 1 AND 128),
 actor_kind TEXT NOT NULL CHECK(actor_kind IN ('user','parent','system','integration')),
 actor_ref TEXT NOT NULL CHECK(length(actor_ref) BETWEEN 1 AND 200),
 request_hash TEXT NOT NULL CHECK(length(request_hash)=64),
 result_json TEXT NOT NULL CHECK(json_valid(result_json)),
 committed_at TEXT NOT NULL
);

CREATE TABLE business_change_events (
 seq INTEGER PRIMARY KEY AUTOINCREMENT,
 event_id TEXT NOT NULL UNIQUE,
 command_key TEXT NOT NULL REFERENCES business_change_commands(command_key) ON DELETE RESTRICT,
 event_type TEXT NOT NULL CHECK(length(event_type) BETWEEN 1 AND 128),
 source_kind TEXT NOT NULL CHECK(length(source_kind) BETWEEN 1 AND 64),
 source_id TEXT NOT NULL CHECK(length(source_id) BETWEEN 1 AND 200),
 source_version INTEGER NOT NULL CHECK(source_version>0),
 occurred_at TEXT NOT NULL,
 UNIQUE(source_kind,source_id,source_version,event_type)
);
CREATE INDEX business_change_events_command_idx ON business_change_events(command_key);

CREATE TABLE business_change_versions (
 subject_kind TEXT NOT NULL CHECK(length(subject_kind) BETWEEN 1 AND 64),
 subject_id TEXT NOT NULL CHECK(length(subject_id) BETWEEN 1 AND 200),
 version INTEGER NOT NULL CHECK(version>0),
 last_event_seq INTEGER NOT NULL REFERENCES business_change_events(seq) ON DELETE RESTRICT,
 updated_at TEXT NOT NULL,
 PRIMARY KEY(subject_kind,subject_id)
);

CREATE TABLE business_change_impacts (
 event_seq INTEGER NOT NULL REFERENCES business_change_events(seq) ON DELETE RESTRICT,
 subject_kind TEXT NOT NULL,
 subject_id TEXT NOT NULL,
 subject_version INTEGER NOT NULL CHECK(subject_version>0),
 relation TEXT NOT NULL CHECK(relation IN ('source','current','previous','both')),
 PRIMARY KEY(event_seq,subject_kind,subject_id),
 FOREIGN KEY(subject_kind,subject_id) REFERENCES business_change_versions(subject_kind,subject_id) ON DELETE RESTRICT
);
CREATE INDEX business_change_impacts_subject_idx
 ON business_change_impacts(subject_kind,subject_id,event_seq);

CREATE TABLE business_change_deliveries (
 event_seq INTEGER NOT NULL,
 handler_name TEXT NOT NULL CHECK(length(handler_name) BETWEEN 1 AND 64),
 handler_version TEXT NOT NULL CHECK(length(handler_version) BETWEEN 1 AND 64),
 subject_kind TEXT NOT NULL,
 subject_id TEXT NOT NULL,
 state TEXT NOT NULL CHECK(state IN ('pending','processing','done','failed','superseded')),
 attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts>=0),
 next_attempt_ms INTEGER NOT NULL DEFAULT 0 CHECK(next_attempt_ms>=0),
 lease_token TEXT,
 lease_until_ms INTEGER,
 dependency_keys_json TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(dependency_keys_json)),
 last_error_code TEXT,
 processed_subject_version INTEGER CHECK(processed_subject_version>0),
 updated_at TEXT NOT NULL,
 PRIMARY KEY(event_seq,handler_name,handler_version,subject_kind,subject_id),
 FOREIGN KEY(event_seq,subject_kind,subject_id)
  REFERENCES business_change_impacts(event_seq,subject_kind,subject_id) ON DELETE RESTRICT,
 CHECK((state='processing' AND lease_token IS NOT NULL AND lease_until_ms IS NOT NULL)
    OR (state<>'processing' AND lease_token IS NULL AND lease_until_ms IS NULL))
);
CREATE INDEX business_change_deliveries_due_idx ON business_change_deliveries(state,next_attempt_ms,event_seq);
CREATE INDEX business_change_deliveries_lease_idx ON business_change_deliveries(state,lease_until_ms);
