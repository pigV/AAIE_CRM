SELECT count(*) = 0 FROM pragma_foreign_key_check;
SELECT NOT EXISTS(
  SELECT application_cycle_id,provider_id FROM higher_education_applications
  WHERE is_current=1 GROUP BY application_cycle_id,provider_id HAVING count(*) > 1
);
SELECT NOT EXISTS(
  SELECT application_cycle_id FROM higher_education_offers
  WHERE status='Accepted' GROUP BY application_cycle_id HAVING count(*) > 1
);
SELECT NOT EXISTS(
  SELECT 1 FROM higher_education_package_segments s
  LEFT JOIN higher_education_applications a ON a.id=s.application_id
  WHERE a.id IS NULL OR a.course_type<>'Package'
);
SELECT NOT EXISTS(
  SELECT 1 FROM higher_education_provider_relationships r
  JOIN higher_education_providers u ON u.id=r.university_id
  JOIN higher_education_providers p ON p.id=r.pathway_provider_id
  WHERE u.provider_type<>'University' OR p.provider_type<>'Pathway Provider'
);
SELECT NOT EXISTS(
  SELECT 1 FROM higher_education_temporary_commission_shares s
  JOIN higher_education_package_segments p ON p.id=s.package_segment_id
  WHERE p.application_id<>s.application_id
);
SELECT NOT EXISTS(
  SELECT 1 FROM higher_education_commission_lines l
  LEFT JOIN higher_education_temporary_commission_shares s
    ON l.source_type='Temporary Share' AND s.id=l.source_id
  WHERE l.source_type='Temporary Share'
    AND (s.id IS NULL OR s.status<>'Approved' OR s.application_id<>l.application_id
         OR NOT (s.package_segment_id IS l.package_segment_id))
);
SELECT NOT EXISTS(
  SELECT 1 FROM higher_education_coe_links l
  LEFT JOIN higher_education_package_segments p ON p.id=l.package_segment_id
  WHERE (l.scope='Whole Application' AND l.package_segment_id IS NOT NULL)
     OR (l.scope='Package Segment' AND (p.id IS NULL OR p.application_id<>l.application_id))
);
