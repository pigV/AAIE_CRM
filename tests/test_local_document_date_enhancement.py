import unittest
from local_document_lab.evidence import dates_in, validate_result
from local_document_lab.project import project_program_fields

class DateEnhancementTests(unittest.TestCase):
    def test_month_first_date_with_comma_is_grounded(self):
        self.assertEqual(dates_in("July 19, 2027 to December 17, 2027"), {"2027-07-19", "2027-12-17"})
    def test_day_month_comma_year_is_grounded(self):
        self.assertEqual(dates_in("19 July, 2027 to 17 December, 2027"), {"2027-07-19", "2027-12-17"})
    def test_invalid_or_incomplete_dates_are_not_invented(self):
        self.assertEqual(dates_in("February 30, 2027; July 2027"), set())
    def test_unique_matching_language_program_uses_grounded_summary_dates(self):
        b={"id":"a","page":1,"text":"Sample IEC July 19, 2027 December 17, 2027","bbox":[]}
        c=lambda v:{"value":v,"block_id":"a","quote":b["text"]}
        r=validate_result({"fields":{"language_school":c("Sample IEC"),"language_start_date":c("2027-07-19"),"language_end_date":c("2027-12-17")},"programs":[{"kind":"language","school":c("Sample IEC")}]},[b])
        self.assertEqual(project_program_fields(r)["programs"][0]["fields"].get("course_start_date",{}).get("value"),"2027-07-19")
    def test_mismatching_school_does_not_receive_summary_dates(self):
        b={"id":"a","page":1,"text":"Sample IEC Other IEC 19-Jul-2027","bbox":[]}
        c=lambda v:{"value":v,"block_id":"a","quote":b["text"]}
        r=validate_result({"fields":{"language_school":c("Other IEC"),"language_start_date":c("2027-07-19")},"programs":[{"kind":"language","school":c("Sample IEC")}]},[b])
        self.assertFalse(project_program_fields(r)["programs"][0]["fields"].get("course_start_date",{}).get("value"))
