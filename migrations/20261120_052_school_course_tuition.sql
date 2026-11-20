-- Explicit course identity and immutable evidence; no historical backfill.
CREATE TABLE revenue_school_courses (
  id INTEGER PRIMARY KEY,
  uid TEXT NOT NULL UNIQUE,
  commercial_case_id INTEGER NOT NULL REFERENCES revenue_commercial_cases(id) ON DELETE RESTRICT,
  course_kind TEXT NOT NULL CHECK(course_kind IN ('language','main')),
  school_id INTEGER NOT NULL REFERENCES schools(id) ON DELETE RESTRICT,
  course_name TEXT NOT NULL CHECK(length(trim(course_name))>0),
  enrolment_component_id INTEGER UNIQUE REFERENCES revenue_enrolment_components(id) ON DELETE RESTRICT,
  status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','superseded','withdrawn')),
  confirmed_by INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
  confirmed_at TEXT NOT NULL
);
CREATE UNIQUE INDEX uq_school_course_identity
  ON revenue_school_courses(commercial_case_id,course_kind,school_id,lower(trim(course_name)))
  WHERE status='active';
CREATE TABLE revenue_school_course_evidence (
  id INTEGER PRIMARY KEY,
  course_id INTEGER NOT NULL REFERENCES revenue_school_courses(id) ON DELETE RESTRICT,
  offer_id INTEGER REFERENCES offers(id) ON DELETE RESTRICT,
  coe_oshc_id INTEGER REFERENCES coe_oshc(id) ON DELETE RESTRICT,
  document_id INTEGER NOT NULL REFERENCES revenue_documents(id) ON DELETE RESTRICT,
  evidence_snapshot TEXT NOT NULL CHECK(json_valid(evidence_snapshot)),
  confirmed_by INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
  confirmed_at TEXT NOT NULL,
  CHECK((offer_id IS NOT NULL AND coe_oshc_id IS NULL)
     OR (offer_id IS NULL AND coe_oshc_id IS NOT NULL))
);
CREATE UNIQUE INDEX uq_school_course_offer_evidence
  ON revenue_school_course_evidence(course_id,offer_id,document_id) WHERE offer_id IS NOT NULL;
CREATE UNIQUE INDEX uq_school_course_coe_evidence
  ON revenue_school_course_evidence(course_id,coe_oshc_id,document_id) WHERE coe_oshc_id IS NOT NULL;
