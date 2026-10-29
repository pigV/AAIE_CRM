"""Independent Visa applicants. No automatic Student creation or conversion."""
from contextlib import contextmanager

@contextmanager
def transaction(conn):
    conn.execute('SAVEPOINT visa_applicant')
    try:
        yield
        conn.execute('RELEASE visa_applicant')
    except Exception:
        conn.execute('ROLLBACK TO visa_applicant')
        conn.execute('RELEASE visa_applicant')
        raise

def create_applicant(conn, *, name, email, actor_id, stamp):
    name=str(name or '').strip()
    if not name or len(name)>200 or len(str(email or ''))>320:
        raise ValueError('Applicant name is required (maximum 200 characters).')
    with transaction(conn):
        return conn.execute('INSERT INTO visa_applicants(name,email,created_by,created_at) VALUES(?,?,?,?)',(name,str(email or '').strip(),actor_id,stamp)).lastrowid

def create_case(conn, *, applicant_id, subclass, actor_id, stamp):
    if str(subclass) not in {'500','590','485'}:
        raise ValueError('Select Visa subclass 500, 590 or 485.')
    applicant=conn.execute('SELECT * FROM visa_applicants WHERE id=?',(applicant_id,)).fetchone()
    if not applicant: raise ValueError('Visa Applicant does not exist.')
    with transaction(conn):
        return conn.execute('''INSERT INTO visa_cases(applicant_id,student_id,visa_case_name,visa_type,visa_subclass,case_type,applicant_name,main_applicant,case_owner,created_at,updated_at)
            VALUES(?,NULL,?,?,?,?,?,?,?,?,?)''',(applicant_id,f"{applicant['name']} — Visa {subclass}",subclass,subclass,'Guardian Visa' if subclass=='590' else 'Student Visa',applicant['name'],applicant['name'],str(actor_id),stamp,stamp)).lastrowid

def link_student(conn, *, applicant_id, student_id, actor_id, stamp):
    if not conn.execute('SELECT 1 FROM visa_applicants WHERE id=?',(applicant_id,)).fetchone(): raise ValueError('Visa Applicant does not exist.')
    if not conn.execute('SELECT 1 FROM students WHERE student_id=?',(student_id,)).fetchone(): raise ValueError('Student Profile does not exist. Create it separately first.')
    with transaction(conn):
        old=conn.execute('SELECT * FROM visa_applicant_student_links WHERE applicant_id=?',(applicant_id,)).fetchone()
        if old:
            if old['student_id']==student_id: return old['id']
            raise ValueError('Applicant is already linked to a different Student. Link replacement requires a reviewed correction.')
        return conn.execute('INSERT INTO visa_applicant_student_links(applicant_id,student_id,linked_by,linked_at) VALUES(?,?,?,?)',(applicant_id,student_id,actor_id,stamp)).lastrowid
