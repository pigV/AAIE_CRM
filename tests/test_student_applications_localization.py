"""Display localization preserves submitted choices, names and #2 controls."""
import copy
import json
import re
import unittest
from unittest.mock import patch
from types import SimpleNamespace
from tests.test_student_detail_internal_output import app, build_fixture_page, REMOVED_LABELS
from higher_education_views import render_higher_education_workspace
from school_plan_student_views import render_school_plan_student_tab
from student_applications_display import (application_error_display, student_stage_display_label,
    application_status_label, student_profile_display_value)


class StudentApplicationsLocalizationTests(unittest.TestCase):
    def test_known_display_messages_preserve_unknown_original_text(self):
        self.assertEqual(student_stage_display_label('尚未生成家长表单 Link'), '尚未生成家长表单链接')
        self.assertEqual(application_error_display('系统处理失败：plan must be confirmed before submission'),
                         '系统处理失败：递交申请前请先确认学校方案')
        self.assertEqual(application_error_display('Original user text'), 'Original user text')
        self.assertEqual(student_stage_display_label('Original status'), 'Original status')
        for field in ('sibling_1_year_level', 'sibling_2_year_level', 'current_school_year_level'):
            self.assertEqual(student_profile_display_value(field, 'Year 8'), '8年级')
        self.assertEqual(student_profile_display_value('preferred_intake_term', 'unsure'), '不确定')
        for internal in ('sent_to_parent', 'signed', 'signed_received'):
            self.assertNotEqual(application_status_label(internal), internal)
        self.assertEqual(application_error_display('an inactive identical course already exists; ask an administrator to reactivate or merge it'),
                         '已存在相同的停用课程，请联系管理员重新启用或合并')

    def test_edit_form_preserves_choice_values_and_original_user_text(self):
        values = {'current_year_level': 'Year 8', 'application_stream': 'higher_education',
                  'higher_education_course_type': 'Diploma', 'english_nickname': 'Original nickname'}
        before = copy.deepcopy(values)
        html = app.student_edit_form('/students/1001/edit', 'fixture', values)
        self.assertIn('value="Year 8" selected>8年级', html)
        self.assertIn('value="Diploma" selected>文凭课程', html)
        self.assertIn('Original nickname', html)
        self.assertIn('name="regenerate_studentinfo" value="no" checked', html)
        self.assertIn('action="/students/1001/edit"', html)
        self.assertIn('href="/students/1001?tab=profile"', html)
        for text in ('Save Changes', 'Keep Existing', 'Cancel /', 'Student ID', 'Package 组合'):
            self.assertNotIn(text, html)
        self.assertEqual(values, before)

    def test_school_application_states_preserve_records_and_post_values(self):
        plans = [dict(id=1, plan_sector='private', city_name='Melbourne', is_current=True,
                     report_title='', status='confirmed', confirmed_schools=[dict(id=2,
                     school_name='Original School', submission_status='submitted', result_status='pending')])]
        before = copy.deepcopy(plans)
        html = render_school_plan_student_tab('1001', plans, 'fixture')
        self.assertIn('学校方案报告', html)
        self.assertIn('状态 已确认', html)
        self.assertIn('Original School', html)
        self.assertIn('value="pending" selected>等待学校结果', html)
        self.assertIn('/students/1001/school-plan/confirmed-schools/2/submission', html)
        self.assertEqual(plans, before)

    def test_report_create_and_snapshot_summary_are_localized_without_record_changes(self):
        student = dict(student_id='1001', passport_first_name='Original', passport_last_name='Name',
                       applying_year_level='Year 8', current_year_level='', preferred_intake_year='2027')
        before = copy.deepcopy(student)
        database = SimpleNamespace(all=lambda *a: [student] if 'FROM students' in a[0] else [],
                                   one=lambda *a: student)
        handler = object.__new__(app.CRMHandler)
        handler.user = {}; handler.csrf = 'fixture'; handler.query = {'student_id':['1001']}
        handler.message = lambda: ('','success')
        rendered = {}
        handler.send_html = lambda html: rendered.update(html=html)
        with patch.object(app,'db',database), patch.object(app,'can_access_module',return_value=True), \
             patch.object(app,'page',side_effect=lambda title,body,*a: title+body), \
             patch.object(app,'nas',SimpleNamespace(status=lambda:{})):
            handler.school_plan_report_create('GET')
        html = rendered['html']
        for text in ('创建学校方案报告','申请年级','8年级','生成学校方案 PDF'):
            self.assertIn(text,html)
        self.assertIn('action="/school-plan-reports/create"',html)
        self.assertIn('name="include_school_brochure_pdf" value="1"',html)
        self.assertEqual(student,before)
        report = dict(student_id='1001',status='generated',confirmed_at='',confirmed_by_name='',report_version=1)
        handler.school_plan_live_school_sections_html = lambda snapshot: ''
        summary = handler.school_plan_report_snapshot_summary_html(report, {'student':student})
        for text in ('学生摘要','学校对比','已生成','确认时间','8年级'):
            self.assertIn(text,summary)
        for text in ('Student Summary','Snapshot Reference','Not set','Parent Confirmation Page'):
            self.assertNotIn(text,summary)

    def test_viewer_form_keeps_english_role_values_and_localizes_known_log_values(self):
        handler = object.__new__(app.CRMHandler); handler.csrf='fixture'
        handler.school_plan_ensure_primary_report_viewer=lambda report: None
        database=SimpleNamespace(all=lambda *a: [])
        with patch.object(app,'db',database):
            html=handler.school_plan_report_share_management_html({'id':7})
        self.assertIn('<option value="Mother">母亲</option>',html)
        self.assertIn('action="/school-plan-reports/7/viewers/add"',html)
        self.assertNotIn('Add another viewer',html)
        from student_applications_display import viewer_role_label,view_event_label,viewer_device_label,school_report_title_label
        self.assertEqual(viewer_role_label('Primary Parent'),'主要家长')
        self.assertEqual(view_event_label('viewer_opened'),'查看人打开报告')
        self.assertEqual(viewer_device_label('mobile'),'移动设备')
        self.assertEqual(school_report_title_label('School Plan Report'),'学校方案报告')
        self.assertEqual(school_report_title_label('Original Report'),'Original Report')

    def test_higher_education_display_preserves_form_contract_and_proper_names(self):
        params = dict(providers=[dict(id=1,provider_type='University',active_status='Active',provider_name='Original University')],
            relationships=[],plans=[dict(id=3,status='Confirmed',student_id='1001',default_pathway_code='Foundation + Bachelor')],
            plan_items=[dict(id=4,plan_id=3,status='Draft',provider_name='Original University',course_name='Bachelor of Arts',course_type='Bachelor')],
            applications=[dict(id=5,status='Submitted',provider_name='Original University',course_name='Bachelor of Arts')],csrf='fixture',can_manage=True)
        before = copy.deepcopy(params)
        html = render_higher_education_workspace(**params)
        for text in ('大学申请', '已确认', '已递交', 'Original University', 'Bachelor of Arts', 'Foundation + Bachelor', 'Offer', 'CoE'):
            self.assertIn(text, html)
        self.assertIn('<option value="Direct">直接递交</option>', html)
        self.assertIn('/higher-education/plan-items/4/submit', html)
        for text in ('Application Submitted', 'No submitted', 'HIGHER EDUCATION APPLICATIONS', 'Select Student'):
            self.assertNotIn(text, html)
        self.assertEqual(params, before)

    def test_student_detail_keeps_closed_issue2_panels_absent(self):
        html = build_fixture_page()
        for text in REMOVED_LABELS:
            self.assertNotIn(text, html)
        self.assertNotIn('Admin / Manager', html)
        self.assertIn('data-tab="applications"', html)

if __name__ == '__main__':
    unittest.main()
