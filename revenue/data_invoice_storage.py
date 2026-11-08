"""Explicit, authenticated storage for signed school commission PDFs on Data."""
from contextlib import contextmanager
import ctypes
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import subprocess
import uuid

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from revenue.secure_storage import StorageUnavailable, _open_directory, _read_at, _regular, _path

DATA = Path("/Volumes/Data")
PRODUCTION_ROOT = DATA/"AAIE_CRM/runtime/production/revenue_documents"
TEST_ROOT = DATA/"AAIE_CRM/worktrees/school-commission-contract-v1/.task-tests/data-invoice-storage"
SUFFIX = ".aaie-invoice"
MAGIC = b"AAIE-INVOICE-1\0"
MAX_PDF = 20 * 1024 * 1024


def volume_uuid(fd):
    class Attributes(ctypes.Structure):
        _fields_=[("count",ctypes.c_uint16),("reserved",ctypes.c_uint16),
                  ("common",ctypes.c_uint32),("volume",ctypes.c_uint32),
                  ("directory",ctypes.c_uint32),("file",ctypes.c_uint32),("fork",ctypes.c_uint32)]
    attr=Attributes(5,0,0,0x80040000,0,0,0)
    buffer=ctypes.create_string_buffer(20)
    call=ctypes.CDLL(None,use_errno=True).fgetattrlist
    call.argtypes=[ctypes.c_int,ctypes.POINTER(Attributes),ctypes.c_void_p,ctypes.c_size_t,ctypes.c_ulong]
    if call(fd,ctypes.byref(attr),buffer,len(buffer),0):
        raise StorageUnavailable()
    return str(uuid.UUID(bytes=buffer.raw[4:20]))


class InvoicePDFStore:
    def __init__(self, root, device, inode, key, *, volume=None):
        self.root=_path(root)
        self.device=device
        self.inode=inode
        self.volume=volume
        self.cipher=AESGCM(key)

    @contextmanager
    def directory(self):
        mount=DATA if self.root.is_relative_to(DATA) else None
        with _open_directory(self.root,writable_mount=mount) as (_,fd):
            stat=os.fstat(fd)
            if stat.st_ino!=self.inode:
                raise StorageUnavailable()
            if self.volume:
                if volume_uuid(fd)!=self.volume:
                    raise StorageUnavailable()
            elif stat.st_dev!=self.device:
                raise StorageUnavailable()
            yield fd

    def _read(self, fd, name, digest, size):
        limit=len(MAGIC)+12+size+16
        data=_read_at(fd,name,limit)
        return self._decrypt(data,digest,size)

    def _decrypt(self,data,digest,size):
        if len(data)!=len(MAGIC)+12+size+16 or not data.startswith(MAGIC):
            raise StorageUnavailable()
        offset=len(MAGIC)
        try:
            pdf=self.cipher.decrypt(data[offset:offset+12],data[offset+12:],digest.encode("ascii"))
        except InvalidTag as error:
            raise StorageUnavailable() from error
        if sha256(pdf).hexdigest()!=digest:
            raise StorageUnavailable()
        return pdf

    def read(self, path, digest, size):
        if (not isinstance(digest,str) or not re.fullmatch("[0-9a-f]{64}",digest)
                or type(size) is not int or not 0<size<=MAX_PDF):
            raise StorageUnavailable()
        name=digest+SUFFIX
        if _path(path)!=self.root/name:
            raise StorageUnavailable()
        try:
            with self.directory() as fd:
                return self._read(fd,name,digest,size)
        except OSError as error:
            raise StorageUnavailable() from error

    def write(self, pdf):
        if not isinstance(pdf,bytes) or not 0<len(pdf)<=MAX_PDF:
            raise StorageUnavailable()
        digest=sha256(pdf).hexdigest()
        name=digest+SUFFIX
        nonce=os.urandom(12)
        payload=MAGIC+nonce+self.cipher.encrypt(nonce,pdf,digest.encode("ascii"))
        return self._publish(name,digest,len(pdf),payload)

    def sealed(self,path,digest,size):
        if _path(path)!=self.root/(digest+SUFFIX) or not re.fullmatch("[0-9a-f]{64}",digest) or not 0<size<=MAX_PDF:
            raise StorageUnavailable()
        with self.directory() as fd:
            data=_read_at(fd,digest+SUFFIX,len(MAGIC)+12+size+16)
            self._decrypt(data,digest,size)
            return data

    def restore_sealed(self,data,digest,size):
        if not re.fullmatch("[0-9a-f]{64}",digest) or not 0<size<=MAX_PDF:
            raise StorageUnavailable()
        self._decrypt(data,digest,size)
        return self._publish(digest+SUFFIX,digest,size,data)

    def _publish(self,name,digest,size,payload):
        with self.directory() as fd:
            try:
                self._read(fd,name,digest,size)
                return self.root/name
            except FileNotFoundError:
                pass
            scratch=".pending-"+uuid.uuid4().hex
            handle=os.open(scratch,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=fd)
            try:
                _regular(handle)
                with os.fdopen(handle,"wb",closefd=False) as stream:
                    stream.write(payload)
                    stream.flush()
                    os.fsync(handle)
                try:
                    os.link(scratch,name,src_dir_fd=fd,dst_dir_fd=fd,follow_symlinks=False)
                except FileExistsError:
                    pass
            finally:
                os.close(handle)
                os.unlink(scratch,dir_fd=fd)
            os.fsync(fd)
            self._read(fd,name,digest,size)
        return self.root/name


def configured_invoice_store():
    path=os.environ.get("AAIE_INVOICE_PDF_ROOT")
    if not path:
        return None
    root=_path(path)
    item=os.environ.get("AAIE_INVOICE_PDF_KEYCHAIN_ITEM")
    mode=os.environ.get("AAIE_CRM_ENV")
    if mode=="production":
        expected="AAIE CRM Production Invoice PDF"
        allowed=root==PRODUCTION_ROOT
    else:
        expected="AAIE CRM Data Invoice PDF Rehearsal"
        allowed=mode=="staging" and root!=TEST_ROOT and root.is_relative_to(TEST_ROOT)
    if item!=expected or not allowed:
        raise StorageUnavailable()
    runtime_path=os.environ.get("AAIE_CRM_RUNTIME_CONFIG")
    if runtime_path or mode=="production":
        from runtime_configuration import RuntimeConfiguration
        from tools.runtime_invoice_mail_config import invoice_pdf_environment
        if not runtime_path:
            raise StorageUnavailable()
        try:
            config=RuntimeConfiguration.load(runtime_path,source_root=Path(__file__).resolve().parents[1])
            current=invoice_pdf_environment(config)
        except (OSError,ValueError,RuntimeError) as error:
            raise StorageUnavailable() from error
        if config.mode!=mode or current!={"AAIE_INVOICE_PDF_ROOT":str(root),"AAIE_INVOICE_PDF_KEYCHAIN_ITEM":item}:
            raise StorageUnavailable()
    keychain=Path("/Users/faqiang/Library/Keychains/login.keychain-db")
    command=["/usr/bin/security","find-generic-password","-s",item,"-a","AAIE CRM","-w",str(keychain)]
    try:
        record=json.loads(subprocess.run(command,capture_output=True,check=True,timeout=30).stdout)
        binding=record["directories"][str(root)]
        volume=str(uuid.UUID(record["volume_uuid"]))
        key=bytes.fromhex(record["key"])
        if (len(key)!=32 or type(binding["device"]) is not int or type(binding["inode"]) is not int
                or binding["inode"]<=0):
            raise StorageUnavailable()
        store=InvoicePDFStore(root,binding["device"],binding["inode"],key,volume=volume)
        with store.directory():
            return store
    except (subprocess.SubprocessError,OSError,ValueError,TypeError,KeyError) as error:
        raise StorageUnavailable() from error


def read_invoice_pdf(path,digest,size):
    store=configured_invoice_store()
    if store is None:
        raise StorageUnavailable()
    return store.read(path,digest,size)
