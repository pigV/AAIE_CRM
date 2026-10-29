SELECT count(*) = 0 FROM pragma_foreign_key_check;
SELECT NOT EXISTS(
  SELECT university_id,pathway_code FROM higher_education_pathway_combinations
  GROUP BY university_id,pathway_code HAVING count(*) > 1
);
SELECT NOT EXISTS(
  SELECT c.id FROM higher_education_pathway_combinations c
  JOIN higher_education_providers u ON u.id=c.university_id
  WHERE u.provider_type<>'University'
);
SELECT NOT EXISTS(
  SELECT s.id FROM higher_education_pathway_segments s
  JOIN higher_education_pathway_combinations c ON c.id=s.pathway_combination_id
  WHERE s.target_university_id<>c.university_id
);
SELECT NOT EXISTS(
  SELECT university_id,normalized_name,course_level FROM higher_education_courses
  GROUP BY university_id,normalized_name,course_level HAVING count(*) > 1
);
SELECT NOT EXISTS(
  SELECT s.id FROM higher_education_package_segments s
  LEFT JOIN higher_education_providers u ON u.id=s.target_university_id
  WHERE s.target_university_id IS NOT NULL AND (u.id IS NULL OR u.provider_type<>'University')
);
SELECT NOT EXISTS(
  SELECT s.id FROM higher_education_pathway_segments s
  JOIN higher_education_pathway_combinations c ON c.id=s.pathway_combination_id
  WHERE s.provider_id<>c.university_id AND NOT EXISTS(
    SELECT 1 FROM higher_education_provider_relationships r
    WHERE r.university_id=c.university_id AND r.pathway_provider_id=s.provider_id AND r.active_status='Active'
  )
);
SELECT NOT EXISTS(
  SELECT c.id FROM higher_education_pathway_combinations c
  WHERE EXISTS(SELECT 1 FROM higher_education_pathway_segments s WHERE s.pathway_combination_id=c.id)
    AND c.final_course_level<>(
      SELECT s.course_level FROM higher_education_pathway_segments s
      WHERE s.pathway_combination_id=c.id ORDER BY s.segment_order DESC LIMIT 1
    )
);
SELECT NOT EXISTS(
  SELECT i.id FROM higher_education_plan_items i
  JOIN higher_education_pathway_combinations c ON c.id=i.pathway_combination_id
  WHERE c.university_id<>i.provider_id
);
SELECT NOT EXISTS(
  SELECT a.id FROM higher_education_applications a
  JOIN higher_education_pathway_combinations c ON c.id=a.pathway_combination_id
  WHERE c.university_id<>a.provider_id
);
