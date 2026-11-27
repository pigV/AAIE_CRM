"""Value-based presentation contracts for the three existing Visa case tabs."""
from html.parser import HTMLParser
import unittest

import visa_overview_ui as tones
from visa_result_workspace_ui import render_result_workspace


class Elements(HTMLParser):
    def __init__(self, html):
        super().__init__(); self.elements=[]; self.feed(html)
    def handle_starttag(self, tag, attrs):
        self.elements.append((tag,dict(attrs)))


class VisaCaseFigmaTests(unittest.TestCase):
    def test_same_status_and_decision_share_existing_overview_tone(self):
        for label,tone in [('Granted','green'),('Refused','red'),('Withdrawn','gray'),('Pending','amber'),('Draft','gray')]:
            self.assertEqual(tones.visa_state_tone(label),tone)
            for kind in ('status','decision'):
                self.assertIn('visa-overview-tag--'+tone, tones.visa_overview_badge(label,kind))

    def test_collection_and_material_translations_share_a_tone(self):
        for english,chinese,tone in [('Not Started','未开始','amber'),('Draft','草稿','gray'),
            ('In Progress','填写中','blue'),('Submitted','已提交','green'),
            ('Returned for Update','已退回补充','amber'),('Reviewed','已审核','green'),
            ('Locked','已锁定','gray'),('Expired','已过期','red'),
            ('Active','有效','green'),('Manually Revoked','已手动失效','gray'),
            ('missing','未收到','amber'),('uploaded','已收到','green'),('not_applicable','不适用','gray')]:
            self.assertEqual(tones.visa_state_tone(english),tone)
            self.assertEqual(tones.visa_state_tone(chinese),tone)

    def test_normalization_and_unknown_values_never_become_markup(self):
        self.assertEqual(tones.visa_state_tone(' REFUSED '),'red')
        self.assertEqual(tones.visa_state_tone('<img src=x>'),'gray')
        self.assertEqual(tones.visa_state_tone(None),'gray')
        self.assertNotIn('<img',tones.visa_overview_badge('<img src=x>','status'))

    def test_terminal_stage_colors_follow_outcome_without_changing_actions(self):
        for label,tone in [('Granted','green'),('Refused','red'),('Withdrawn','gray'),('Closed','gray')]:
            markup=Elements(render_result_workspace({'id':7,'student_id':'S1'}, {'effective_result':label},'<p>materials</p>','<p>processing</p>'))
            current=next(a for _,a in markup.elements if 'visa-result-current' in a.get('class',''))
            self.assertEqual(current.get('data-visa-tone'),tone)
            action=next(a for t,a in markup.elements if t=='a' and a.get('class')=='primary button-link')
            self.assertEqual(action['href'],'#visa-result-dates')

    def test_review_and_conflict_warn_even_when_retained_result_is_granted(self):
        for flag in ('requires_review','conflict'):
            html=render_result_workspace({'id':7,'student_id':'S1'}, {'effective_result':'Granted',flag:True},'','')
            attrs=next(a for _,a in Elements(html).elements if 'visa-result-current' in a.get('class',''))
            self.assertEqual(attrs.get('data-visa-tone'),'amber')
            self.assertIn('href="#visa-result-dates"',html)



class ExpandedOverviewToneTests(unittest.TestCase):
    def test_expanded_status_and_decision_match_header_and_summary(self):
        import ast
        from html import escape
        from pathlib import Path
        source=(Path(__file__).resolve().parents[1]/'app.py').read_text()
        node=next(n for n in ast.walk(ast.parse(source)) if isinstance(n,ast.FunctionDef) and n.name=='student_visa_case_foundation_field_card_html')
        env={'esc':lambda v:escape(str(v or ''))}
        exec(compile(ast.Module(body=[node],type_ignores=[]),'real-field-card','exec'),env)
        class Handler:
            visa_management_text=staticmethod(lambda v:v)
        render=env[node.name]
        for label in ('Status','Result Status'):
            for value,tone in [('Granted','green'),('Refused','red'),('Pending','amber'),('Withdrawn','gray')]:
                attrs=next(a for t,a in Elements(render(Handler(),'S1',{'id':1},label,value)).elements if t=='dd')
                self.assertEqual(attrs.get('data-visa-tone'),tone)
        html=render(Handler(),'S1',{'id':1},'Case Owner','<script>')
        self.assertNotIn('data-visa-tone',html)
        self.assertNotIn('<script>',html)


class ResultSummaryToneTests(unittest.TestCase):
    def test_current_detected_and_editable_outcomes_have_semantic_values(self):
        from visa_workspace_candidate import document_workspace_html
        for value,tone in [('Granted','green'),('Refused','red'),('Withdrawn','gray'),('Pending','amber')]:
            html=document_workspace_html(7,'csrf',[],[],[],result={'effective_result':value,'auto_detected_result':value,'authority':'auto'})
            colored=[a['data-visa-tone'] for _,a in Elements(html).elements if 'data-visa-tone' in a]
            self.assertEqual(colored,[tone,tone])
            self.assertIn(f'<option value="{value}" selected>',html)

class MaterialLegendScopeTests(unittest.TestCase):
    def test_legend_matches_student_and_independent_workspace_palettes(self):
        from visa_material_workspace_ui import render_material_workspace
        from tests.test_visa_material_workspace_ui import Handler, rows
        for sid,color in [('S1','紫色'),('','橙色')]:
            html=render_material_workspace(Handler(),sid,{'id':7},rows(),panel_id='material-checklist',title='Checklist',description='Materials',show_smart_batch=True)
            self.assertIn(color+'：未完成',html)

class OverviewStateScopeTests(unittest.TestCase):
    def test_state_palette_markers_do_not_change_type_or_subclass(self):
        for kind in ('status','decision','type','subclass'):
            html=tones.visa_overview_badge('Granted' if kind in ('status','decision') else '500',kind)
            attrs=Elements(html).elements[0][1]
            self.assertEqual('data-visa-state' in attrs,kind in ('status','decision'))

if __name__=='__main__': unittest.main()
