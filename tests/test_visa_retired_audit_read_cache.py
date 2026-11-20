import json,sqlite3,unittest
from contextlib import closing
from pathlib import Path
import sys
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from snapshot_read_cache import read_cache_scope
from visa_documents_candidate import _retired_extraction_ids

class RetiredAuditReadBudgetTests(unittest.TestCase):
 def setUp(self):
  self.c=sqlite3.connect(':memory:');self.c.row_factory=sqlite3.Row;self.addCleanup(self.c.close)
  self.c.execute('CREATE TABLE audit_logs(record_id TEXT,module_name TEXT,action_type TEXT,new_value TEXT)')
  self.add('11',1);self.add('22',2);self.add('bad-foreign-record-id',3);self.add('44','1');self.add('55',None)
  self.add('66',1,module='other');self.add('77',1,action='other');self.c.commit();self.trace=[];self.c.set_trace_callback(self.trace.append)
 def add(self,record_id,case,module='visa_management',action='visa_letter_extraction_retired'):
  self.c.execute('INSERT INTO audit_logs VALUES(?,?,?,?)',(record_id,module,action,json.dumps({'visa_case_id':case})))
 def begin(self):self.c.execute('PRAGMA query_only=ON');self.c.execute('BEGIN')
 def scans(self):return [s for s in self.trace if 'FROM audit_logs' in s]
 def test_one_snapshot_loads_retirement_audit_once_for_multiple_cases(self):
  self.begin()
  with read_cache_scope(self.c):
   self.assertEqual({11},_retired_extraction_ids(self.c,1));self.assertEqual({22},_retired_extraction_ids(self.c,2));self.assertEqual({11},_retired_extraction_ids(self.c,1))
  self.assertEqual(1,len(self.scans()),'all cases in one readonly snapshot must share one audit scan')
 def test_case_types_foreign_bad_ids_and_null_are_isolated(self):
  self.begin()
  with read_cache_scope(self.c):
   self.assertEqual({11},_retired_extraction_ids(self.c,1));self.assertEqual({44},_retired_extraction_ids(self.c,'1'));self.assertEqual(set(),_retired_extraction_ids(self.c,None))
   with self.assertRaises(ValueError):_retired_extraction_ids(self.c,3)
 def test_mutable_result_cannot_poison_another_read(self):
  self.begin()
  with read_cache_scope(self.c):
   value=_retired_extraction_ids(self.c,1);value.clear();self.assertEqual({11},_retired_extraction_ids(self.c,1))
 def test_new_transaction_observes_new_retirement(self):
  self.begin()
  with read_cache_scope(self.c):self.assertEqual({11},_retired_extraction_ids(self.c,1))
  self.c.rollback();self.c.execute('PRAGMA query_only=OFF');self.add('88',1);self.c.commit();self.begin()
  with read_cache_scope(self.c):self.assertEqual({11,88},_retired_extraction_ids(self.c,1))
 def test_writer_reads_new_retirements_without_snapshot_cache(self):
  self.assertEqual({11},_retired_extraction_ids(self.c,1));self.add('88',1);self.assertEqual({11,88},_retired_extraction_ids(self.c,1));self.assertEqual(2,len(self.scans()))
 def test_readonly_connection_without_cache_scope_does_not_reuse_results(self):
  self.begin();self.assertEqual({11},_retired_extraction_ids(self.c,1));self.assertEqual({11},_retired_extraction_ids(self.c,1));self.assertEqual(2,len(self.scans()))

if __name__=='__main__':unittest.main()
