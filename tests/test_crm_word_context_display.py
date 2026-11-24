import ast
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote
from html import escape
import unittest

ROOT=Path(__file__).resolve().parents[1]

class StudentBusinessControlsTests(unittest.TestCase):
 def test_university_hides_local_admission_but_keeps_enrolment_management(self):
  tree=ast.parse((ROOT/'app.py').read_text())
  fn=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='student_detail_business_operations_html')
  ns={'quote':quote,'esc':escape}
  exec(compile(ast.fix_missing_locations(ast.Module(body=[fn],type_ignores=[])),'business-controls','exec'),ns)
  view=SimpleNamespace(student_id='S1',current_school=None,current=SimpleNamespace(lifecycle_type='initial'))
  html=ns[fn.name](view,[],can_edit_profile=True,higher_education=True)
  self.assertNotIn('local_admission=yes',html)
  self.assertIn('/students/S1/school-enrolments',html)
  school=ns[fn.name](view,[],can_edit_profile=True)
  self.assertIn('local_admission=yes',school)

class StudentTypeLabelsTests(unittest.TestCase):
 def test_type_is_a_label_and_does_not_change_the_legal_name(self):
  from student_context_display import student_type_badge_html
  university={'passport_first_name':'Anze','passport_last_name':'Zhao','application_stream':'higher_education','higher_education_course_type':None}
  before=dict(university)
  self.assertIn('大学',student_type_badge_html(university));self.assertEqual(before,university)
  self.assertIn('TAFE',student_type_badge_html(dict(university,higher_education_course_type='TAFE')))
  self.assertEqual('',student_type_badge_html(dict(university,application_stream='school_education')))

class CompletedReviewCountTests(unittest.TestCase):
 def test_confirmed_and_explicitly_removed_files_finish_legacy_batch(self):
  import sqlite3
  conn=sqlite3.connect(':memory:');conn.row_factory=sqlite3.Row
  conn.executescript("CREATE TABLE upload_sessions(id,status,total_files,confirmed_files,cancelled_files); CREATE TABLE upload_review_items(upload_session_id,status); INSERT INTO upload_sessions VALUES(1,'NeedsReview',2,0,0); INSERT INTO upload_review_items VALUES(1,'Confirmed'),(1,'Cancelled');")
  class Database:
   def all(self,sql,params):return conn.execute(sql,params).fetchall()
   def execute(self,sql,params):return conn.execute(sql,params)
  tree=ast.parse((ROOT/'app.py').read_text());fn=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='refresh_upload_session_counts')
  ns={'db':Database()};exec(compile(ast.fix_missing_locations(ast.Module(body=[fn],type_ignores=[])),'review-counts','exec'),ns)
  ns[fn.name](1)
  self.assertEqual(('Confirmed',2,1,1),tuple(conn.execute('SELECT status,total_files,confirmed_files,cancelled_files FROM upload_sessions').fetchone()))

if __name__=='__main__':unittest.main()
