import unittest
from local_document_lab.import_cache import decode_structure, cached_structure
class ImportCacheTests(unittest.TestCase):
 def test_export_cache_reconstructs_sets_and_prefix_without_changing_names(self):
  payload=[{'backends':['torch'],'modules':{'modeling_sample':['SampleModel']}}]
  result=decode_structure(payload)
  self.assertEqual(result,{frozenset({'torch'}):{'modeling_sample':{'SampleModel'}}})
  self.assertEqual(cached_structure({'models/sample':payload},'models/sample','models.sample'),{frozenset({'torch'}):{'models.sample.modeling_sample':{'SampleModel'}}})
 def test_uncached_package_falls_back_instead_of_inventing_exports(self):
  self.assertIsNone(cached_structure({},'models/other',None))
