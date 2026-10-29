-- Forward-only R5 delta. Migration 020 remains immutable.

CREATE TABLE progression_pathway_acceptances (
  id INTEGER PRIMARY KEY,
  application_cycle_id INTEGER NOT NULL UNIQUE
    REFERENCES application_cycles(id) ON DELETE RESTRICT,
  student_case_id INTEGER NOT NULL UNIQUE
    REFERENCES student_cases(id) ON DELETE RESTRICT,
  progression_case_id INTEGER NOT NULL UNIQUE
    REFERENCES progression_cases(case_id) ON DELETE RESTRICT,
  student_id TEXT NOT NULL REFERENCES students(student_id) ON DELETE RESTRICT,
  pathway_uid TEXT NOT NULL UNIQUE CHECK(length(trim(pathway_uid)) > 0),
  status TEXT NOT NULL DEFAULT 'assembling'
    CHECK(status IN ('assembling','accepted','cancelled')),
  accepted_at TEXT,
  created_by INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
  created_at TEXT NOT NULL,
  updated_by INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
  updated_at TEXT NOT NULL,
  CHECK(student_case_id = progression_case_id),
  CHECK(
    (status='assembling' AND accepted_at IS NULL)
    OR (status='accepted' AND accepted_at IS NOT NULL)
    OR status='cancelled'
  )
);

CREATE TRIGGER progression_pathway_acceptances_ownership_insert
BEFORE INSERT ON progression_pathway_acceptances
WHEN NOT EXISTS (
  SELECT 1
    FROM application_cycles AS cycle
    JOIN student_cases AS student_case ON student_case.id=NEW.student_case_id
    JOIN progression_cases AS progression_case
      ON progression_case.case_id=NEW.progression_case_id
   WHERE cycle.id=NEW.application_cycle_id
     AND cycle.student_id=NEW.student_id
     AND cycle.cycle_type='progression'
     AND cycle.source_student_case_id=NEW.student_case_id
     AND student_case.student_id=NEW.student_id
     AND student_case.case_type='progression'
     AND progression_case.case_id=student_case.id
)
BEGIN
  SELECT RAISE(ABORT,'progression pathway ownership mismatch');
END;

CREATE TRIGGER progression_pathway_acceptances_ownership_update
BEFORE UPDATE OF application_cycle_id,student_case_id,progression_case_id,student_id
ON progression_pathway_acceptances
WHEN NOT EXISTS (
  SELECT 1
    FROM application_cycles AS cycle
    JOIN student_cases AS student_case ON student_case.id=NEW.student_case_id
    JOIN progression_cases AS progression_case
      ON progression_case.case_id=NEW.progression_case_id
   WHERE cycle.id=NEW.application_cycle_id
     AND cycle.student_id=NEW.student_id
     AND cycle.cycle_type='progression'
     AND cycle.source_student_case_id=NEW.student_case_id
     AND student_case.student_id=NEW.student_id
     AND student_case.case_type='progression'
     AND progression_case.case_id=student_case.id
)
BEGIN
  SELECT RAISE(ABORT,'progression pathway ownership mismatch');
END;

CREATE TRIGGER progression_pathway_acceptances_no_delete
BEFORE DELETE ON progression_pathway_acceptances
BEGIN
  SELECT RAISE(ABORT,'progression pathway acceptances are retained permanently');
END;

CREATE TABLE progression_pathway_components (
  id INTEGER PRIMARY KEY,
  pathway_id INTEGER NOT NULL
    REFERENCES progression_pathway_acceptances(id) ON DELETE RESTRICT,
  progression_target_application_id INTEGER NOT NULL UNIQUE
    REFERENCES progression_target_applications(id) ON DELETE RESTRICT,
  component_order INTEGER NOT NULL CHECK(component_order BETWEEN 1 AND 3),
  component_role TEXT NOT NULL DEFAULT 'required'
    CHECK(component_role IN ('required','optional')),
  active_final INTEGER NOT NULL DEFAULT 1 CHECK(active_final IN (0,1)),
  canonical_application_kind TEXT
    CHECK(canonical_application_kind IS NULL
      OR canonical_application_kind IN ('public','private')),
  canonical_application_id INTEGER,
  accepted_progression_offer_id INTEGER UNIQUE
    REFERENCES progression_case_offers(id) ON DELETE RESTRICT,
  canonical_offer_event_id INTEGER UNIQUE
    REFERENCES offer_received_events(id) ON DELETE RESTRICT,
  signed_offer_document_link_id INTEGER UNIQUE
    REFERENCES progression_document_links(id) ON DELETE RESTRICT,
  payment_document_link_id INTEGER UNIQUE
    REFERENCES progression_document_links(id) ON DELETE RESTRICT,
  coe_document_link_id INTEGER UNIQUE
    REFERENCES progression_document_links(id) ON DELETE RESTRICT,
  oshc_document_link_id INTEGER UNIQUE
    REFERENCES progression_document_links(id) ON DELETE RESTRICT,
  coe_record_id INTEGER UNIQUE REFERENCES coe_oshc(id) ON DELETE RESTRICT,
  oshc_record_id INTEGER UNIQUE REFERENCES coe_oshc(id) ON DELETE RESTRICT,
  oshc_context_kind TEXT
    CHECK(oshc_context_kind IS NULL
      OR oshc_context_kind IN ('lifecycle_owned','carried_forward')),
  created_by INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
  created_at TEXT NOT NULL,
  updated_by INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
  updated_at TEXT NOT NULL,
  CHECK(
    (canonical_application_kind IS NULL AND canonical_application_id IS NULL)
    OR
    (canonical_application_kind IS NOT NULL AND canonical_application_id IS NOT NULL)
  ),
  CHECK(
    (oshc_record_id IS NULL AND oshc_context_kind IS NULL)
    OR (oshc_record_id IS NOT NULL AND oshc_context_kind IS NOT NULL)
  )
);

CREATE UNIQUE INDEX uq_progression_pathway_components_active_order
ON progression_pathway_components(pathway_id,component_order)
WHERE active_final=1;

CREATE INDEX ix_progression_pathway_components_pathway
ON progression_pathway_components(pathway_id,active_final,component_order);

CREATE UNIQUE INDEX uq_progression_pathway_components_active_application
ON progression_pathway_components(
  pathway_id,canonical_application_kind,canonical_application_id
)
WHERE active_final=1 AND canonical_application_id IS NOT NULL;

CREATE TRIGGER progression_pathway_components_ownership_insert
BEFORE INSERT ON progression_pathway_components
WHEN NOT EXISTS (
  SELECT 1
    FROM progression_pathway_acceptances AS pathway
    JOIN progression_target_applications AS target
      ON target.id=NEW.progression_target_application_id
   WHERE pathway.id=NEW.pathway_id
     AND target.case_id=pathway.progression_case_id
     AND target.is_final=1
)
OR (
  NEW.accepted_progression_offer_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM progression_case_offers AS offer
     WHERE offer.id=NEW.accepted_progression_offer_id
       AND offer.target_application_id=NEW.progression_target_application_id
       AND offer.offer_status='accepted'
  )
)
OR (
  NEW.canonical_application_kind='private' AND NOT EXISTS (
    SELECT 1
      FROM progression_pathway_acceptances AS pathway
      JOIN private_school_applications AS application
        ON application.id=NEW.canonical_application_id
     WHERE pathway.id=NEW.pathway_id
       AND application.application_cycle_id=pathway.application_cycle_id
       AND application.student_id=pathway.student_id
  )
)
OR (
  NEW.canonical_application_kind='public' AND NOT EXISTS (
    SELECT 1
      FROM progression_pathway_acceptances AS pathway
      JOIN public_application_preferences AS preference
        ON preference.id=NEW.canonical_application_id
      JOIN public_application_groups AS group_row
        ON group_row.id=preference.public_application_group_id
     WHERE pathway.id=NEW.pathway_id
       AND group_row.application_cycle_id=pathway.application_cycle_id
       AND group_row.student_id=pathway.student_id
  )
)
BEGIN
  SELECT RAISE(ABORT,'progression pathway component ownership mismatch');
END;

CREATE TRIGGER progression_pathway_components_ownership_update
BEFORE UPDATE OF pathway_id,progression_target_application_id,
  canonical_application_kind,canonical_application_id,
  accepted_progression_offer_id,canonical_offer_event_id
ON progression_pathway_components
WHEN NOT EXISTS (
  SELECT 1
    FROM progression_pathway_acceptances AS pathway
    JOIN progression_target_applications AS target
      ON target.id=NEW.progression_target_application_id
   WHERE pathway.id=NEW.pathway_id
     AND target.case_id=pathway.progression_case_id
     AND target.is_final=1
)
OR (
  NEW.accepted_progression_offer_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM progression_case_offers AS offer
     WHERE offer.id=NEW.accepted_progression_offer_id
       AND offer.target_application_id=NEW.progression_target_application_id
       AND offer.offer_status='accepted'
  )
)
OR (
  NEW.canonical_application_kind='private' AND NOT EXISTS (
    SELECT 1
      FROM progression_pathway_acceptances AS pathway
      JOIN private_school_applications AS application
        ON application.id=NEW.canonical_application_id
     WHERE pathway.id=NEW.pathway_id
       AND application.application_cycle_id=pathway.application_cycle_id
       AND application.student_id=pathway.student_id
  )
)
OR (
  NEW.canonical_application_kind='public' AND NOT EXISTS (
    SELECT 1
      FROM progression_pathway_acceptances AS pathway
      JOIN public_application_preferences AS preference
        ON preference.id=NEW.canonical_application_id
      JOIN public_application_groups AS group_row
        ON group_row.id=preference.public_application_group_id
     WHERE pathway.id=NEW.pathway_id
       AND group_row.application_cycle_id=pathway.application_cycle_id
       AND group_row.student_id=pathway.student_id
  )
)
BEGIN
  SELECT RAISE(ABORT,'progression pathway component ownership mismatch');
END;

CREATE TRIGGER progression_pathway_components_no_delete
BEFORE DELETE ON progression_pathway_components
BEGIN
  SELECT RAISE(ABORT,'progression pathway components are retained permanently');
END;

CREATE TRIGGER progression_pathway_acceptances_ready_update
BEFORE UPDATE OF status ON progression_pathway_acceptances
WHEN NEW.status='accepted' AND (
  (SELECT count(*) FROM progression_pathway_components
    WHERE pathway_id=NEW.id AND active_final=1) NOT BETWEEN 1 AND 3
  OR EXISTS (
    SELECT 1 FROM progression_pathway_components
     WHERE pathway_id=NEW.id AND active_final=1
       AND (
         accepted_progression_offer_id IS NULL
         OR canonical_offer_event_id IS NULL
       )
  )
)
BEGIN
  SELECT RAISE(ABORT,'progression pathway acceptance requires every component offer');
END;

DROP INDEX uq_progression_case_offers_one_accepted;

CREATE UNIQUE INDEX uq_progression_case_offers_one_accepted_per_target
ON progression_case_offers(target_application_id)
WHERE offer_status='accepted' AND target_application_id IS NOT NULL;

CREATE TRIGGER progression_case_offers_accepted_target_insert
BEFORE INSERT ON progression_case_offers
WHEN NEW.offer_status='accepted' AND NEW.target_application_id IS NULL
BEGIN
  SELECT RAISE(ABORT,'accepted Progression Offer requires exact target');
END;

CREATE TRIGGER progression_case_offers_accepted_target_update
BEFORE UPDATE OF offer_status,target_application_id ON progression_case_offers
WHEN NEW.offer_status='accepted' AND NEW.target_application_id IS NULL
BEGIN
  SELECT RAISE(ABORT,'accepted Progression Offer requires exact target');
END;
