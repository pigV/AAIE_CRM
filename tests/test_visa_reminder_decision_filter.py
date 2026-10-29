"""Run the actual Visa branch of the reminder producer with an isolated DB."""
import ast
from pathlib import Path
import sqlite3
import unittest

class VisaReminderDecisionFilterTests(unittest.TestCase):
    def generated(self,expiry):
        conn=sqlite3.connect(':memory:');conn.row_factory=sqlite3.Row;self.addCleanup(conn.close)
        conn.executescript('CREATE TABLE students(student_id TEXT PRIMARY KEY,consultant_id INTEGER); CREATE TABLE visas(id INTEGER PRIMARY KEY,student_id TEXT,visa_status TEXT,visa_expiry_date TEXT,file_path TEXT,created_at TEXT,visa_grant_date TEXT);')
        for index,status in enumerate(('granted','active','pending','refused',''),1):
            conn.execute('INSERT INTO students VALUES(?,1)',(str(index),))
            conn.execute('INSERT INTO visas VALUES(?,?,?,?,?,?,?)',(index,str(index),status,expiry,'/synthetic/letter.pdf','2026-09-07','2026-09-05'))
        class DB:
            def all(self,sql,params=()):return conn.execute(sql,params).fetchall()
        payloads=[]
        tree=ast.parse((Path(__file__).resolve().parents[1]/'app.py').read_text())
        fn=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='run_reminder_engine')
        block=next(n for n in fn.body if isinstance(n,ast.If) and isinstance(n.test,ast.Call) and n.test.args and isinstance(n.test.args[0],ast.Constant) and n.test.args[0].value=='reminder_visa_enabled')
        scope={'db':DB(),'bool_setting':lambda *a:True,'setting_int_list':lambda *a:[30],
            'reminder_source_on_or_after_baseline':lambda *a:True,'reminder_assignee_for_student':lambda *a:(1,'Staff'),
            'reminder_datetime_for_date':lambda expiry,days:'2026-10-01T09:00:00' if expiry else None,
            'create_auto_reminder':lambda value:payloads.append(value) or True,'generated':0,'missing_due_at':'2026-09-07T17:00:00'}
        exec(compile(ast.Module(body=[block],type_ignores=[]),'actual Visa reminder producer','exec'),scope)
        return payloads

    def test_only_granted_or_active_get_missing_grant_expiry_reminders(self):
        values=self.generated('')
        self.assertEqual({'1','2'},{v['student_id'] for v in values})
        self.assertEqual({'visa_missing_expiry'},{v['reminder_type'] for v in values})

    def test_pending_and_refused_never_get_grant_expiry_schedule(self):
        values=self.generated('2026-11-01')
        self.assertEqual({'1','2'},{v['student_id'] for v in values})
        self.assertEqual({'visa_expiry'},{v['reminder_type'] for v in values})

if __name__=='__main__':unittest.main()
