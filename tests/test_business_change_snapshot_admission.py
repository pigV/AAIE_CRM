from concurrent.futures import ThreadPoolExecutor
import threading
import unittest
from unittest.mock import patch

from business_change_sync import BusinessChangeSyncMixin


class SnapshotAdmissionTests(unittest.TestCase):
    def test_excess_full_reads_return_busy_without_queueing_and_capacity_recovers(self):
        entered=threading.Barrier(2);release=threading.Event()
        class Handler(BusinessChangeSyncMixin):
            user={'id':1}
            def _business_change_snapshot(self,method):
                entered.wait(timeout=3);release.wait(timeout=3);return 200,{'ok':True}
            def json_response(self,data,status=200):return status,data
        with ThreadPoolExecutor(max_workers=10) as pool:
            first=[pool.submit(Handler().business_change_snapshot,'GET')]
            try:
                entered.wait(timeout=3)
                overflow=[pool.submit(Handler().business_change_snapshot,'GET') for _ in range(8)]
                for future in overflow:
                    status,data=future.result(timeout=1)
                    self.assertEqual(503,status);self.assertEqual('snapshot_busy',data['code'])
            finally:release.set()
            for future in first:self.assertEqual(200,future.result(timeout=3)[0])

    def test_exception_releases_the_slot(self):
        class Handler(BusinessChangeSyncMixin):
            user={'id':1}
            def _business_change_snapshot(self,method):raise ValueError('native error')
        for _ in range(4):
            with self.assertRaisesRegex(ValueError,'native error'):Handler().business_change_snapshot('GET')
