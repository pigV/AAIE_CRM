ALTER TABLE students ADD COLUMN application_stream TEXT
  CHECK(application_stream IS NULL OR application_stream IN ('school_education','higher_education'));
ALTER TABLE students ADD COLUMN higher_education_course_type TEXT
  CHECK(higher_education_course_type IS NULL OR higher_education_course_type IN ('Foundation','Diploma','Bachelor','Postgraduate','Package'));

ALTER TABLE application_cycles ADD COLUMN education_stream TEXT NOT NULL DEFAULT 'school_education'
  CHECK(education_stream IN ('school_education','higher_education'));
DROP INDEX uq_application_cycles_active_student;
CREATE UNIQUE INDEX uq_application_cycles_active_student_stream
  ON application_cycles(student_id,education_stream)
  WHERE status='active' AND student_id IS NOT NULL;
CREATE INDEX idx_application_cycles_student_stream
  ON application_cycles(student_id,education_stream,status,id);

CREATE TABLE higher_education_providers (
  id INTEGER PRIMARY KEY,
  provider_name TEXT NOT NULL CHECK(length(trim(provider_name)) > 0),
  normalized_name TEXT NOT NULL UNIQUE CHECK(length(trim(normalized_name)) > 0),
  provider_type TEXT NOT NULL CHECK(provider_type IN ('University','Pathway Provider')),
  review_status TEXT NOT NULL DEFAULT 'Pending' CHECK(review_status IN ('Pending','Approved','Rejected','Merged')),
  active_status TEXT NOT NULL DEFAULT 'Active' CHECK(active_status IN ('Active','Inactive')),
  source_url TEXT,
  evidence TEXT,
  created_by_user_id INTEGER REFERENCES users(id) ON DELETE RESTRICT,
  created_at TEXT NOT NULL,
  updated_by_user_id INTEGER REFERENCES users(id) ON DELETE RESTRICT,
  updated_at TEXT NOT NULL
);

CREATE TABLE higher_education_provider_relationships (
  id INTEGER PRIMARY KEY,
  university_id INTEGER NOT NULL REFERENCES higher_education_providers(id) ON DELETE RESTRICT,
  pathway_provider_id INTEGER NOT NULL REFERENCES higher_education_providers(id) ON DELETE RESTRICT,
  relationship_type TEXT NOT NULL CHECK(relationship_type IN ('Direct','Partner')),
  effective_from TEXT,
  effective_to TEXT,
  active_status TEXT NOT NULL DEFAULT 'Active' CHECK(active_status IN ('Active','Inactive')),
  source_url TEXT,
  evidence TEXT,
  created_by_user_id INTEGER REFERENCES users(id) ON DELETE RESTRICT,
  created_at TEXT NOT NULL,
  updated_by_user_id INTEGER REFERENCES users(id) ON DELETE RESTRICT,
  updated_at TEXT NOT NULL,
  CHECK(university_id <> pathway_provider_id),
  CHECK(effective_to IS NULL OR effective_from IS NULL OR effective_to >= effective_from),
  UNIQUE(university_id,pathway_provider_id,relationship_type,effective_from)
);
CREATE INDEX idx_he_provider_relationship_pathway ON higher_education_provider_relationships(pathway_provider_id,active_status);

CREATE TRIGGER higher_education_provider_relationship_types_insert
BEFORE INSERT ON higher_education_provider_relationships
WHEN NOT EXISTS(SELECT 1 FROM higher_education_providers WHERE id=NEW.university_id AND provider_type='University')
  OR NOT EXISTS(SELECT 1 FROM higher_education_providers WHERE id=NEW.pathway_provider_id AND provider_type='Pathway Provider')
BEGIN SELECT RAISE(ABORT, 'higher education provider relationship type mismatch'); END;
CREATE TRIGGER higher_education_provider_relationship_types_update
BEFORE UPDATE OF university_id,pathway_provider_id ON higher_education_provider_relationships
WHEN NOT EXISTS(SELECT 1 FROM higher_education_providers WHERE id=NEW.university_id AND provider_type='University')
  OR NOT EXISTS(SELECT 1 FROM higher_education_providers WHERE id=NEW.pathway_provider_id AND provider_type='Pathway Provider')
BEGIN SELECT RAISE(ABORT, 'higher education provider relationship type mismatch'); END;

CREATE TABLE higher_education_course_names (
  id INTEGER PRIMARY KEY,
  course_name TEXT NOT NULL,
  normalized_name TEXT NOT NULL UNIQUE,
  created_by_user_id INTEGER REFERENCES users(id) ON DELETE RESTRICT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE higher_education_provider_course_names (
  provider_id INTEGER NOT NULL REFERENCES higher_education_providers(id) ON DELETE RESTRICT,
  course_name_id INTEGER NOT NULL REFERENCES higher_education_course_names(id) ON DELETE RESTRICT,
  usage_count INTEGER NOT NULL DEFAULT 1 CHECK(usage_count > 0),
  last_used_at TEXT NOT NULL,
  PRIMARY KEY(provider_id,course_name_id)
);

CREATE TABLE higher_education_plans (
  id INTEGER PRIMARY KEY,
  application_cycle_id INTEGER NOT NULL REFERENCES application_cycles(id) ON DELETE RESTRICT,
  status TEXT NOT NULL DEFAULT 'Draft' CHECK(status IN ('Draft','Confirmed','Withdrawn','Converted')),
  confirmed_at TEXT,
  confirmed_by_user_id INTEGER REFERENCES users(id) ON DELETE RESTRICT,
  created_by_user_id INTEGER REFERENCES users(id) ON DELETE RESTRICT,
  created_at TEXT NOT NULL,
  updated_by_user_id INTEGER REFERENCES users(id) ON DELETE RESTRICT,
  updated_at TEXT NOT NULL,
  CHECK((status='Confirmed' AND confirmed_at IS NOT NULL) OR status<>'Confirmed')
);
CREATE INDEX idx_he_plans_cycle ON higher_education_plans(application_cycle_id,status,id);

CREATE TABLE higher_education_plan_items (
  id INTEGER PRIMARY KEY,
  plan_id INTEGER NOT NULL REFERENCES higher_education_plans(id) ON DELETE RESTRICT,
  provider_id INTEGER NOT NULL REFERENCES higher_education_providers(id) ON DELETE RESTRICT,
  course_type TEXT NOT NULL CHECK(course_type IN ('Foundation','Diploma','Bachelor','Postgraduate','Package')),
  course_name TEXT NOT NULL CHECK(length(trim(course_name)) > 0),
  intake TEXT,
  item_order INTEGER NOT NULL DEFAULT 1 CHECK(item_order > 0),
  created_by_user_id INTEGER REFERENCES users(id) ON DELETE RESTRICT,
  created_at TEXT NOT NULL,
  updated_by_user_id INTEGER REFERENCES users(id) ON DELETE RESTRICT,
  updated_at TEXT NOT NULL,
  UNIQUE(plan_id,item_order)
);

CREATE TABLE higher_education_applications (
  id INTEGER PRIMARY KEY,
  application_cycle_id INTEGER NOT NULL REFERENCES application_cycles(id) ON DELETE RESTRICT,
  source_plan_item_id INTEGER REFERENCES higher_education_plan_items(id) ON DELETE RESTRICT,
  provider_id INTEGER NOT NULL REFERENCES higher_education_providers(id) ON DELETE RESTRICT,
  course_type TEXT NOT NULL CHECK(course_type IN ('Foundation','Diploma','Bachelor','Postgraduate','Package')),
  course_name TEXT NOT NULL CHECK(length(trim(course_name)) > 0),
  intake TEXT NOT NULL CHECK(length(trim(intake)) > 0),
  submission_channel TEXT NOT NULL CHECK(submission_channel IN ('Direct','Master Agent')),
  master_agent_id INTEGER,
  status TEXT NOT NULL DEFAULT 'Draft' CHECK(status IN ('Draft','Submitted','Offer Received','Closed','Withdrawn','Superseded')),
  is_current INTEGER NOT NULL DEFAULT 1 CHECK(is_current IN (0,1)),
  submitted_at TEXT,
  created_by_user_id INTEGER REFERENCES users(id) ON DELETE RESTRICT,
  created_at TEXT NOT NULL,
  updated_by_user_id INTEGER REFERENCES users(id) ON DELETE RESTRICT,
  updated_at TEXT NOT NULL,
  CHECK(submission_channel='Direct' OR master_agent_id IS NOT NULL)
);
CREATE UNIQUE INDEX uq_he_application_current_provider ON higher_education_applications(application_cycle_id,provider_id) WHERE is_current=1;
CREATE INDEX idx_he_applications_cycle_status ON higher_education_applications(application_cycle_id,status,id);

CREATE TABLE higher_education_package_segments (
  id INTEGER PRIMARY KEY,
  application_id INTEGER NOT NULL REFERENCES higher_education_applications(id) ON DELETE RESTRICT,
  segment_order INTEGER NOT NULL CHECK(segment_order > 0),
  teaching_provider_id INTEGER NOT NULL REFERENCES higher_education_providers(id) ON DELETE RESTRICT,
  course_type TEXT NOT NULL CHECK(course_type IN ('Foundation','Diploma','Bachelor','Postgraduate')),
  course_name TEXT NOT NULL CHECK(length(trim(course_name)) > 0),
  intake TEXT NOT NULL CHECK(length(trim(intake)) > 0),
  created_by_user_id INTEGER REFERENCES users(id) ON DELETE RESTRICT,
  created_at TEXT NOT NULL,
  updated_by_user_id INTEGER REFERENCES users(id) ON DELETE RESTRICT,
  updated_at TEXT NOT NULL,
  UNIQUE(application_id,segment_order)
);
CREATE TRIGGER higher_education_package_segment_application_insert
BEFORE INSERT ON higher_education_package_segments
WHEN NOT EXISTS(SELECT 1 FROM higher_education_applications WHERE id=NEW.application_id AND course_type='Package')
BEGIN SELECT RAISE(ABORT, 'package segment requires a Package application'); END;

CREATE TABLE higher_education_offers (
  id INTEGER PRIMARY KEY,
  application_id INTEGER NOT NULL REFERENCES higher_education_applications(id) ON DELETE RESTRICT,
  application_cycle_id INTEGER NOT NULL REFERENCES application_cycles(id) ON DELETE RESTRICT,
  package_segment_id INTEGER REFERENCES higher_education_package_segments(id) ON DELETE RESTRICT,
  status TEXT NOT NULL DEFAULT 'Received' CHECK(status IN ('Received','Accepted','Declined','Withdrawn')),
  received_at TEXT NOT NULL,
  accepted_at TEXT,
  accepted_by_user_id INTEGER REFERENCES users(id) ON DELETE RESTRICT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX uq_he_offer_accepted_cycle ON higher_education_offers(application_cycle_id)
  WHERE status='Accepted';
CREATE TRIGGER higher_education_offer_segment_scope_insert
BEFORE INSERT ON higher_education_offers
WHEN NEW.package_segment_id IS NOT NULL AND NOT EXISTS(
  SELECT 1 FROM higher_education_package_segments
  WHERE id=NEW.package_segment_id AND application_id=NEW.application_id
)
BEGIN SELECT RAISE(ABORT, 'offer segment is outside application'); END;

CREATE TABLE higher_education_temporary_commission_shares (
  id INTEGER PRIMARY KEY,
  application_id INTEGER NOT NULL REFERENCES higher_education_applications(id) ON DELETE RESTRICT,
  package_segment_id INTEGER REFERENCES higher_education_package_segments(id) ON DELETE RESTRICT,
  share_percent REAL NOT NULL CHECK(share_percent > 0 AND share_percent <= 100),
  status TEXT NOT NULL DEFAULT 'Pending' CHECK(status IN ('Pending','Approved','Rejected','Expired')),
  evidence TEXT NOT NULL CHECK(length(trim(evidence)) > 0),
  review_reason TEXT,
  created_by_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
  reviewed_by_user_id INTEGER REFERENCES users(id) ON DELETE RESTRICT,
  reviewed_at TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  CHECK((status='Pending' AND reviewed_at IS NULL AND reviewed_by_user_id IS NULL)
     OR (status<>'Pending' AND reviewed_at IS NOT NULL AND reviewed_by_user_id IS NOT NULL))
);
CREATE UNIQUE INDEX uq_he_temporary_share_pending_whole
  ON higher_education_temporary_commission_shares(application_id)
  WHERE status='Pending' AND package_segment_id IS NULL;
CREATE UNIQUE INDEX uq_he_temporary_share_pending_segment
  ON higher_education_temporary_commission_shares(application_id,package_segment_id)
  WHERE status='Pending' AND package_segment_id IS NOT NULL;

CREATE TABLE higher_education_commission_lines (
  id INTEGER PRIMARY KEY,
  application_id INTEGER NOT NULL REFERENCES higher_education_applications(id) ON DELETE RESTRICT,
  package_segment_id INTEGER REFERENCES higher_education_package_segments(id) ON DELETE RESTRICT,
  teaching_provider_id INTEGER NOT NULL REFERENCES higher_education_providers(id) ON DELETE RESTRICT,
  source_type TEXT NOT NULL CHECK(source_type IN ('Agreement','Temporary Share')),
  source_id INTEGER NOT NULL,
  status TEXT NOT NULL DEFAULT 'Draft' CHECK(status IN ('Draft','Claimable','Invoiced','Paid','Void')),
  created_by_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(application_id,package_segment_id,source_type,source_id)
);

CREATE TABLE higher_education_coe_links (
  id INTEGER PRIMARY KEY,
  application_id INTEGER NOT NULL REFERENCES higher_education_applications(id) ON DELETE RESTRICT,
  package_segment_id INTEGER REFERENCES higher_education_package_segments(id) ON DELETE RESTRICT,
  coe_oshc_id INTEGER NOT NULL REFERENCES coe_oshc(id) ON DELETE RESTRICT,
  scope TEXT NOT NULL CHECK(scope IN ('Whole Application','Package Segment')),
  created_by_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
  created_at TEXT NOT NULL,
  CHECK((scope='Whole Application' AND package_segment_id IS NULL)
     OR (scope='Package Segment' AND package_segment_id IS NOT NULL)),
  UNIQUE(coe_oshc_id)
);

CREATE TRIGGER higher_education_segment_scope_share_insert
BEFORE INSERT ON higher_education_temporary_commission_shares
WHEN NOT EXISTS(
  SELECT 1 FROM higher_education_applications
  WHERE id=NEW.application_id AND submission_channel='Master Agent'
) OR (NEW.package_segment_id IS NOT NULL AND NOT EXISTS(
  SELECT 1 FROM higher_education_package_segments
  WHERE id=NEW.package_segment_id AND application_id=NEW.application_id
))
BEGIN SELECT RAISE(ABORT, 'temporary share segment is outside application'); END;

CREATE TRIGGER higher_education_segment_scope_commission_insert
BEFORE INSERT ON higher_education_commission_lines
WHEN NEW.package_segment_id IS NOT NULL AND NOT EXISTS(
  SELECT 1 FROM higher_education_package_segments
  WHERE id=NEW.package_segment_id AND application_id=NEW.application_id
)
BEGIN SELECT RAISE(ABORT, 'commission segment is outside application'); END;

CREATE TRIGGER higher_education_temporary_share_commission_insert
BEFORE INSERT ON higher_education_commission_lines
WHEN NEW.source_type='Temporary Share' AND NOT EXISTS(
  SELECT 1 FROM higher_education_temporary_commission_shares s
  WHERE s.id=NEW.source_id AND s.application_id=NEW.application_id
    AND s.status='Approved'
    AND (s.package_segment_id IS NEW.package_segment_id)
)
BEGIN SELECT RAISE(ABORT, 'temporary share is not approved for commission line'); END;

CREATE TRIGGER higher_education_segment_scope_coe_insert
BEFORE INSERT ON higher_education_coe_links
WHEN NOT EXISTS(
  SELECT 1 FROM higher_education_applications a
  JOIN coe_oshc c ON c.id=NEW.coe_oshc_id AND c.application_cycle_id=a.application_cycle_id
  WHERE a.id=NEW.application_id
) OR (NEW.package_segment_id IS NOT NULL AND NOT EXISTS(
  SELECT 1 FROM higher_education_package_segments
  WHERE id=NEW.package_segment_id AND application_id=NEW.application_id
))
BEGIN SELECT RAISE(ABORT, 'COE segment is outside application'); END;

CREATE TABLE higher_education_audit_log (
  id INTEGER PRIMARY KEY,
  application_cycle_id INTEGER REFERENCES application_cycles(id) ON DELETE RESTRICT,
  entity_type TEXT NOT NULL,
  entity_id INTEGER,
  action TEXT NOT NULL,
  old_value_json TEXT,
  new_value_json TEXT,
  reason TEXT,
  actor_user_id INTEGER REFERENCES users(id) ON DELETE RESTRICT,
  occurred_at TEXT NOT NULL
);

CREATE TRIGGER higher_education_plan_cycle_insert
BEFORE INSERT ON higher_education_plans
WHEN NOT EXISTS(SELECT 1 FROM application_cycles WHERE id=NEW.application_cycle_id AND education_stream='higher_education')
BEGIN SELECT RAISE(ABORT, 'higher education plan requires a higher education cycle'); END;

-- Australian Government Study Australia list, verified 2026-08-13.
INSERT INTO higher_education_providers(
  provider_name,normalized_name,provider_type,review_status,active_status,source_url,evidence,created_at,updated_at
) VALUES
('Adelaide University','adelaide university','University','Approved','Active','https://www.studyaustralia.gov.au/en/plan-your-studies/list-of-australian-universities','Australian Government Study Australia university list','2026-08-13','2026-08-13'),
('Australian Catholic University','australian catholic university','University','Approved','Active','https://www.studyaustralia.gov.au/en/plan-your-studies/list-of-australian-universities','Australian Government Study Australia university list','2026-08-13','2026-08-13'),
('Australian University of Theology','australian university of theology','University','Approved','Active','https://www.studyaustralia.gov.au/en/plan-your-studies/list-of-australian-universities','Australian Government Study Australia university list','2026-08-13','2026-08-13'),
('Australian National University','australian national university','University','Approved','Active','https://www.studyaustralia.gov.au/en/plan-your-studies/list-of-australian-universities','Australian Government Study Australia university list','2026-08-13','2026-08-13'),
('Avondale University','avondale university','University','Approved','Active','https://www.studyaustralia.gov.au/en/plan-your-studies/list-of-australian-universities','Australian Government Study Australia university list','2026-08-13','2026-08-13'),
('Bond University','bond university','University','Approved','Active','https://www.studyaustralia.gov.au/en/plan-your-studies/list-of-australian-universities','Australian Government Study Australia university list','2026-08-13','2026-08-13'),
('Charles Darwin University','charles darwin university','University','Approved','Active','https://www.studyaustralia.gov.au/en/plan-your-studies/list-of-australian-universities','Australian Government Study Australia university list','2026-08-13','2026-08-13'),
('Charles Sturt University','charles sturt university','University','Approved','Active','https://www.studyaustralia.gov.au/en/plan-your-studies/list-of-australian-universities','Australian Government Study Australia university list','2026-08-13','2026-08-13'),
('CQ University','cq university','University','Approved','Active','https://www.studyaustralia.gov.au/en/plan-your-studies/list-of-australian-universities','Australian Government Study Australia university list','2026-08-13','2026-08-13'),
('Curtin University','curtin university','University','Approved','Active','https://www.studyaustralia.gov.au/en/plan-your-studies/list-of-australian-universities','Australian Government Study Australia university list','2026-08-13','2026-08-13'),
('Deakin University','deakin university','University','Approved','Active','https://www.studyaustralia.gov.au/en/plan-your-studies/list-of-australian-universities','Australian Government Study Australia university list','2026-08-13','2026-08-13'),
('Edith Cowan University','edith cowan university','University','Approved','Active','https://www.studyaustralia.gov.au/en/plan-your-studies/list-of-australian-universities','Australian Government Study Australia university list','2026-08-13','2026-08-13'),
('Federation University of Australia','federation university of australia','University','Approved','Active','https://www.studyaustralia.gov.au/en/plan-your-studies/list-of-australian-universities','Australian Government Study Australia university list','2026-08-13','2026-08-13'),
('Flinders University','flinders university','University','Approved','Active','https://www.studyaustralia.gov.au/en/plan-your-studies/list-of-australian-universities','Australian Government Study Australia university list','2026-08-13','2026-08-13'),
('Griffith University','griffith university','University','Approved','Active','https://www.studyaustralia.gov.au/en/plan-your-studies/list-of-australian-universities','Australian Government Study Australia university list','2026-08-13','2026-08-13'),
('James Cook University','james cook university','University','Approved','Active','https://www.studyaustralia.gov.au/en/plan-your-studies/list-of-australian-universities','Australian Government Study Australia university list','2026-08-13','2026-08-13'),
('La Trobe University','la trobe university','University','Approved','Active','https://www.studyaustralia.gov.au/en/plan-your-studies/list-of-australian-universities','Australian Government Study Australia university list','2026-08-13','2026-08-13'),
('Macquarie University','macquarie university','University','Approved','Active','https://www.studyaustralia.gov.au/en/plan-your-studies/list-of-australian-universities','Australian Government Study Australia university list','2026-08-13','2026-08-13'),
('Monash University','monash university','University','Approved','Active','https://www.studyaustralia.gov.au/en/plan-your-studies/list-of-australian-universities','Australian Government Study Australia university list','2026-08-13','2026-08-13'),
('Murdoch University','murdoch university','University','Approved','Active','https://www.studyaustralia.gov.au/en/plan-your-studies/list-of-australian-universities','Australian Government Study Australia university list','2026-08-13','2026-08-13'),
('Queensland University of Technology','queensland university of technology','University','Approved','Active','https://www.studyaustralia.gov.au/en/plan-your-studies/list-of-australian-universities','Australian Government Study Australia university list','2026-08-13','2026-08-13'),
('RMIT University','rmit university','University','Approved','Active','https://www.studyaustralia.gov.au/en/plan-your-studies/list-of-australian-universities','Australian Government Study Australia university list','2026-08-13','2026-08-13'),
('Southern Cross University','southern cross university','University','Approved','Active','https://www.studyaustralia.gov.au/en/plan-your-studies/list-of-australian-universities','Australian Government Study Australia university list','2026-08-13','2026-08-13'),
('Swinburne University of Technology','swinburne university of technology','University','Approved','Active','https://www.studyaustralia.gov.au/en/plan-your-studies/list-of-australian-universities','Australian Government Study Australia university list','2026-08-13','2026-08-13'),
('Torrens University Australia','torrens university australia','University','Approved','Active','https://www.studyaustralia.gov.au/en/plan-your-studies/list-of-australian-universities','Australian Government Study Australia university list','2026-08-13','2026-08-13'),
('University of Canberra','university of canberra','University','Approved','Active','https://www.studyaustralia.gov.au/en/plan-your-studies/list-of-australian-universities','Australian Government Study Australia university list','2026-08-13','2026-08-13'),
('University of Divinity','university of divinity','University','Approved','Active','https://www.studyaustralia.gov.au/en/plan-your-studies/list-of-australian-universities','Australian Government Study Australia university list','2026-08-13','2026-08-13'),
('University of Melbourne','university of melbourne','University','Approved','Active','https://www.studyaustralia.gov.au/en/plan-your-studies/list-of-australian-universities','Australian Government Study Australia university list','2026-08-13','2026-08-13'),
('University of New England','university of new england','University','Approved','Active','https://www.studyaustralia.gov.au/en/plan-your-studies/list-of-australian-universities','Australian Government Study Australia university list','2026-08-13','2026-08-13'),
('University of New South Wales','university of new south wales','University','Approved','Active','https://www.studyaustralia.gov.au/en/plan-your-studies/list-of-australian-universities','Australian Government Study Australia university list','2026-08-13','2026-08-13'),
('University of Newcastle','university of newcastle','University','Approved','Active','https://www.studyaustralia.gov.au/en/plan-your-studies/list-of-australian-universities','Australian Government Study Australia university list','2026-08-13','2026-08-13'),
('University of Notre Dame Australia','university of notre dame australia','University','Approved','Active','https://www.studyaustralia.gov.au/en/plan-your-studies/list-of-australian-universities','Australian Government Study Australia university list','2026-08-13','2026-08-13'),
('University of Queensland','university of queensland','University','Approved','Active','https://www.studyaustralia.gov.au/en/plan-your-studies/list-of-australian-universities','Australian Government Study Australia university list','2026-08-13','2026-08-13'),
('University of Southern Queensland','university of southern queensland','University','Approved','Active','https://www.studyaustralia.gov.au/en/plan-your-studies/list-of-australian-universities','Australian Government Study Australia university list','2026-08-13','2026-08-13'),
('University of Sydney','university of sydney','University','Approved','Active','https://www.studyaustralia.gov.au/en/plan-your-studies/list-of-australian-universities','Australian Government Study Australia university list','2026-08-13','2026-08-13'),
('University of Tasmania','university of tasmania','University','Approved','Active','https://www.studyaustralia.gov.au/en/plan-your-studies/list-of-australian-universities','Australian Government Study Australia university list','2026-08-13','2026-08-13'),
('University of Technology Sydney','university of technology sydney','University','Approved','Active','https://www.studyaustralia.gov.au/en/plan-your-studies/list-of-australian-universities','Australian Government Study Australia university list','2026-08-13','2026-08-13'),
('University of the Sunshine Coast','university of the sunshine coast','University','Approved','Active','https://www.studyaustralia.gov.au/en/plan-your-studies/list-of-australian-universities','Australian Government Study Australia university list','2026-08-13','2026-08-13'),
('University of Western Australia','university of western australia','University','Approved','Active','https://www.studyaustralia.gov.au/en/plan-your-studies/list-of-australian-universities','Australian Government Study Australia university list','2026-08-13','2026-08-13'),
('University of Wollongong','university of wollongong','University','Approved','Active','https://www.studyaustralia.gov.au/en/plan-your-studies/list-of-australian-universities','Australian Government Study Australia university list','2026-08-13','2026-08-13'),
('Victoria University','victoria university','University','Approved','Active','https://www.studyaustralia.gov.au/en/plan-your-studies/list-of-australian-universities','Australian Government Study Australia university list','2026-08-13','2026-08-13'),
('Western Sydney University','western sydney university','University','Approved','Active','https://www.studyaustralia.gov.au/en/plan-your-studies/list-of-australian-universities','Australian Government Study Australia university list','2026-08-13','2026-08-13');

INSERT INTO higher_education_providers(
  provider_name,normalized_name,provider_type,review_status,active_status,source_url,evidence,created_at,updated_at
) VALUES
('Monash College','monash college','Pathway Provider','Approved','Active','https://www.monashcollege.edu.au/home','Official provider site states it is a pathway to Monash University','2026-08-13','2026-08-13'),
('UNSW College','unsw college','Pathway Provider','Approved','Active','https://www.unswcollege.edu.au/','Official provider site states it provides pathways to UNSW Sydney','2026-08-13','2026-08-13'),
('UQ College','uq college','Pathway Provider','Approved','Active','https://uqcollege.uq.edu.au/study-options','Official provider site lists international Foundation pathways for UQ entry','2026-08-13','2026-08-13');

INSERT INTO higher_education_provider_relationships(
  university_id,pathway_provider_id,relationship_type,active_status,source_url,evidence,created_at,updated_at
) VALUES
((SELECT id FROM higher_education_providers WHERE normalized_name='monash university'),(SELECT id FROM higher_education_providers WHERE normalized_name='monash college'),'Partner','Active','https://www.monashcollege.edu.au/home','Official pathway relationship','2026-08-13','2026-08-13'),
((SELECT id FROM higher_education_providers WHERE normalized_name='university of new south wales'),(SELECT id FROM higher_education_providers WHERE normalized_name='unsw college'),'Direct','Active','https://www.unswcollege.edu.au/','Official site states UNSW College is wholly owned by UNSW Sydney','2026-08-13','2026-08-13'),
((SELECT id FROM higher_education_providers WHERE normalized_name='university of queensland'),(SELECT id FROM higher_education_providers WHERE normalized_name='uq college'),'Direct','Active','https://uqcollege.uq.edu.au/study-options','Official UQ College pathway source','2026-08-13','2026-08-13');
