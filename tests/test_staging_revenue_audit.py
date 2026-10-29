import os
from pathlib import Path
import unittest
from unittest.mock import patch
from staging_environment import Staging, IsolationError
ROOT=Path('/Volumes/Zhang/AAIE CodeX/CRM/.worktrees/agent-reach-trends-single-platform-20260912/STAGING_DATA')
class StagingRevenueAuditTests(unittest.TestCase):
    def setUp(self):
        self.stage=Staging(ROOT,8880,8881,'aaie_staging_')
        self.fd=os.open(ROOT/'revenue_documents',os.O_RDONLY|os.O_DIRECTORY)
        self.addCleanup(os.close,self.fd)
        with patch('sys.addaudithook') as capture, patch.object(Staging,'assert_sandbox'):
            self.stage.install_audit()
        self.hook=capture.call_args.args[0]
    def test_descriptor_relative_directory_is_inside_staging(self):
        self.hook('os.mkdir',('new-synthetic-dir',0o700,self.fd))
    def test_descriptor_relative_link_checks_both_endpoints(self):
        self.hook('os.link',('new-source','new-destination',self.fd,self.fd))
    def test_descriptor_relative_escape_is_denied(self):
        with self.assertRaises(IsolationError):self.hook('os.mkdir',('../../../outside',0o700,self.fd))
    def test_absolute_outside_open_is_denied(self):
        with self.assertRaises(IsolationError):self.hook('open',(str(ROOT.parent/'outside'),None,os.O_WRONLY|os.O_CREAT))
    def test_invalid_descriptor_fails_closed(self):
        with self.assertRaises(IsolationError):self.hook('os.mkdir',('synthetic',0o700,123456789))
    def test_relative_write_without_nofollow_is_denied(self):
        with self.assertRaises(IsolationError):self.hook('open',('synthetic',None,os.O_WRONLY|os.O_CREAT))
