"""Synthetic cases served by real CRM handlers inside existing staging sandbox."""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))


def main():
    from staging_environment import ACTIVE
    if ACTIVE is None or ACTIVE.root != ROOT/'STAGING_DATA' or ACTIVE.admin_port != 18968:
        raise RuntimeError('This harness requires its dedicated staging config')
    import app
    from security import password_hash
    from http.server import ThreadingHTTPServer
    app.ensure_dirs()
    app.db.initialize()
    with app.db.transaction() as connection:
        connection.executescript((ROOT/'migrations/20260805_013_parent_form_access_history.sql').read_text())
    if not app.db.one("SELECT 1 FROM students WHERE student_id='990001'"):
        stamp=app.now_iso()
        app.db.execute("UPDATE users SET password_hash=?,must_change_password=0 WHERE username='admin'", (password_hash('DemoVisa!2026'),))
        app.db.execute("INSERT INTO students(student_id,passport_last_name,passport_first_name,current_status,created_date,updated_at,form_data_json) VALUES(?,?,?,?,?,?,?)", ('990001','Demo','Chen','Active',stamp,stamp,'{}'))
        case_id=app.db.execute("INSERT INTO visa_cases(student_id,visa_type,application_type,status,case_owner,result_status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",('990001','500 Student Visa','New Application','Documents Preparing','演示顾问','Pending',stamp,stamp))
        handler=object.__new__(app.CRMHandler)
        handler.user={'id':1,'username':'admin','display_name':'演示顾问','role_name':'Admin'}
        handler.csrf='fixture-bootstrap'
        case=app.db.one('SELECT * FROM visa_cases WHERE id=?',(case_id,))
        handler.visa_material_initialize_checklist(case)
        app.db.execute("UPDATE visa_checklist_items SET status='uploaded',notes='模拟案件：文件已收到不代表已审核' WHERE visa_case_id=? AND id IN (SELECT id FROM visa_checklist_items WHERE visa_case_id=? ORDER BY id LIMIT 5)",(case_id,case_id))
        from reportlab.pdfgen import canvas
        upload=ACTIVE.root/'demo-passport.pdf'
        document=canvas.Canvas(str(upload));document.drawString(72,720,'Synthetic passport for material UX acceptance');document.save()
    student=app.db.one("SELECT * FROM students WHERE student_id='990001'")
    if not student['folder_path']:
        folder=app.nas.create_student_tree('990001','Chen','Demo')
        app.db.execute("UPDATE students SET folder_path=?,nas_status='ready' WHERE student_id='990001'",(str(folder),))
    app.maybe_run_auto_backup=lambda:None
    server=ThreadingHTTPServer(('127.0.0.1',18968),app.CRMHandler)
    server.service_name='admin'
    print('Material UX staging: http://127.0.0.1:18968/students/990001/visa-cases/1?visa_tab=material_checklist',flush=True)
    server.serve_forever()


if __name__=='__main__': main()
