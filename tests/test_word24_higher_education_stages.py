"""University and visa-only services must not inherit school-only gates."""
import unittest
import sqlite3

from tests.test_workflow_resolver import stage_facts
from workflow_resolver import resolve_workflow_stage
from dashboard_confirmed_stages import resolve_dashboard_stage


class HigherEducationServiceStageTests(unittest.TestCase):
    def test_university_intake_does_not_require_a_contract_or_optional_plan(self):
        result = resolve_workflow_stage(stage_facts(2, education_stream='higher_education'))
        self.assertEqual('06_school_application_submission', result.key)

    def test_university_submission_without_optional_plan_has_no_missing_plan_issue(self):
        result = resolve_workflow_stage(stage_facts(2, education_stream='higher_education',
                                                  submitted_application_count=1))
        self.assertEqual('07_school_response', result.key)
        self.assertNotIn('APPLICATION_SUBMITTED_WITHOUT_CONFIRMED_PLAN', result.issue_codes)

    def test_university_offer_advances_without_contract_or_plan(self):
        result = resolve_workflow_stage(stage_facts(2, education_stream='higher_education',
                                                  actionable_offer_count=1))
        self.assertEqual('08_offer_acceptance', result.key)

    def test_university_current_confirmed_coe_does_not_require_return_event(self):
        result = resolve_workflow_stage(stage_facts(2, education_stream='higher_education',
            final_acceptance_selected=True, coe_exists=True, final_school_coe_confirmed=True))
        self.assertEqual('10_visa_submission', result.key)

    def test_school_intake_keeps_contract_gate(self):
        self.assertEqual('02_contract_creation', resolve_workflow_stage(stage_facts(2)).key)

    def test_optional_university_plan_is_not_a_required_dashboard_gate(self):
        self.assertEqual(('06_school_application_submission', ''), resolve_dashboard_stage(
            form_submitted=True, contract='not_required', plan_optional=True))

    def test_visa_only_current_service_ignores_historical_school_waiting(self):
        self.assertEqual(('10_visa_submission', ''), resolve_dashboard_stage(
            submitted=True, school_application_applicable=False))

    def test_visa_only_does_not_resurrect_a_terminal_visa(self):
        for status in ('granted', 'refused', 'withdrawn'):
            with self.subTest(status=status):
                self.assertIsNone(resolve_dashboard_stage(visa=status,
                    school_application_applicable=False))

    def test_only_visa_resolver_ignores_historical_school_plan(self):
        result = resolve_workflow_stage(stage_facts(7, school_application_applicable=False))
        self.assertEqual('10_visa_submission', result.key)

    def test_closed_transfer_resolver_stays_closed(self):
        result = resolve_workflow_stage(stage_facts(9, cycle_type='transfer',
            cycle_status='closed', cycle_outcome='completed'))
        self.assertTrue(result.is_closed)
        self.assertIsNone(result.key)


class CurrentEvidenceScopeTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(':memory:')
        self.addCleanup(self.conn.close)
        self.conn.executescript('''
            CREATE TABLE students(student_id,form_data_json);
            INSERT INTO students VALUES('S','{"application_service":"visa_only"}');
            CREATE TABLE application_cycles(id,student_id,status,cycle_type,sequence_no,
                source_student_case_id,education_stream);
            INSERT INTO application_cycles VALUES(1,'S','active','initial',1,NULL,'higher_education');
            CREATE TABLE cycle_final_acceptances(application_cycle_id,student_id,
                school_name_snapshot,acceptance_status);
            INSERT INTO cycle_final_acceptances VALUES(1,'S','Synthetic University','active');
            CREATE TABLE coe_oshc(application_cycle_id,student_id,document_kind,coe_provider,
                school_name,coe_course,coe_start_date,coe_end_date,file_path);
            INSERT INTO coe_oshc VALUES(1,'S','COE','Synthetic University','Synthetic University',
                'Bachelor of Science','2027-02-01','2099-12-31','/synthetic/coe.pdf');
            CREATE TABLE student_documents(id,student_id,file_path,is_active,document_status,voided_at);
            INSERT INTO student_documents VALUES(1,'S','/synthetic/coe.pdf',1,'active',NULL);
        ''')

    def coe(self):
        from higher_education_workflow_adapter import confirmed_higher_education_coe_cycles
        return confirmed_higher_education_coe_cycles(self.conn, (1,))

    def test_valid_accepted_main_course_coe_advances_without_optional_plan(self):
        self.assertEqual(frozenset({1}), self.coe())
        self.assertEqual(1, self.conn.execute('SELECT count(*) FROM coe_oshc').fetchone()[0])

    def test_coe_without_current_active_acceptance_cannot_advance(self):
        self.conn.execute("UPDATE cycle_final_acceptances SET acceptance_status='withdrawn'")
        self.assertFalse(self.coe())

    def test_old_wrong_student_expired_void_and_bridge_coes_cannot_advance(self):
        for column, value in (('application_cycle_id', 99), ('student_id', 'OTHER'),
                              ('coe_end_date', '2020-01-01'), ('coe_course', 'English Bridging'),
                              ('coe_provider', 'Other University'), ('document_kind', 'OSHC')):
            with self.subTest(column=column):
                self.conn.execute('SAVEPOINT candidate')
                self.conn.execute('UPDATE coe_oshc SET '+column+'=?', (value,))
                self.assertFalse(self.coe())
                self.conn.execute('ROLLBACK TO candidate')
                self.conn.execute('RELEASE candidate')
        self.conn.execute("UPDATE student_documents SET is_active=0,document_status='voided'")
        self.assertFalse(self.coe())

    def test_visa_only_profile_applies_only_to_current_initial_cycle(self):
        from higher_education_workflow_adapter import visa_only_cycle_ids
        self.assertEqual(frozenset({1}), visa_only_cycle_ids(self.conn, (1,)))
        self.conn.execute("INSERT INTO application_cycles VALUES(2,'S','active','progression',2,20,'higher_education')")
        self.assertFalse(visa_only_cycle_ids(self.conn, (1, 2)))
        self.conn.execute("UPDATE application_cycles SET cycle_type='initial',source_student_case_id=NULL WHERE id=2")
        self.assertEqual(frozenset({2}), visa_only_cycle_ids(self.conn, (1, 2)))

    def test_coe_explicit_application_link_must_match_accepted_application(self):
        self.conn.executescript('''
            ALTER TABLE coe_oshc ADD COLUMN id;
            UPDATE coe_oshc SET id=10;
            ALTER TABLE cycle_final_acceptances ADD COLUMN offer_received_event_id;
            UPDATE cycle_final_acceptances SET offer_received_event_id=20;
            CREATE TABLE offer_received_events(id,higher_education_offer_id);
            INSERT INTO offer_received_events VALUES(20,30);
            CREATE TABLE higher_education_offers(id,application_id);
            INSERT INTO higher_education_offers VALUES(30,40);
            CREATE TABLE higher_education_coe_links(coe_oshc_id,application_id);
            INSERT INTO higher_education_coe_links VALUES(10,99);
        ''')
        self.assertFalse(self.coe(), 'Same university is insufficient when the CoE belongs to another application')
        self.conn.execute('UPDATE higher_education_coe_links SET application_id=40')
        self.assertEqual(frozenset({1}), self.coe())

    def legacy_coe(self):
        from higher_education_workflow_adapter import legacy_higher_education_coe_schools
        return legacy_higher_education_coe_schools(self.conn, (1,))

    def prepare_legacy_offer(self):
        self.conn.executescript('''
            DELETE FROM cycle_final_acceptances;
            ALTER TABLE student_documents ADD COLUMN uploaded_at;
            ALTER TABLE student_documents ADD COLUMN document_type;
            CREATE TABLE offers(id,student_id,application_cycle_id,document_type,school_name,
                offer_status,offer_file_path,signed_offer_file_path,offer_expiry_date);
            INSERT INTO offers VALUES(4,'S',1,'SignedOffer','QUT','accepted',
                '/synthetic/signed.pdf','/synthetic/signed.pdf','2099-01-01');
            INSERT INTO student_documents VALUES(2,'S','/synthetic/signed.pdf',1,'active',NULL,
                '2026-11-01','SignedOffer');
            UPDATE coe_oshc SET coe_provider='Queensland University of Technology (QUT) [00213J]',
                school_name='Queensland University of Technology (QUT) [00213J]';
        ''')

    def test_owned_legacy_signed_offer_and_qut_alias_main_coe_are_real_acceptance(self):
        self.prepare_legacy_offer()
        self.assertEqual({1: 'QUT'}, self.legacy_coe())

    def test_legacy_does_not_override_active_or_same_school_withdrawal(self):
        self.prepare_legacy_offer()
        self.conn.execute("INSERT INTO cycle_final_acceptances VALUES(1,'S','Other University','active')")
        self.assertFalse(self.legacy_coe())
        self.conn.execute("UPDATE cycle_final_acceptances SET school_name_snapshot='QUT',acceptance_status='withdrawn'")
        self.assertFalse(self.legacy_coe())

    def test_unrelated_withdrawn_school_does_not_block_new_verified_school(self):
        self.prepare_legacy_offer()
        self.conn.execute("INSERT INTO cycle_final_acceptances VALUES(1,'S','Unrelated High School','withdrawn')")
        self.assertEqual({1:'QUT'},self.legacy_coe())

    def test_legacy_package_keeps_earlier_pathway_start_and_later_main_coe(self):
        self.prepare_legacy_offer()
        self.conn.execute('ALTER TABLE offers ADD COLUMN course_start_date')
        self.conn.execute("UPDATE offers SET course_start_date='2027-02-22'")
        self.conn.execute("UPDATE coe_oshc SET coe_start_date='2028-02-21'")
        self.assertEqual({1:'QUT'},self.legacy_coe())

    def test_actual_signed_document_date_is_distinct_from_late_import_date(self):
        self.prepare_legacy_offer()
        self.conn.execute('ALTER TABLE student_documents ADD COLUMN document_date')
        self.conn.execute("UPDATE student_documents SET document_date='2026-01-01' WHERE id=2")
        self.conn.execute("UPDATE offers SET offer_expiry_date='2026-06-01'")
        self.assertEqual({1:'QUT'},self.legacy_coe())
        self.conn.execute("UPDATE student_documents SET document_date='2026-07-01' WHERE id=2")
        self.assertFalse(self.legacy_coe())

    def test_unsigned_document_cannot_prove_acceptance(self):
        self.prepare_legacy_offer()
        self.conn.execute("UPDATE student_documents SET document_type='Offer' WHERE id=2")
        self.assertFalse(self.legacy_coe())

    def test_import_timestamp_does_not_prove_signature_before_expiry(self):
        self.prepare_legacy_offer()
        self.conn.execute("UPDATE student_documents SET uploaded_at='2026-01-01' WHERE id=2")
        self.conn.execute("UPDATE offers SET offer_expiry_date='2026-06-01'")
        self.assertFalse(self.legacy_coe())

    def test_legacy_offer_needs_current_scope_live_signature_and_exact_provider_alias(self):
        self.prepare_legacy_offer()
        for table, column, value in (
            ('offers', 'application_cycle_id', 99), ('offers', 'student_id', 'OTHER'),
            ('offers', 'offer_status', 'withdrawn'), ('offers', 'school_name', 'QUT College'),
            ('offers', 'school_name', 'York Business Institute'),
            ('coe_oshc', 'coe_course', 'English for Academic Purposes'),
            ('coe_oshc', 'coe_end_date', '2020-01-01'),
            ('student_documents', 'is_active', 0),
        ):
            with self.subTest(table=table,column=column):
                self.conn.execute('SAVEPOINT candidate')
                self.conn.execute('UPDATE '+table+' SET '+column+'=?', (value,))
                self.assertFalse(self.legacy_coe())
                self.conn.execute('ROLLBACK TO candidate')
                self.conn.execute('RELEASE candidate')

    def test_two_different_universities_do_not_match_a_shared_parenthetical_city(self):
        self.conn.execute("UPDATE cycle_final_acceptances SET school_name_snapshot='Charles Sturt University (Sydney)'")
        self.conn.execute("UPDATE coe_oshc SET coe_provider='University of Wollongong (Sydney)'")
        self.assertFalse(self.coe())
