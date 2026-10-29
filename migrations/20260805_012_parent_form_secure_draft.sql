CREATE TABLE IF NOT EXISTS parent_form_access_bindings (
  token TEXT PRIMARY KEY REFERENCES parent_form_tokens(token) ON DELETE CASCADE,
  bound_wechat_openid TEXT NOT NULL,
  bound_wechat_unionid TEXT,
  bound_wechat_nickname TEXT,
  bound_wechat_avatar_url TEXT,
  bound_at TEXT NOT NULL,
  last_login_at TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS parent_form_drafts (
  token TEXT PRIMARY KEY REFERENCES parent_form_tokens(token) ON DELETE CASCADE,
  payload_encrypted TEXT NOT NULL,
  revision INTEGER NOT NULL CHECK (revision >= 1),
  save_source TEXT NOT NULL CHECK (save_source IN ('auto','manual')),
  last_saved_at TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_parent_form_bindings_openid
  ON parent_form_access_bindings(bound_wechat_openid, updated_at DESC);

CREATE INDEX IF NOT EXISTS idx_parent_form_drafts_saved
  ON parent_form_drafts(last_saved_at DESC);
