SELECT
    EXISTS (SELECT 1 FROM sqlite_master WHERE type='table' AND name='visa_only_material_uploads')
    AND EXISTS (SELECT 1 FROM sqlite_master WHERE type='trigger' AND name='visa_only_material_uploads_pending_insert')
    AND EXISTS (SELECT 1 FROM sqlite_master WHERE type='trigger' AND name='visa_only_material_uploads_confirm_owned');
