"""Homepage grouping tests exercise rendered counts, links and read-only snapshots."""
import ast
from html.parser import HTMLParser
from pathlib import Path
import sqlite3
from types import SimpleNamespace
import unittest

import app
from mobile.home.data import _stage_summaries
from student_application_stage_distribution import ApplicationStageResult, ApplicationStageStudent
from tests import test_business_change_sync_ui as sync_fixture
from workflow_cutover import CUTOVER_SETTING_KEY, REQUIRED_EVIDENCE_KEYS
import json


def student(number, kind='initial', key='07_school_response', label='等待学校发 Offer'):
    return ApplicationStageStudent(str(number),f'Student {number}',
        ApplicationStageResult(key,label,7,'等待学校处理结果',0,'school_plans'),
        cycle_id=100+number,source_model='application_cycle',cycle_type=kind,
        cycle_sequence=1,target_context='Target School')


def mixed_students():
    return tuple(student(i) for i in range(1,12)) + tuple(
        student(i,'transfer','school_application','学校申请') for i in (12,13))


class Groups(HTMLParser):
    def __init__(self,html):
        super().__init__();self.groups={};self.current=None;self.feed(html)
    def handle_starttag(self,tag,attrs):
        attrs=dict(attrs)
        if tag=='details' and 'data-business-sync-key' in attrs:
            self.current={'summary':'','links':[],'text':[]}
            self.groups[attrs['data-business-sync-key']]=self.current
        if self.current is not None:
            if tag=='summary':self.current['summary']=attrs.get('aria-label','')
            if tag=='a':self.current['links'].append(attrs)
    def handle_data(self,data):
        if self.current is not None:self.current['text'].append(data)
    def handle_endtag(self,tag):
        if tag=='details':self.current=None


class HomepageOfferGroupingTests(unittest.TestCase):
    def test_eleven_initial_and_two_transfer_cycles_share_one_thirteen_count_bucket(self):
        rows=mixed_students();before=tuple((r.stage.key,r.cycle_id) for r in rows)
        html=app.dashboard_workflow_distribution_chart_html(rows,canonical=True)
        groups=Groups(html).groups
        self.assertNotIn('school_application',groups)
        self.assertEqual('展开等待学校发 Offer明细，共 13 个申请周期',groups['07_school_response']['summary'])
        self.assertEqual(13,len(groups['07_school_response']['links']))
        self.assertIn('共 13 个在办周期',html)
        self.assertEqual(before,tuple((r.stage.key,r.cycle_id) for r in rows))

    def test_unified_details_keep_cycle_links_and_initial_transfer_identity(self):
        groups=Groups(app.dashboard_workflow_distribution_chart_html(mixed_students(),canonical=True)).groups
        bucket=groups['07_school_response'];links=bucket['links']
        self.assertEqual(11,sum(a['data-lifecycle-type']=='initial' for a in links))
        self.assertEqual(2,sum(a['data-lifecycle-type']=='transfer' for a in links))
        self.assertEqual(13,len({a['href'] for a in links}))
        self.assertIn('/students/12?tab=school_plans&application_cycle_id=112',[a['href'] for a in links])
        self.assertIn('首次申请',''.join(bucket['text']))
        self.assertIn('转学',''.join(bucket['text']))
        self.assertIn('等待学校处理结果',''.join(bucket['text']))

    def test_transfer_only_is_visible_under_offer_group(self):
        html=app.dashboard_workflow_distribution_chart_html((student(12,'transfer','school_application','学校申请'),),canonical=True)
        groups=Groups(html).groups
        self.assertEqual(1,len(groups['07_school_response']['links']))
        self.assertNotIn('school_application',groups)

    def test_preparing_and_received_offer_stay_out_of_waiting_response(self):
        rows=(student(1),student(2,'transfer','school_application','学校申请'),
              student(3,'initial','06_school_application_submission','准备学校申请'),
              student(4,'transfer','offer_received','已收到 Offer'))
        groups=Groups(app.dashboard_workflow_distribution_chart_html(rows,canonical=True)).groups
        self.assertEqual(2,len(groups['07_school_response']['links']))
        self.assertEqual(1,len(groups['06_school_application_submission']['links']))
        self.assertEqual(1,len(groups['offer_received']['links']))

    def test_no_transfer_preserves_original_offer_membership_and_no_extra_group(self):
        rows=(student(1),student(2))
        groups=Groups(app.dashboard_workflow_distribution_chart_html(rows,canonical=True)).groups
        self.assertEqual(2,len(groups['07_school_response']['links']))
        self.assertNotIn('school_application',groups)

    def test_legacy_chart_also_groups_transfer_with_waiting_offer(self):
        rows=(student(1,'initial','waiting_school_offer','等待学校 Offer'),student(2,'transfer','school_application','学校申请'))
        groups=Groups(app.dashboard_workflow_distribution_chart_html(rows,canonical=False)).groups
        self.assertNotIn('school_application',groups)
        self.assertEqual(2,len(groups['waiting_school_offer']['links']))

    def test_mobile_initial_card_has_same_thirteen_count_and_destination(self):
        c=sqlite3.connect(':memory:');self.addCleanup(c.close)
        c.execute('CREATE TABLE system_settings(key,value)')
        for table in ('application_cycles','public_application_groups','private_school_applications',
                      'offer_received_events','cycle_final_acceptances','final_school_submission_events','visa_decision_events'):
            c.execute('CREATE TABLE '+table+'(id)')
        c.execute('INSERT INTO system_settings VALUES(?,?)',(CUTOVER_SETTING_KEY,json.dumps(
            {'version':1,'active':True,'evidence':{k:True for k in REQUIRED_EVIDENCE_KEYS}})))
        c.commit();before=c.total_changes
        stages=_stage_summaries(None,lambda conn,**kwargs:mixed_students(),connection=c,cycles_only=True)
        by_key={s.key:s for s in stages}
        self.assertNotIn('school_application',by_key)
        self.assertEqual(13,by_key['07_school_response'].count)
        self.assertEqual('等待学校发 Offer',by_key['07_school_response'].label)
        self.assertEqual('/students?stage=07_school_response',by_key['07_school_response'].href)
        self.assertEqual(13,sum(s.count for s in stages))
        self.assertEqual(before,c.total_changes)

    def test_mobile_group_destination_student_filter_keeps_all_thirteen_students(self):
        tree=ast.parse(Path(app.__file__).read_text())
        method=next(node for node in ast.walk(tree) if isinstance(node,ast.FunctionDef) and node.name=='students')
        filtering=next(node for node in method.body if isinstance(node,ast.If) and isinstance(node.test,ast.Name) and node.test.id=='stage_filter')
        namespace=dict(vars(app));namespace.update(stage_filter='07_school_response',
            rows_data=[{'student_id':r.student_id} for r in mixed_students()],
            summary_map={r.student_id:SimpleNamespace(stage_key=r.stage.key,lifecycle_type=r.cycle_type) for r in mixed_students()})
        exec(compile(ast.Module(body=[filtering],type_ignores=[]),app.__file__,'exec'),namespace)
        self.assertEqual(13,len(namespace['rows_data']))
        self.assertEqual({'12','13'},{r['student_id'] for r in namespace['rows_data'] if r['student_id'] in {'12','13'}})


class HomepageOfferSnapshotTests(unittest.TestCase):
    setUp=sync_fixture.BusinessSyncUITests.setUp

    def test_refresh_counts_and_actual_html_fragment_share_group(self):
        self.h.g['load_application_stage_distribution']=lambda conn,**kwargs:mixed_students()
        self.h.g['dashboard_workflow_distribution_chart_html']=app.dashboard_workflow_distribution_chart_html
        status,result=self.h.business_change_snapshot('GET')
        self.assertEqual(200,status)
        self.assertEqual(13,result['data']['total'])
        self.assertEqual({'07_school_response':13},result['data']['counts'])
        groups=Groups(result['data']['fragments']['dashboard-workflow']).groups
        self.assertEqual(13,len(groups['07_school_response']['links']))
        self.assertNotIn('school_application',groups)
