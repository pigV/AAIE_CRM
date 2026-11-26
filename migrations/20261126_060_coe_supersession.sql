-- Preserve replacement history when the latest uploaded CoE is edited or voided.
ALTER TABLE coe_oshc ADD COLUMN superseded_by_coe_id INTEGER
  REFERENCES coe_oshc(id) ON DELETE RESTRICT
  CHECK (superseded_by_coe_id IS NULL OR superseded_by_coe_id > id);
