import unittest
from local_document_lab.regression_report import prediction_is_current


class RegressionReportTests(unittest.TestCase):
    def setUp(self):
        self.prediction={'status':'completed_requires_review','packet_sha256':'packet',
                         'prompt_sha256':'prompt','model_revision':'model','projection_version':1}
        self.run={'prompt_sha256':'prompt','model_revision':'model'}
        self.entry={'inference':{'returncode':0},'projection':{'returncode':0}}

    def test_matching_successful_prediction_is_current(self):
        self.assertTrue(prediction_is_current(self.prediction,self.entry,self.run,'packet'))

    def test_failed_current_run_cannot_reuse_an_old_success_file(self):
        self.entry['inference']['returncode']=124
        self.assertFalse(prediction_is_current(self.prediction,self.entry,self.run,'packet'))

    def test_failed_projection_cannot_count_an_old_projected_file(self):
        self.entry['projection']['returncode']=1
        self.assertFalse(prediction_is_current(self.prediction,self.entry,self.run,'packet'))

    def test_implementation_and_model_inventory_must_match_when_recorded(self):
        self.run.update(implementation_sha256='new-implementation',model_inventory_sha256='new-inventory')
        self.prediction.update(implementation_sha256='old-implementation',model_inventory_sha256='new-inventory')
        self.assertFalse(prediction_is_current(self.prediction,self.entry,self.run,'packet'))
        self.prediction['implementation_sha256']='new-implementation'
        self.prediction['model_inventory_sha256']='old-inventory'
        self.assertFalse(prediction_is_current(self.prediction,self.entry,self.run,'packet'))

    def test_changed_packet_or_prompt_is_not_current(self):
        self.assertFalse(prediction_is_current(self.prediction,self.entry,self.run,'changed'))
        self.prediction['prompt_sha256']='old'
        self.assertFalse(prediction_is_current(self.prediction,self.entry,self.run,'packet'))

if __name__=='__main__':unittest.main()
