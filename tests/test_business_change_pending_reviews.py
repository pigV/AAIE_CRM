import sqlite3
import unittest


class PendingReviewSummaryTests(unittest.TestCase):
    def setUp(self):
        self.conn=sqlite3.connect(':memory:')
        self.addCleanup(self.conn.close)
        self.conn.executescript('''
            CREATE TABLE upload_sessions(id INTEGER PRIMARY KEY,session_token TEXT,student_id TEXT,module_key TEXT,uploaded_at TEXT);
            CREATE TABLE upload_review_items(id INTEGER PRIMARY KEY,upload_session_id INTEGER,student_id TEXT,module_key TEXT,status TEXT,error_message TEXT);
            INSERT INTO upload_sessions VALUES(1,'private-app','S1','Applications','2026-11-19'),(2,'private-visa','S1','Visa','2026-11-19'),(3,'foreign','S2','Applications','2026-11-19');
            INSERT INTO upload_review_items VALUES(1,1,'S1','Applications','NeedsReview',NULL),(2,1,'S1','Applications','NeedsReview','retry'),(3,1,'S1','Applications','Confirmed',NULL),(4,2,'S1','Visa','NeedsReview',NULL),(5,3,'S2','Applications','NeedsReview',NULL),(6,1,'S2','Applications','NeedsReview',NULL);
        ''')

    def read(self,can_view=lambda module:module=='Applications'):
        from business_changes.pending_reviews import pending_reviews
        return pending_reviews(self.conn,'S1',can_view=can_view)

    def test_counts_are_owned_pending_facts_filtered_by_module_permission(self):
        self.conn.execute('PRAGMA query_only=ON')
        result=self.read()
        self.assertEqual((1,2,1),(result['batches'],result['pending'],result['failed']))
        self.assertEqual('private-app',result['rows'][0]['token'])
        self.assertNotIn('private-visa',str(result))
        self.assertNotIn('foreign',str(result))

    def test_no_module_rights_reveals_no_counts_or_private_links(self):
        self.assertEqual({'batches':0,'pending':0,'failed':0,'rows':[]},self.read(lambda module:False))

    def test_finished_batch_disappears_and_retry_error_is_not_exposed(self):
        self.conn.execute("UPDATE upload_review_items SET status='Cancelled' WHERE upload_session_id=1")
        self.assertEqual(0,self.read()['pending'])
        self.assertEqual(1,self.read(lambda module:True)['pending'])
        self.assertNotIn('retry',str(self.read(lambda module:True)))

    def test_bounded_links_preserve_full_totals(self):
        for index in range(4,19):
            self.conn.execute("INSERT INTO upload_sessions VALUES(?,?,'S1','Applications','2026-11-20')",(index,'token-'+str(index)))
            self.conn.execute("INSERT INTO upload_review_items VALUES(?,?,'S1','Applications','NeedsReview',NULL)",(index+10,index))
        result=self.read()
        self.assertEqual(16,result['batches']);self.assertEqual(17,result['pending'])
        self.assertEqual(10,len(result['rows']))
