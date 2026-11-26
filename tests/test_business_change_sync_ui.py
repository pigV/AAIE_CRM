from contextlib import contextmanager
import sqlite3
from types import SimpleNamespace
import unittest

from business_changes.schema import install
from business_changes.runtime import SETTING
from business_change_sync import BusinessChangeSyncMixin


class MemoryDB:
    def __init__(self,c):self.c=c
    def connect(self):return self.c
    def _release_connection(self,c):pass
    @contextmanager
    def read_snapshot(self):
        self.c.execute('BEGIN');self.c.execute('PRAGMA query_only=ON')
        try:yield self.c
        finally:self.c.rollback();self.c.execute('PRAGMA query_only=OFF')


class Base:
    def send_html(self,content,status=200,headers=None,**kwargs):return content
    def json_response(self,value,status=200):return status,value


class Harness(BusinessChangeSyncMixin,Base):
    def business_change_runtime(self):return self.g


class BusinessSyncUITests(unittest.TestCase):
    def test_student_material_tab_snapshot_uses_original_renderer_inside_readonly_snapshot(self):
        c=self.h.g['db'].c
        c.execute('CREATE TABLE students(student_id TEXT PRIMARY KEY,deleted_at TEXT)')
        c.execute("INSERT INTO students VALUES('S1',NULL)");c.commit()
        self.h.g['allowed']=lambda user,permission:permission=='students.view'
        def render(method,student_id,tab,*,fragment_only=False):
            self.assertEqual(('GET','S1','applications',True),(method,student_id,tab,fragment_only))
            self.assertTrue(c.in_transaction);self.assertEqual(1,c.execute('PRAGMA query_only').fetchone()[0])
            return '<div data-business-sync-fragment="student-tab-applications">Material list</div>'
        self.h.student_detail_tab=render
        self.h.query={'surface':['student_tab'],'student_id':['S1'],'tab':['applications']}
        status,value=self.h.business_change_snapshot('GET')
        self.assertEqual(200,status)
        self.assertIn('Material list',value['data']['fragments']['student-tab-applications'])
        self.h.query['tab']=['private_unknown'];self.assertEqual(400,self.h.business_change_snapshot('GET')[0])
    def setUp(self):
        c=sqlite3.connect(':memory:');self.addCleanup(c.close);c.row_factory=sqlite3.Row;c.execute('PRAGMA foreign_keys=ON')
        c.execute('BEGIN');install(c);c.commit()
        c.execute('CREATE TABLE system_settings(key PRIMARY KEY,value)');c.execute('INSERT INTO system_settings VALUES(?,?)',(SETTING,'enabled'));c.commit()
        self.h=Harness();self.h.user={'id':1};self.h.query={'surface':['dashboard_workflow']}
        self.allowed=True;self.reads=0
        def rows(conn,**kwargs):
            self.reads+=1
            return [SimpleNamespace(cycle_id=7,stage=SimpleNamespace(key='11_visa_result')),SimpleNamespace(cycle_id=8,stage=SimpleNamespace(key='10_visa_submission'))]
        self.h.g={'db':MemoryDB(c),'can_access_module':lambda u,key:self.allowed,'allowed':lambda user,permission:False,'load_application_stage_distribution':rows,
                  'is_workflow_v2_active':lambda conn:True,'dashboard_workflow_distribution_chart_html':lambda rows,**kwargs:'<section>approved dashboard</section>'}

    def test_snapshot_requires_login_and_existing_dashboard_permission(self):
        self.h.user=None;self.assertEqual(401,self.h.business_change_snapshot('GET')[0]);self.assertEqual(0,self.reads)
        self.h.user={'id':1};self.allowed=False;self.assertEqual(403,self.h.business_change_snapshot('GET')[0]);self.assertEqual(0,self.reads)

    def test_snapshot_counts_and_fragments_share_one_readonly_snapshot(self):
        status,value=self.h.business_change_snapshot('GET');self.assertEqual(200,status)
        self.assertEqual(2,value['data']['total']);self.assertEqual({'11_visa_result':1,'10_visa_submission':1},value['data']['counts'])
        self.assertEqual('<section>approved dashboard</section>',value['data']['fragments']['dashboard-workflow'])

    def test_chart_refresh_rereads_stages_without_loading_other_dashboard_data(self):
        conn = self.h.g['db'].c
        conn.execute('CREATE TABLE synthetic_cycles (id INTEGER PRIMARY KEY, stage TEXT)')
        conn.execute("INSERT INTO synthetic_cycles VALUES (1, '10_visa_submission')")
        conn.commit()

        def load_stages(connection, *, cycles_only, read_context):
            self.assertTrue(cycles_only)
            self.assertIs(connection, read_context.connection)
            self.assertEqual(1, connection.execute('PRAGMA query_only').fetchone()[0])
            return [SimpleNamespace(cycle_id=row['id'], stage=SimpleNamespace(key=row['stage']))
                    for row in connection.execute('SELECT * FROM synthetic_cycles')]

        def unrelated_read(*args, **kwargs):
            self.fail('the chart refresh loaded an unrelated dashboard read')

        self.h.g['load_application_stage_distribution'] = load_stages
        self.h.business_change_visa_consistency_issues = unrelated_read
        self.h._dashboard_read_snapshot = unrelated_read
        self.h.query['view'] = ['chart']

        status, before = self.h.business_change_snapshot('GET')
        self.assertEqual(200, status)
        self.assertEqual({'10_visa_submission': 1}, before['data']['counts'])

        conn.execute("INSERT INTO synthetic_cycles VALUES (2, '11_visa_result')")
        conn.commit()
        status, after = self.h.business_change_snapshot('GET')
        self.assertEqual(200, status)
        self.assertEqual({'10_visa_submission': 1, '11_visa_result': 1}, after['data']['counts'])
        self.assertEqual(2, after['data']['total'])
        self.assertEqual({'dashboard-workflow'}, set(after['data']['fragments']))
        self.assertGreater(after['as_of'], before['as_of'])
        self.assertNotEqual(after['etag'], before['etag'])

        status, unchanged = self.h.business_change_snapshot('GET')
        self.assertEqual(200, status)
        self.assertEqual(after['etag'], unchanged['etag'])
        self.assertGreater(unchanged['as_of'], after['as_of'])

    def test_post_unknown_surface_and_invalid_revision_are_rejected(self):
        self.assertEqual(405,self.h.business_change_snapshot('POST')[0])
        self.h.query={'surface':['all_database_records']};self.assertEqual(400,self.h.business_change_snapshot('GET')[0])
        self.h.query={'surface':['dashboard_workflow'],'minimum_version':['-1']};self.assertEqual(400,self.h.business_change_snapshot('GET')[0])

    def test_behind_required_revision_returns_retryable_response(self):
        self.h.query['minimum_version']=['2'];self.assertEqual(409,self.h.business_change_snapshot('GET')[0]);self.assertEqual(0,self.reads)

    def test_script_only_injected_for_enabled_authenticated_app_shell(self):
        html='<div class="app-shell"></div></body>'
        self.assertIn('/assets/business-change-refresh.js',self.h.send_html(html))
        self.h.user=None;self.assertNotIn('business-change-refresh',self.h.send_html(html))

    def test_mobile_source_snapshot_loads_the_same_refresh_controller(self):
        html='<body class="mobile-page-body"><div class="mobile-ui"><div data-business-sync-endpoint="/api/business-changes/snapshot?surface=mobile_dashboard_overview"></div></div></body>'
        self.assertIn('/assets/business-change-refresh.js',self.h.send_html(html))
        self.h.user=None
        self.assertNotIn('/assets/business-change-refresh.js',self.h.send_html(html))

    def test_overview_uses_same_native_dashboard_renderer_and_read_connection(self):
        calls=[]
        def render(method,context,*,fragment_only,stage_students):
            self.assertIs(self.h.g['db'].c,context.connection)
            self.assertTrue(context.connection.in_transaction)
            self.assertEqual(1,context.connection.execute('PRAGMA query_only').fetchone()[0])
            self.assertEqual([7,8],[row.cycle_id for row in stage_students])
            calls.append(fragment_only)
            return '<div data-business-sync-fragment="dashboard-overview">Recent students and KPIs</div>'
        self.h._dashboard_read_snapshot=render
        self.h.query={'surface':['dashboard_overview']}
        status,value=self.h.business_change_snapshot('GET')
        self.assertEqual(200,status)
        self.assertIn('Recent students and KPIs',value['data']['fragments']['dashboard-overview'])
        self.assertEqual([True],calls)
        self.assertEqual(1,self.reads)

    def test_visa_list_snapshot_uses_visa_permission_and_same_readonly_connection(self):
        self.h.g['can_access_module']=lambda user,module:module=='visa'
        def render(method,*,fragment_only=False):
            self.assertTrue(fragment_only)
            self.assertTrue(self.h.g['db'].c.in_transaction)
            self.assertEqual(1,self.h.g['db'].c.execute('PRAGMA query_only').fetchone()[0])
            return '<div data-business-sync-fragment="visa-overview">Visa list</div>'
        self.h.visa_management=render;self.h.query={'surface':['visa_overview']}
        status,value=self.h.business_change_snapshot('GET')
        self.assertEqual(200,status)
        self.assertIn('Visa list',value['data']['fragments']['visa-overview'])
        self.assertEqual(0,self.reads)

    def test_initial_visa_page_renders_whole_list_and_kpis_inside_one_snapshot(self):
        self.h.g['can_access_module']=lambda u,k:k=='visa'
        self.h.csrf='synthetic';self.h.g['page']=lambda title,body,*args:body
        self.h.g['nas']=SimpleNamespace(status=lambda:'synthetic storage')
        def render(method,*,fragment_only):
            self.assertTrue(self.h.g['db'].c.in_transaction)
            self.assertEqual(1,self.h.g['db'].c.execute('PRAGMA query_only').fetchone()[0])
            return '<div data-business-sync-fragment="visa-overview">consistent initial list</div>'
        self.h.visa_management=render
        self.assertIn('consistent initial list',self.h.business_change_visa_overview_page())

    def test_student_summary_uses_student_permission_and_owns_requested_cycle(self):
        c=self.h.g['db'].c
        c.execute('CREATE TABLE students(student_id TEXT PRIMARY KEY,deleted_at TEXT)')
        c.execute("INSERT INTO students VALUES('S1',NULL)")
        c.execute('CREATE TABLE application_cycles(id INTEGER PRIMARY KEY,student_id TEXT)')
        c.execute("INSERT INTO application_cycles VALUES(7,'S1'),(8,'S2')");c.commit()
        self.h.g['can_access_module']=lambda u,m:m=='students'
        self.h.g['allowed']=lambda u,p:p=='students.view'
        def render(method,student_id,*,fragment_only):
            self.assertEqual('S1',student_id);self.assertTrue(fragment_only)
            self.assertTrue(c.in_transaction);self.assertEqual(1,c.execute('PRAGMA query_only').fetchone()[0])
            return '<div data-business-sync-fragment="student-summary">Current stage</div>'
        self.h.student_detail=render
        self.h.query={'surface':['student_summary'],'student_id':['S1'],'application_cycle_id':['7']}
        status,value=self.h.business_change_snapshot('GET')
        self.assertEqual(200,status);self.assertIn('Current stage',value['data']['fragments']['student-summary'])
        self.h.query['application_cycle_id']=['8']
        self.assertEqual(404,self.h.business_change_snapshot('GET')[0])

    def test_student_summary_denies_before_loading_without_student_permission(self):
        self.h.g['allowed']=lambda u,p:False
        self.h.query={'surface':['student_summary'],'student_id':['S1']}
        self.assertEqual(403,self.h.business_change_snapshot('GET')[0])
