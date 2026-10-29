-- Canonical source modification timestamp for content source materials.
-- Schema-only: existing business rows remain byte-for-byte unchanged.

ALTER TABLE content_materials
ADD COLUMN source_modified_at TEXT;

CREATE INDEX idx_content_materials_source_modified_at
ON content_materials(source_modified_at DESC,id DESC);

CREATE INDEX idx_content_materials_script_id_id
ON content_materials(script_id,id DESC);
