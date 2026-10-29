"""Request-owned payment file cleanup on the actual fixture filesystem."""
from contextlib import contextmanager
import os
from pathlib import Path
import unittest
from unittest.mock import patch
from tests import test_transfer_payment_proof_append as fixtures


class PaymentCleanupTests(unittest.TestCase):
    setUp = fixtures.TransferPaymentProofAppendTests.setUp
    append = fixtures.TransferPaymentProofAppendTests.append

    def documents(self):
        return fixtures.document_entries(self.folder)

    def fail_after_write(self, callback=lambda: None):
        @contextmanager
        def transaction():
            self.conn.execute('BEGIN IMMEDIATE')
            try:
                yield self.conn
                callback()
                raise RuntimeError('synthetic commit failure')
            except Exception:
                self.conn.rollback()
                raise
        self.db.transaction = transaction

    def original_path(self):
        return next(p for p in self.documents() if p.name.startswith('Payment_'))

    def assert_absent_rows(self):
        self.assertEqual(self.conn.execute('SELECT count(*) FROM student_documents').fetchone()[0], 0)
        self.assertIsNone(self.conn.execute('SELECT receipt_file_path FROM payments').fetchone()[0])
        self.assertEqual(self.conn.execute('SELECT count(*) FROM audit_logs').fetchone()[0], 0)

    def test_success_file_and_database_remain_consistent(self):
        result = self.append()
        self.assertEqual(Path(result.receipt_path).read_bytes(), b'%PDF-1.4\nsynthetic proof')
        self.assertEqual(self.conn.execute('SELECT file_path FROM student_documents WHERE id=?', (result.document_id,)).fetchone()[0], result.receipt_path)
        self.assertEqual(self.documents(), [Path(result.receipt_path)])

    def test_real_filesystem_commit_failure_removes_only_created_pdf(self):
        old = self.folder/'user-original.pdf'; old.write_bytes(b'original')
        before = list(self.conn.iterdump()); self.commit_failure = True
        with self.assertRaisesRegex(RuntimeError, 'synthetic commit failure'):
            self.append()
        self.assertEqual(list(self.conn.iterdump()), before)
        self.assertEqual(self.documents(), [old]); self.assertEqual(old.read_bytes(), b'original')

    def test_collision_does_not_claim_or_remove_existing_file(self):
        old=self.folder/'Payment_5_proof_collision.pdf'; old.write_bytes(b'foreign')
        with patch.object(self.subject.uuid, 'uuid4', return_value=type('ID',(),{'hex':'collision'})()):
            with self.assertRaises(FileExistsError): self.append()
        self.assertEqual(old.read_bytes(), b'foreign'); self.assert_absent_rows()

    def test_foreign_replacement_before_cleanup_is_preserved(self):
        paths=[]
        def replace():
            p=self.original_path(); paths.append(p); p.unlink(); p.write_bytes(b'foreign replacement')
        self.fail_after_write(replace)
        with self.assertRaises(self.subject.PaymentProofCleanupError) as raised: self.append()
        self.assertEqual(raised.exception.code, 'ownership_changed')
        self.assertEqual(paths[0].read_bytes(), b'foreign replacement'); self.assert_absent_rows()
        self.assertIn('synthetic commit failure', str(raised.exception.__context__))

    def test_symlink_replacement_never_removes_target(self):
        target=self.root/'user-original.pdf'; target.write_bytes(b'original'); paths=[]
        def replace():
            p=self.original_path(); paths.append(p); p.unlink(); p.symlink_to(target)
        self.fail_after_write(replace)
        with self.assertRaises(self.subject.PaymentProofCleanupError): self.append()
        self.assertTrue(paths[0].is_symlink()); self.assertEqual(target.read_bytes(), b'original')
        self.assert_absent_rows()

    def test_original_path_replacement_during_capture_not_deleted(self):
        rename=self.subject._capture_created_file; paths=[]
        def racing(src,directory_fd,capture_fd):
            if src.startswith('Payment_'):
                p=self.folder/src; paths.append(p); p.unlink(); p.write_bytes(b'late foreign')
            return rename(src,directory_fd,capture_fd)
        self.commit_failure=True
        with patch.object(self.subject,'_capture_created_file',side_effect=racing):
            with self.assertRaises(self.subject.PaymentProofCleanupError) as raised: self.append()
        self.assertFalse(paths[0].exists()); self.assertEqual(Path(raised.exception.retained_path).read_bytes(),b'late foreign'); self.assert_absent_rows()

    def test_captured_foreign_and_new_public_occupant_both_preserved(self):
        rename=self.subject._capture_created_file; paths=[]
        def racing(src,directory_fd,capture_fd):
            if src.startswith('Payment_'):
                p=self.folder/src; paths.append(p); p.unlink(); p.write_bytes(b'captured foreign')
                result=rename(src,directory_fd,capture_fd); p.write_bytes(b'new occupant'); return result
            return rename(src,directory_fd,capture_fd)
        self.commit_failure=True
        with patch.object(self.subject,'_capture_created_file',side_effect=racing):
            with self.assertRaises(self.subject.PaymentProofCleanupError) as raised: self.append()
        self.assertEqual(raised.exception.code,'ownership_changed')
        self.assertEqual(paths[0].read_bytes(),b'new occupant')
        self.assertEqual(Path(raised.exception.retained_path).read_bytes(),b'captured foreign')
        self.assert_absent_rows()

    def test_unlink_failure_is_explicit_with_original_error_and_retained_file(self):
        unlink=os.unlink
        def denied(name,*args,**kwargs):
            if str(name) == 'document': raise PermissionError('synthetic unlink denied')
            return unlink(name,*args,**kwargs)
        self.commit_failure=True
        with patch.object(self.subject.os,'unlink',side_effect=denied):
            with self.assertRaises(self.subject.PaymentProofCleanupError) as raised: self.append()
        self.assertEqual(raised.exception.code,'remove_failed')
        self.assertTrue(Path(raised.exception.retained_path).is_file()); self.assert_absent_rows()
        self.assertIsInstance(raised.exception.__cause__,PermissionError)
        self.assertIn('synthetic commit failure', str(raised.exception.__cause__.__context__))

    def test_capture_failure_is_explicit_and_preserves_file(self):
        self.commit_failure=True
        with patch.object(self.subject,'_capture_created_file',side_effect=OSError('not supported')):
            with self.assertRaises(self.subject.PaymentProofCleanupError) as raised: self.append()
        self.assertEqual(raised.exception.code,'capture_failed')
        self.assertTrue(Path(raised.exception.retained_path).exists()); self.assert_absent_rows()

    def test_fstat_failure_before_capture_is_explicit_and_preserves_path(self):
        self.commit_failure=True
        with patch.object(self.subject.os,'fstat',side_effect=OSError('synthetic fstat failure')):
            with self.assertRaises(self.subject.PaymentProofCleanupError) as raised: self.append()
        self.assertEqual(raised.exception.code,'ownership_check_failed')
        self.assertTrue(Path(raised.exception.retained_path).exists()); self.assert_absent_rows()

    def test_write_fsync_failure_removes_partial_owned_file(self):
        with patch.object(self.subject.os,'fsync',side_effect=OSError('synthetic fsync failure')):
            with self.assertRaisesRegex(OSError,'synthetic fsync failure'): self.append()
        self.assertEqual(self.documents(),[]); self.assert_absent_rows()

    def test_file_already_missing_at_rollback_is_idempotent(self):
        self.fail_after_write(lambda:self.original_path().unlink())
        with self.assertRaisesRegex(RuntimeError,'synthetic commit failure'): self.append()
        self.assertEqual(self.documents(),[]); self.assert_absent_rows()

    def test_repeated_cleanup_is_safe_with_same_creation_handle(self):
        directory=os.open(self.folder,os.O_RDONLY|os.O_DIRECTORY)
        fd=os.open('owned.pdf',os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600,dir_fd=directory)
        try:
            os.write(fd,b'own');os.fsync(fd)
            self.subject._cleanup_created_file(self.folder/'owned.pdf',fd,directory)
            self.subject._cleanup_created_file(self.folder/'owned.pdf',fd,directory)
            self.assertEqual(self.documents(),[])
        finally: os.close(fd);os.close(directory)

    def test_atomic_capture_collision_preserves_preexisting_quarantine(self):
        old=self.folder/'.payment-rollback-collision';old.write_bytes(b'other request')
        ids=iter(['newproof','collision'])
        self.commit_failure=True
        with patch.object(self.subject.uuid,'uuid4',side_effect=lambda:type('ID',(),{'hex':next(ids)})()):
            with self.assertRaises(self.subject.PaymentProofCleanupError) as raised:self.append()
        self.assertEqual(old.read_bytes(),b'other request')
        self.assertEqual(raised.exception.code,'capture_failed');self.assert_absent_rows()

    def test_missing_capture_destination_is_not_mistaken_for_removed_source(self):
        self.commit_failure=True
        with patch.object(self.subject,'_capture_created_file',side_effect=FileNotFoundError('capture missing')):
            with self.assertRaises(self.subject.PaymentProofCleanupError) as raised:self.append()
        self.assertEqual(raised.exception.code,'capture_failed')
        self.assertTrue(Path(raised.exception.retained_path).exists());self.assert_absent_rows()

    def test_fstat_enoent_is_not_mistaken_for_removed_source(self):
        self.commit_failure=True
        with patch.object(self.subject.os,'fstat',side_effect=FileNotFoundError('held fd inspection failed')):
            with self.assertRaises(self.subject.PaymentProofCleanupError) as raised:self.append()
        self.assertEqual(raised.exception.code,'ownership_check_failed')
        self.assertTrue(Path(raised.exception.retained_path).exists());self.assert_absent_rows()

    def test_empty_created_file_is_cleaned_if_stream_creation_fails(self):
        with patch.object(self.subject.os,'dup',side_effect=OSError('stream unavailable')):
            with self.assertRaisesRegex(OSError,'stream unavailable'):self.append()
        self.assertEqual(self.documents(),[]);self.assert_absent_rows()

    def test_empty_directory_removal_failure_reports_no_retained_document(self):
        rmdir=os.rmdir;self.commit_failure=True
        def denied(name,*args,**kwargs):
            if str(name).startswith('.payment-rollback-'):raise PermissionError('rmdir denied')
            return rmdir(name,*args,**kwargs)
        with patch.object(self.subject.os,'rmdir',side_effect=denied):
            with self.assertRaises(self.subject.PaymentProofCleanupError) as raised:self.append()
        self.assertEqual(raised.exception.code,'empty_cleanup_directory_remove_failed')
        self.assertIsNone(raised.exception.retained_path)
        self.assertEqual(fixtures.document_entries(Path(raised.exception.cleanup_directory)),[])
        self.assert_absent_rows()

    def test_creation_and_directory_handles_close_after_cleanup_failure(self):
        opened=[];original_open=os.open;self.commit_failure=True
        def capture(*args,**kwargs):
            fd=original_open(*args,**kwargs);opened.append(fd);return fd
        with patch.object(self.subject.os,'open',side_effect=capture):
            with patch.object(self.subject,'_capture_created_file',side_effect=OSError('capture denied')):
                with self.assertRaises(self.subject.PaymentProofCleanupError):self.append()
        for fd in set(opened):
            with self.assertRaises(OSError):os.fstat(fd)

    def test_metadata_filter_does_not_hide_unrelated_dot_file(self):
        foreign=self.folder/'._not-metadata.pdf';foreign.write_bytes(b'\x00\x05\x16\x07\x00\x02\x00\x00')
        self.assertEqual(self.documents(),[foreign])
