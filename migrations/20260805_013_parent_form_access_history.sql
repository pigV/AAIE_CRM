CREATE TABLE IF NOT EXISTS parent_form_access_events (
  id INTEGER PRIMARY KEY,
  token TEXT NOT NULL REFERENCES parent_form_tokens(token) ON DELETE CASCADE,
  event_type TEXT NOT NULL CHECK (event_type IN ('opened','authenticated')),
  occurred_at TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_parent_form_access_events_token_time
  ON parent_form_access_events(token, occurred_at DESC, id DESC);

CREATE INDEX IF NOT EXISTS idx_parent_form_access_events_token_type_time
  ON parent_form_access_events(token, event_type, occurred_at DESC, id DESC);
