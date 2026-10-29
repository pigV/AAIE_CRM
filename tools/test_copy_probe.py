import json
import ctypes
import os
from pathlib import Path
import socket
import subprocess
import sys
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from staging_environment import ACTIVE as c
assert c is not None
c.assert_sandbox()
result={}
def denied_open(path,flags):
    lib=ctypes.CDLL("/usr/lib/libSystem.B.dylib",use_errno=True)
    lib.open.argtypes=[ctypes.c_char_p,ctypes.c_int]
    lib.open.restype=ctypes.c_int
    fd=lib.open(str(path).encode(),flags)
    if fd==-1:return ctypes.get_errno() in (1,13)
    os.close(fd);return False
result["source_write_denied"]=denied_open(ROOT/"app.py",os.O_WRONLY)
result["production_read_denied"]=denied_open("/Volumes/Zhang/AAIE CodeX/CRM/AGENTS.md",os.O_RDONLY)
result["production_write_denied"]=denied_open("/Volumes/Zhang/AAIE CodeX/CRM/AGENTS.md",os.O_WRONLY)
try:
    with socket.socket() as s:s.connect(("127.0.0.1",9))
except PermissionError:result["external_connection_denied"]=True
except OSError:result["external_connection_denied"]=False
else:result["external_connection_denied"]=False
try:
    with socket.socket() as s:s.bind(("0.0.0.0",18882))
except PermissionError:result["wrong_bind_denied"]=True
else:result["wrong_bind_denied"]=False
try:subprocess.run(["/usr/bin/true"],check=True)
except PermissionError:result["child_process_denied"]=True
else:result["child_process_denied"]=False
p=c.check(c.root/"tmp/isolation-probe.txt")
p.write_text("local writes are allowed")
result["local_write_allowed"]=p.read_text()=="local writes are allowed"
print(json.dumps(result))
