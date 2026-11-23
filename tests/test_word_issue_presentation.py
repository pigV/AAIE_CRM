import unittest

import application_withdrawal
import student_transfer_summary


class WordIssuePresentationTests(unittest.TestCase):
    def test_withdrawn_students_group_by_id_preserving_every_cycle(self):
        records = [dict(student_id='A', cycle_id=3, sequence_no=2),
                   dict(student_id='B', cycle_id=2, sequence_no=1),
                   dict(student_id='A', cycle_id=1, sequence_no=1)]
        groups = application_withdrawal.group_withdrawn_students(records)
        self.assertEqual(['A', 'B'], [g['student_id'] for g in groups])
        self.assertEqual([3, 1], [r['cycle_id'] for r in groups[0]['history']])
        self.assertEqual(3, groups[0]['latest']['cycle_id'])
        self.assertEqual(3, len(records))

    def test_transfer_header_badge_covers_ready_contract_but_not_ordinary_student(self):
        Summary = student_transfer_summary.StudentTransferSummary
        for state in ('contract_pending', 'ready', 'active', 'history'):
            badge = student_transfer_summary.render_student_transfer_badge(Summary('S1', state, '转学'))
            self.assertIn('转学', badge)
        self.assertEqual('', student_transfer_summary.render_student_transfer_badge(None))
        self.assertEqual('', student_transfer_summary.render_student_transfer_badge(Summary('S1', 'none', '暂无转学')))
