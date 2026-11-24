import unittest
from datetime import date

from final_school_coe import eligibility_reason


class ExactTradingProviderTests(unittest.TestCase):
    def row(self, provider, course='Junior Secondary Studies Years 7-10 [097391M]', school='Citipointe Christian College'):
        return dict(document_id=3212, file_path='/existing/coe.pdf',
                    coe_start_date='2028-01-20', coe_end_date='2030-11-30',
                    coe_course=course, coe_provider=provider, school_name=provider,
                    school_name_snapshot=school, state='QLD', sector_key='private')

    def reason(self, **values):
        return eligibility_reason(self.row(**values), today=date(2026, 11, 24))

    def test_exact_name_in_explicit_trading_as_list(self):
        self.assertEqual('', self.reason(provider='Christian Outreach Centre [00996F] (trading as: Citipointe Christian College and Citipointe Christian College International)'))

    def test_existing_truncated_trading_as_still_has_complete_exact_first_name(self):
        self.assertEqual('', self.reason(provider='Christian Outreach Centre [00996F] (trading as: Citipointe Christian College and Citipointe Christian College'))

    def test_shared_city_or_school_substring_is_not_identity(self):
        for provider in ('Citipointe Christian College International',
                         'Christian Outreach Centre (trading as: Citipointe Christian College International)',
                         'Sydney Other School (trading as: Sydney Academy)',
                         'Citipointe Christian College and Another School'):
            with self.subTest(provider=provider):
                self.assertIn('不匹配', self.reason(provider=provider))

    def test_preparation_program_never_counts_as_main_course(self):
        self.assertIn('语言或衔接', self.reason(provider='Citipointe Christian College',
            course='Secondary School Preparation Program (Elementary to Upper Intermediate) [043173A]'))

    def test_regular_secondary_course_remains_eligible(self):
        self.assertEqual('', self.reason(provider='Citipointe Christian College', course='Senior Secondary Years 11-12 [010045D]'))


if __name__ == '__main__':
    unittest.main()
