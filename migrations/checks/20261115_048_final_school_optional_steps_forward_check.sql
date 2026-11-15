SELECT EXISTS(
    SELECT 1 FROM sqlite_master WHERE type='view' AND name='final_school_optional_steps'
) AND EXISTS(
    SELECT 1 FROM sqlite_master WHERE type='trigger'
      AND name='final_school_submission_ownership_insert'
      AND sql LIKE '%final_school_optional_steps%'
);
