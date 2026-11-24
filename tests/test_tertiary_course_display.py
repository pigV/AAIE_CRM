import unittest
from tests import test_reviewed_tertiary_course as fixture


class TertiaryCourseDisplayTests(unittest.TestCase):
    setUp=fixture.ReviewedTertiaryCourseTests.setUp
    tearDown=fixture.ReviewedTertiaryCourseTests.tearDown
    confirm=fixture.ReviewedTertiaryCourseTests.confirm

    def bachelor(self):
        self.bachelor_file=self.coe.with_name('bachelor-offer.pdf');self.bachelor_file.write_bytes(b'original conditional bachelor')
        self.c.execute("INSERT INTO offers VALUES(21,'S',10,'Example University','received','Offer',?,NULL,'2026-09-16',NULL,'2028-02-21',NULL)",(str(self.bachelor_file),))
        self.c.execute("INSERT INTO student_documents VALUES(42,'S',?,'Offer',1,'active',NULL,NULL)",(str(self.bachelor_file),))

    def conditional(self):
        from tertiary_course_display import confirm_conditional_offer
        return confirm_conditional_offer(self.c,student_id='S',offer_id=21,actor_id=1,
            course_name='Bachelor Course',course_cricos='115421A',condition='Complete the Foundation course',
            reason='User verified original conditional offer',stamp='2026-11-25T00:00:00+11:00')

    def test_separate_accepted_foundation_from_conditional_bachelor(self):
        from tertiary_course_display import offer_course_annotations
        self.c.execute('BEGIN');self.confirm();self.bachelor();self.assertTrue(self.conditional())
        before=self.c.total_changes;self.assertFalse(self.conditional());self.assertEqual(before,self.c.total_changes)
        rows=offer_course_annotations(self.c,'S',[20,21])
        self.assertIn('已接受课程',rows[20]);self.assertIn('Foundation Course',rows[20])
        self.assertIn('有条件录取',rows[21]);self.assertIn('尚未接受',rows[21]);self.assertIn('Bachelor Course',rows[21])
        self.assertEqual('received',self.c.execute('SELECT offer_status FROM offers WHERE id=21').fetchone()[0])
        self.assertFalse(offer_course_annotations(self.c,'OTHER',[20,21]))

    def test_conditional_note_disappears_on_acceptance_or_source_change(self):
        from tertiary_course_display import offer_course_annotations
        self.c.execute('BEGIN');self.bachelor();self.conditional()
        self.c.execute("UPDATE offers SET offer_status='accepted' WHERE id=21")
        self.assertNotIn(21,offer_course_annotations(self.c,'S',[21]))
        self.c.execute("UPDATE offers SET offer_status='received' WHERE id=21")
        self.bachelor_file.write_bytes(b'changed conditional original')
        self.assertNotIn(21,offer_course_annotations(self.c,'S',[21]))

    def test_unaccepted_note_does_not_grant_acceptance_or_allow_inactive_actor(self):
        self.c.execute('BEGIN');self.bachelor();self.c.execute('UPDATE users SET is_active=0')
        with self.assertRaises(PermissionError):self.conditional()
        self.assertEqual(0,self.c.execute('SELECT count(*) FROM audit_logs').fetchone()[0])
