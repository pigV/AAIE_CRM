"""Cooperative admission for scheduled snapshots of the formal CRM database.

No SQLite patch and no state creation. Legacy already-running processes still
require operator exclusion before restoration. Direct script imports use only
the containing committed project, never a user package search path.
"""
from pathlib import Path
import sys
_PROJECT=Path(__file__).resolve().parents[1]
if str(_PROJECT) not in sys.path:sys.path.insert(0,str(_PROJECT))
from business_write_gate import Gate,MaintenanceError
FORMAL_DATABASE=Path('/Volumes/Zhang/AAIE CodeX/CRM/database/aaie_crm.db')


def run_snapshot(database,create):
    source=Path(database)
    protected=source.resolve()==FORMAL_DATABASE.resolve()
    if not protected and source.exists() and FORMAL_DATABASE.exists():
        protected=source.samefile(FORMAL_DATABASE)
    if not protected:return create()
    gate=Gate(FORMAL_DATABASE,Path.home()/'Library/Application Support/AAIE CRM/production-services/business-write-gate',required=True)
    if gate.status()['state']!='OPEN':
        return {'was_skipped':True,'skip_reason':'release_read_only','success':True,'failed':False}
    try:
        with gate.operation('business'):
            return create()
    except MaintenanceError:
        return {'was_skipped':True,'skip_reason':'release_read_only','success':True,'failed':False}
