"""Regressions for the Visa Management Word test report and confirmed date meanings."""
import json
import unittest
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch

from tests import test_visa_level2 as fixtures
from visa_document_lifecycle import case_projection
from visa_processing_dates import parse_processing_dates, processing_summary
from visa_processing_dates import correct_processing_dates
from visa_workspace_candidate import document_workspace_html, VisaWorkspaceCandidateMixin


class VisaReportExtractionTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.VisaLevel2Tests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.c = self.fixture.c

    def test_health_uses_sent_on_and_never_appointment_or_completion(self):
        text = ('Health Examinations\nSent on: 1 September 2026\n'
                'Appointment: 3 September 2026\nExamination completed: 4 September 2026')
        fields = parse_processing_dates(text, today='2026-09-08')['fields']
        self.assertEqual(fields['health_exam_date']['value'], '2026-09-01')
        self.assertEqual(fields['health_exam_date']['source'], 'sent_on')
        self.assertEqual(fields['medical_completed_date']['value'], '')

    def test_health_without_sent_on_does_not_use_completed_date(self):
        for label in ('Examination completed', 'Date', 'Date of letter', 'Generated'):
            with self.subTest(label=label):
                fields = parse_processing_dates(
                    'Health Examinations\n' + label + ': 4 September 2026', today='2026-09-08'
                )['fields']
                self.assertEqual(fields['health_exam_date']['value'], '')

    def test_obsolete_cached_health_completion_is_not_a_letter_date(self):
        doc = self.fixture.doc(category='health_examination')
        legacy = {'fields': {key: {'value': '', 'status': 'not_detected', 'source': ''}
                            for key in ('acknowledge_sent_on', 'health_exam_date', 'grant_sent_on', 'medical_completed_date')}}
        legacy['fields']['health_exam_date'] = {
            'value': '2026-09-04', 'status': 'detected', 'source': 'completed'}
        self.c.execute('INSERT INTO visa_processing_date_extractions(visa_case_id,document_id,proposed_json,actor_user_id,occurred_at) VALUES(1,?,?,1,?)',
                       (doc['id'], json.dumps(legacy), fixtures.STAMP))
        self.assertEqual(processing_summary(self.c, 1)['fields']['health_exam_date']['value'], '')

    def test_active_grant_projects_all_three_official_values_without_writes(self):
        self.fixture.doc(category='grant_letter')
        text = ('IMMI Grant Notification\nSent on: 6 September 2026\n'
                'Date of Grant: 5 September 2026\nLength of stay: Until 15 March 2029')
        before = self.c.total_changes
        projection = case_projection(self.c, 1, read_text=lambda doc: text)
        self.assertEqual(projection['official_decision']['grant_date'], '2026-09-05')
        self.assertEqual(projection['official_decision']['expiry_date'], '2029-03-15')
        self.assertEqual(projection['official_decision']['length_of_stay'], 'Until 15 March 2029')
        self.assertEqual(projection['processing']['fields']['grant_sent_on']['value'], '2026-09-06')
        self.assertEqual(self.c.total_changes, before)

    def test_case_panel_displays_unconfirmed_grant_expiry_and_length(self):
        self.fixture.doc(category='grant_letter')
        text = ('IMMI Grant Notification\nDate of Grant: 5 September 2026\n'
                'Length of stay: 3 years\nVisa expiry date: 5 September 2029')
        @contextmanager
        def transaction():
            self.c.execute('SAVEPOINT test_route')
            try:
                yield self.c
                self.c.execute('RELEASE test_route')
            except Exception:
                self.c.execute('ROLLBACK TO test_route')
                self.c.execute('RELEASE test_route')
                raise
        db = SimpleNamespace(
            transaction=transaction,
            one=lambda q, args=(): self.c.execute(q, args).fetchone(),
            all=lambda q, args=(): self.c.execute(q, args).fetchall(),
        )
        handler = VisaWorkspaceCandidateMixin()
        handler.csrf = 'csrf'
        handler._visa_runtime = lambda: {'db': db}
        handler._visa_read_text = lambda case, doc: text
        html = handler.visa_processing_panel_html(dict(self.c.execute('SELECT * FROM visa_cases WHERE id=1').fetchone()))
        self.assertIn('Visa expiry date: 2029-09-05', html)
        self.assertIn('Length of Stay: 3 years', html)

    def test_result_history_is_hidden_without_removing_result_or_audit_input(self):
        result = {'effective_result': 'Granted', 'authority': 'auto', 'history': [
            {'occurred_at': '2026-09-06', 'event_kind': 'detected', 'effective_result': 'Granted'}]}
        html = document_workspace_html(1, 'csrf', [], [], [], result)
        self.assertNotIn('Result History', html)
        self.assertIn('Current Result', html)
        self.assertIn('Save correction', html)
        self.assertEqual(len(result['history']), 1)

    def replaced_grant_projection(self, retained_values):
        from visa_documents_candidate import save_extraction, set_document_archived
        old = self.fixture.doc(category='grant_letter')
        extraction = save_extraction(self.c, 1, old['id'], text=
            'IMMI Grant Notification\nDate of Grant: 1 September 2026\nLength of stay: 3 years\nVisa expiry date: 1 September 2029', **self.fixture.actor)
        self.c.execute("INSERT INTO visa_case_confirmed_decisions(visa_case_id,extraction_id,decision_type,decision_date,confirmed_json,confirmed_by,confirmed_at) VALUES(1,?,'grant','2026-09-01',?,1,?)",
                       (extraction['id'], json.dumps(retained_values), fixtures.STAMP))
        set_document_archived(self.c, 1, old['id'], archived=True, **self.fixture.actor)
        self.fixture.doc(name='replacement.txt', data=b'new grant', category='grant_letter')
        return case_projection(self.c, 1, read_text=lambda doc:
            'IMMI Grant Notification\nDate of Grant: 5 September 2026\nLength of stay: 5 years\nVisa expiry date: 5 September 2031')

    def test_replacement_grant_releases_old_automatic_expiry_and_length(self):
        projection = self.replaced_grant_projection({'length_of_stay': '3 years', 'expiry_date': '2029-09-01'})
        self.assertEqual(projection['official_decision']['length_of_stay'], '5 years')
        self.assertEqual(projection['official_decision']['expiry_date'], '2031-09-05')

    def test_replacement_grant_preserves_staff_corrected_official_values(self):
        projection = self.replaced_grant_projection({'length_of_stay': '4 years', 'expiry_date': '2030-09-01'})
        self.assertEqual(projection['official_decision']['length_of_stay'], '4 years')
        self.assertEqual(projection['official_decision']['expiry_date'], '2030-09-01')


class VisaLodgementDateCorrectionTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.VisaLevel2Tests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.c = self.fixture.c
        for definition in ('actual_lodgement_date TEXT', 'lodgement_date TEXT',
                           'lodgement_recorded_at TEXT', 'lodged_by_user_id INTEGER',
                           'lodged_by_name TEXT'):
            self.c.execute('ALTER TABLE visa_cases ADD COLUMN ' + definition)
        self.c.execute("UPDATE visa_cases SET actual_lodgement_date='2026-09-01',lodgement_date='2026-09-01',lodgement_recorded_at='2026-09-01T12:00:00',lodged_by_user_id=1,lodged_by_name='Original Staff',status='Granted' WHERE id=1")

    def correct(self, value):
        return correct_processing_dates(self.c, 1, {'acknowledge_sent_on': value},
            actor_user_id=1, occurred_at='2026-09-08T12:00:00', today='2026-09-08')

    def test_explicit_date_correction_synchronizes_dates_and_preserves_original_evidence(self):
        self.correct('2026-09-02')
        case = dict(self.c.execute('SELECT * FROM visa_cases WHERE id=1').fetchone())
        self.assertEqual(case['actual_lodgement_date'], '2026-09-02')
        self.assertEqual(case['lodgement_date'], '2026-09-02')
        self.assertEqual(case['status'], 'Granted')
        self.assertEqual(case['lodged_by_name'], 'Original Staff')
        self.assertEqual(case['lodgement_recorded_at'], '2026-09-01T12:00:00')
        revision = dict(self.c.execute('SELECT * FROM visa_processing_date_revisions').fetchone())
        self.assertEqual(json.loads(revision['before_json'])['lodgement_evidence']['actual_lodgement_date'], '2026-09-01')
        self.assertEqual(revision['value'], '2026-09-02')
        self.assertEqual(revision['actor_user_id'], 1)

    def test_reading_new_acknowledgement_never_rewrites_recorded_lodgement(self):
        self.fixture.doc(category='immi_acknowledgment')
        before = self.c.total_changes
        projection = case_projection(self.c, 1, read_text=lambda doc:
            'Acknowledgement of application\nSent on: 2 September 2026')
        self.assertEqual(projection['processing']['fields']['acknowledge_sent_on']['value'], '2026-09-02')
        self.assertEqual(self.c.execute('SELECT actual_lodgement_date FROM visa_cases WHERE id=1').fetchone()[0], '2026-09-01')
        self.assertEqual(self.c.total_changes, before)

    def test_recorded_submission_date_cannot_be_cleared(self):
        with self.assertRaises(ValueError):
            self.correct('')
        self.assertEqual(self.c.execute('SELECT COUNT(*) FROM visa_processing_date_revisions').fetchone()[0], 0)

    def test_failed_revision_rolls_back_date_synchronization(self):
        self.c.executescript("""CREATE TRIGGER reject_revision BEFORE INSERT ON visa_processing_date_revisions
            BEGIN SELECT RAISE(ABORT,'synthetic revision failure'); END;""")
        import sqlite3
        with self.assertRaises(sqlite3.IntegrityError):
            self.correct('2026-09-02')
        self.assertEqual(self.c.execute('SELECT actual_lodgement_date FROM visa_cases WHERE id=1').fetchone()[0], '2026-09-01')

    def test_unsubmitted_case_date_correction_does_not_mark_submitted(self):
        self.c.execute("UPDATE visa_cases SET actual_lodgement_date=NULL,lodgement_date=NULL,lodgement_recorded_at=NULL,status='Draft' WHERE id=1")
        self.correct('2026-09-02')
        case = dict(self.c.execute('SELECT * FROM visa_cases WHERE id=1').fetchone())
        self.assertIsNone(case['lodgement_recorded_at'])
        self.assertIsNone(case['actual_lodgement_date'])
        self.assertEqual(case['status'], 'Draft')

    @contextmanager
    def handler(self):
        import app
        @contextmanager
        def transaction():
            self.c.execute('SAVEPOINT test_route')
            try:
                yield self.c
                self.c.execute('RELEASE test_route')
            except Exception:
                self.c.execute('ROLLBACK TO test_route')
                self.c.execute('RELEASE test_route')
                raise
        db = SimpleNamespace(
            transaction=transaction,
            one=lambda q, args=(): self.c.execute(q, args).fetchone(),
        )
        self.c.execute("UPDATE visa_cases SET student_id='S1' WHERE id=1")
        handler = object.__new__(app.CRMHandler)
        handler.user = {'id': 1, 'role_name': 'Admin', 'permissions_json': '["*"]'}
        handler.csrf = 'csrf'
        handler.form = {'action': 'correct-lodgement-date', 'actual_lodgement_date': '2026-09-02'}
        handler.flash_redirect = lambda url, text, kind='ok': (text, kind)
        handler._visa_read_text = lambda case, doc: 'Acknowledgement of application\nSent on: 2 September 2026'
        with patch.object(app, 'db', db):
            yield handler

    def test_existing_submitted_case_has_prefilled_explicit_correction_control(self):
        # A previous staff confirmation must not hide a newer letter proposal.
        self.correct('2026-09-01')
        self.fixture.doc(category='immi_acknowledgment')
        with self.handler() as handler:
            html = handler.visa_application_submitted_html('S1', self.c.execute('SELECT * FROM visa_cases WHERE id=1').fetchone())
        self.assertIn('更正日期', html)
        self.assertIn('name="action" value="correct-lodgement-date"', html)
        self.assertIn('value="2026-09-02"', html)
        self.assertIn('Original Staff', html)
        self.assertEqual(self.c.execute('SELECT actual_lodgement_date FROM visa_cases WHERE id=1').fetchone()[0], '2026-09-01')

    def test_explicit_correction_route_updates_dates_without_replaying_submission(self):
        with self.handler() as handler:
            response = handler.student_visa_case_lodgement_submit('POST', 'S1', '1')
        self.assertEqual(response[1], 'ok')
        self.assertEqual(self.c.execute('SELECT actual_lodgement_date FROM visa_cases WHERE id=1').fetchone()[0], '2026-09-02')
        self.assertEqual(self.c.execute('SELECT status FROM visa_cases WHERE id=1').fetchone()[0], 'Granted')

    def test_normal_submission_replay_still_preserves_original_date(self):
        with self.handler() as handler:
            handler.form.pop('action')
            response = handler.student_visa_case_lodgement_submit('POST', 'S1', '1')
        self.assertEqual(response[1], 'error')
        self.assertEqual(self.c.execute('SELECT actual_lodgement_date FROM visa_cases WHERE id=1').fetchone()[0], '2026-09-01')

    def test_invalid_initial_submission_shows_error_and_rolls_back(self):
        self.c.execute("UPDATE visa_cases SET actual_lodgement_date=NULL,lodgement_date=NULL,lodgement_recorded_at=NULL,status='Draft' WHERE id=1")
        with self.handler() as handler:
            handler.form = {'actual_lodgement_date': '9999-01-01'}
            response = handler.student_visa_case_lodgement_submit('POST', 'S1', '1')
        self.assertEqual(response[1], 'error')
        case = dict(self.c.execute('SELECT * FROM visa_cases WHERE id=1').fetchone())
        self.assertIsNone(case['lodgement_recorded_at'])
        self.assertIsNone(case['actual_lodgement_date'])
        self.assertEqual(case['status'], 'Draft')

    def test_same_request_reuses_letter_reads_but_next_request_refreshes(self):
        import app
        self.fixture.doc(category='immi_acknowledgment')
        state = {}
        reads = []
        letter_date = ['2 September 2026']
        def read(case, document):
            reads.append(document['id'])
            return 'Acknowledgement of application\nSent on: ' + letter_date[0]
        with self.handler() as handler:
            app.db.all = lambda q, args=(): self.c.execute(q, args).fetchall()
            app.db._request_state = lambda: state
            handler._visa_read_text = read
            case = self.c.execute('SELECT * FROM visa_cases WHERE id=1').fetchone()
            handler.visa_processing_panel_html(case)
            handler.visa_application_submitted_html('S1', case)
            self.assertEqual(len(reads), 1)
            state.clear()  # A fresh database request scope has no cached projection.
            letter_date[0] = '3 September 2026'
            html = handler.visa_application_submitted_html('S1', case)
            self.assertEqual(len(reads), 2)
            self.assertIn('value="2026-09-03"', html)
