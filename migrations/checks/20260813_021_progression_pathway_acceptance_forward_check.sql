SELECT count(*) = 1
FROM sqlite_master
WHERE type='table' AND name='progression_pathway_acceptances';

SELECT count(*) = 1
FROM sqlite_master
WHERE type='table' AND name='progression_pathway_components';

SELECT count(*) = 1
FROM sqlite_master
WHERE type='index'
  AND name='uq_progression_case_offers_one_accepted_per_target';

SELECT count(*) = 0
FROM sqlite_master
WHERE type='index' AND name='uq_progression_case_offers_one_accepted';

SELECT count(*) = 1
FROM sqlite_master
WHERE type='index'
  AND name='uq_progression_pathway_components_active_application';

SELECT count(*) = 0 FROM progression_pathway_acceptances;
SELECT count(*) = 0 FROM progression_pathway_components;
SELECT count(*) = 0 FROM pragma_foreign_key_check;
SELECT quick_check = 'ok' FROM pragma_quick_check;
