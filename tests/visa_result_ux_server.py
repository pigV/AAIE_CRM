"""Real result handlers over freshly built synthetic schema and task-only storage."""
from pathlib import Path
import sys
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))


def main():
    from staging_environment import ACTIVE
    if ACTIVE is None or ACTIVE.root!=ROOT/'STAGING_DATA' or ACTIVE.admin_port!=18970:
        raise RuntimeError('Dedicated result staging required')
    import app
    from security import password_hash
    from http.server import ThreadingHTTPServer
    app.ensure_dirs();app.db.initialize()
    if not app.db.one("SELECT 1 FROM sqlite_master WHERE name='visa_applicants'"):
        import re
        from tests.visa_test_schema import prepare_production_visa_schema
        connection=app.db.connect()
        try:
            # Build only empty prerequisite tables from their owning SQL sources.
            prerequisites=[('20260820_027_transfer_contract_ownership.sql','application_cycles_rebuild_027','application_cycles'),
                           ('20260816_023_dashboard_canonical_states.sql','visa_decision_events','visa_decision_events')]
            for filename,source_name,target_name in prerequisites:
                if not connection.execute("SELECT 1 FROM sqlite_master WHERE name=?",(target_name,)).fetchone():
                    source=(ROOT/'migrations'/filename).read_text()
                    ddl=re.search(r'CREATE TABLE '+source_name+r' \([\s\S]*?\n\);',source).group(0)
                    connection.executescript(ddl.replace(source_name,target_name,1))
            visa_sql=(ROOT/'migrations/20260907_035_visa_management_authorities.sql').read_text()
            trigger=re.search(r'CREATE TRIGGER visa_decision_events_ownership_insert\b[\s\S]*?\nEND;',visa_sql).group(0)
            connection.executescript(trigger)
            prepare_production_visa_schema(connection)
            connection.execute('PRAGMA foreign_keys=OFF')
            connection.executescript(visa_sql)
            connection.commit();connection.execute('PRAGMA foreign_keys=ON')
        finally:connection.close()
    with app.db.transaction() as connection:
        app.db.ensure_columns(connection,'visa_case_document_links',{'document_category':'TEXT','document_type':'TEXT','used_in_this_case':'INTEGER DEFAULT 1'})
    migrations=[('parent_form_access_bindings','20260805_012_parent_form_secure_draft.sql'),
                ('parent_form_access_events','20260805_013_parent_form_access_history.sql'),
                ('visa_document_metadata','20260909_037_visa_document_metadata.sql'),
                ('visa_processing_date_revisions','20260909_038_visa_processing_dates.sql'),
                ('visa_result_status_current','20260914_039_visa_result_status_authority.sql'),
                ('visa_only_material_uploads','20261117_049_visa_only_materials.sql')]
    for table,filename in migrations:
        if not app.db.one("SELECT 1 FROM sqlite_master WHERE name=?",(table,)):
            with app.db.transaction() as connection:connection.executescript((ROOT/'migrations'/filename).read_text())
    if not app.db.one("SELECT 1 FROM students WHERE student_id='990002'"):
        stamp=app.now_iso();folder=app.nas.create_student_tree('990002','Result','Demo')
        app.db.execute("UPDATE users SET password_hash=?,must_change_password=0 WHERE username='admin'",(password_hash('DemoResult!2026'),))
        app.db.execute("INSERT INTO students(student_id,passport_last_name,passport_first_name,current_status,created_date,updated_at,form_data_json,folder_path,nas_status) VALUES(?,?,?,?,?,?,?,?,?)",('990002','Demo','Result','Active',stamp,stamp,'{}',str(folder),'ready'))
        specs=[(1,'Documents Preparing','Pending',None,None),(2,'Lodged','Pending',stamp,stamp[:10]),(3,'S56 Requested','Pending',stamp,stamp[:10]),(4,'Granted','Granted',stamp,stamp[:10])]
        for cid,status,result,recorded,date in specs:
            app.db.execute("INSERT INTO visa_cases(id,student_id,visa_case_name,visa_type,applicant_name,application_type,status,case_owner,result_status,created_at,updated_at,lodgement_recorded_at,actual_lodgement_date,lodged_by_name) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",(cid,'990002',f'Result Demo {cid}','500 Student Visa','Result Demo','New Application',status,'演示顾问',result,stamp,stamp,recorded,date,'演示顾问' if recorded else None))
        aid=app.db.execute("INSERT INTO visa_applicants(name,email,created_by,created_at) VALUES(?,?,?,?)",('Independent Result Demo','',1,stamp))
        app.db.execute("INSERT INTO visa_cases(id,student_id,applicant_id,visa_case_name,visa_type,applicant_name,application_type,status,result_status,created_at,updated_at) VALUES(5,NULL,?,?,?,?,?,?,?,?,?)",(aid,'Independent Result Demo','500 Student Visa','Independent Result Demo','New Application','Draft','Pending',stamp,stamp))
        handler=object.__new__(app.CRMHandler);handler.user={'id':1,'username':'admin','role_name':'Admin'};handler.csrf='fixture';handler.query={}
        for cid in range(1,6):handler.visa_material_initialize_checklist(app.db.one('SELECT * FROM visa_cases WHERE id=?',(cid,)))
        from reportlab.pdfgen import canvas
        pdf=canvas.Canvas(str(ACTIVE.root/'demo-receipt.pdf'));pdf.drawString(72,720,'Synthetic IMMI application payment receipt');pdf.save()
    from reportlab.pdfgen import canvas
    retry_pdf=ACTIVE.root/'demo-receipt-v2.pdf'
    if not retry_pdf.exists():
        pdf=canvas.Canvas(str(retry_pdf));pdf.drawString(72,720,'Synthetic IMMI application payment receipt - second test version');pdf.save()
    app.maybe_run_auto_backup=lambda:None
    server=ThreadingHTTPServer(('127.0.0.1',18970),app.CRMHandler);server.service_name='admin'
    print('Result UX staging listening on 127.0.0.1:18970; synthetic cases 1-5',flush=True)
    server.serve_forever()


if __name__=='__main__':main()
