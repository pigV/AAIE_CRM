"""Scoped ownership, funding and checklist presentation for the Visa candidate.

No database or application startup side effects on import. Persistence continues
through the authenticated existing evidence save endpoint and migration-owned schema.
"""
import json
import re
import sys
from html import escape

OWNERS = ('Applicant', 'Father', 'Mother', 'Parents jointly', 'Spouse', 'Family', 'Other', 'None')
FUNDING_SOURCES = ('Deposit / Savings', 'Employment Income', 'Investment / Wealth Management', 'Business Income', 'Rental Income', 'Other')
OWNER_FIELDS = {
    'has_property': ('property_owner', 'Property owner / 房产持有人'),
    'has_company_or_business': ('company_owner', 'Business owner / 公司持有人'),
    'has_investment_assets': ('investment_assets_owner', 'Investment owner / 投资持有人'),
    'has_property_in_home_country': ('home_property_owner', 'Home property owner / 国内房产持有人'),
    'has_company_in_home_country': ('home_company_owner', 'Home business owner / 国内公司持有人'),
    'has_job_in_home_country': ('home_job_owner', 'Employment belongs to / 国内工作归属'),
}


def owner_options(existing=()):
    return tuple((value, value) for value in OWNERS) + tuple(option for option in existing if option[0] not in OWNERS)


def normalize_funding_sources(values):
    if isinstance(values, str):
        values = json.loads(values or '[]')
    if not isinstance(values, (list, tuple)) or any(value not in FUNDING_SOURCES for value in values):
        raise ValueError('Funds & Income: unsupported funding source')
    return [value for value in FUNDING_SOURCES if value in values]


def ownership_context(profile, assets, financial):
    profile = dict(profile or {})
    return {
        'ownership_rule': 'Never infer Applicant ownership from family property, company, funds or a yes/no answer. Use the explicit owner; missing ownership is Unknown. Family assets are not applicant-owned assets.',
        'profile': profile,
        'assets': [dict(dict(row), owner=dict(row).get('owner') or 'Unknown') for row in assets],
        'funds': [dict(dict(row), owner=dict(row).get('provider') or 'Unknown') for row in financial],
    }


def analysis_html(summary=None, full_analysis=''):
    summary = summary or {}
    cards = ''.join('<section><h3>' + label + '</h3><p>' + escape(str(summary.get(label) or 'Not assessed / 尚未评估')) + '</p></section>' for label in ('Summary', 'Risk', 'Missing', 'Action Required'))
    return '<link rel="stylesheet" href="/assets/visa-evidence-candidate.css"><section class="panel visa-analysis-summary" id="visa-analysis-summary">' + cards + '<details class="visa-full-analysis"><summary>View Full Analysis</summary><pre>' + escape(str(full_analysis or 'AI analysis has not been run.')) + '</pre></details></section>'


def checklist_search_html(panel_id):
    # DOM scope is the enclosing panel, not the whole page (several checklists coexist).
    return '''<div class="visa-checklist-search"><label>Keyword Search / 搜索材料 <input type="search" aria-label="Checklist keyword" placeholder="Keyword / 关键词"></label><label><input type="checkbox" data-filter> Filter matches / 仅显示匹配</label><button type="button" data-locate>Locate next / 定位下一项</button><span role="status" aria-live="polite"></span></div>
<link rel="stylesheet" href="/assets/visa-evidence-candidate.css">
<script src="/assets/visa-evidence-candidate.js" defer></script>'''


class VisaEvidenceCandidateMixin:
    def visa_evidence_sections(self):
        sections = super().visa_evidence_sections()
        output = []
        for section in sections:
            fields = []
            for field in section['fields']:
                if field[0] == 'funds_holder':
                    field = (*field[:3], owner_options(field[3]))
                fields.append(field)
                if field[0] in OWNER_FIELDS:
                    name, label = OWNER_FIELDS[field[0]]
                    fields.append((name, label, 'select', owner_options()))
            output.append(dict(section, fields=fields))
        return output

    def visa_evidence_field_names(self):
        return super().visa_evidence_field_names() + ['funding_sources_json']

    def visa_evidence_financial_item_options(self):
        options = super().visa_evidence_financial_item_options()
        return dict(options, provider=owner_options(options['provider']))

    def visa_evidence_asset_item_options(self):
        options = super().visa_evidence_asset_item_options()
        return dict(options, owner=owner_options(options['owner']))

    def visa_evidence_form_html(self, student_id, case, return_path=''):
        html = super().visa_evidence_form_html(student_id, case, return_path)
        profile = dict(self.visa_evidence_profile(case['id']) or {})
        try:
            selected = normalize_funding_sources(profile.get('funding_sources_json') or '[]')
        except (ValueError, TypeError):
            selected = []
        checks = ''.join('<label><input type="checkbox" name="funding_sources" value="' + escape(value) + '"' + (' checked' if value in selected else '') + '>' + escape(value) + '</label>' for value in FUNDING_SOURCES)
        fieldset = '<fieldset class="visa-funding-sources"><legend>Funds &amp; Income / 资金与收入来源（可多选）</legend><input type="hidden" name="funding_sources_present" value="1">' + checks + '</fieldset><p>Owner must identify who owns each asset or source. Parent assets do not mean applicant ownership. / 父母资产不代表申请人本人持有。</p>'
        return '<link rel="stylesheet" href="/assets/visa-evidence-candidate.css">' + re.sub(r'(<form\b[^>]*>)', lambda match: match.group(1) + fieldset, html, count=1)

    def student_visa_case_evidence_save(self, method, student_id, case_id):
        if method == 'POST':
            if getattr(self, 'form', None) is None:
                # Resolve the running app module; never import app (which has startup effects).
                runtime = next((sys.modules.get(cls.__module__) for cls in type(self).__mro__ if hasattr(sys.modules.get(cls.__module__), 'parse_request_body')), None)
                if runtime is None:
                    raise RuntimeError('Request body parser unavailable')
                self.form, _files = runtime.parse_request_body(self)
            form = self.form
            # The request parser returns one checkbox as str, repeated ones as list.
            sources = form.get('funding_sources', [])
            if isinstance(sources, str):
                sources = [sources]
            try:
                form['funding_sources_json'] = [json.dumps(normalize_funding_sources(sources), ensure_ascii=False)]
            except (ValueError, TypeError):
                return self.error_page('Funds & Income: unsupported funding source', 400)
        return super().student_visa_case_evidence_save(method, student_id, case_id)

    def visa_material_checklist_html(self, *args, **kwargs):
        html = super().visa_material_checklist_html(*args, **kwargs)
        if 'visa-material-row' not in html:
            return html
        return html.replace('<div class="panel-head">', checklist_search_html(kwargs.get('panel_id', 'material-checklist')) + '<div class="panel-head">', 1)

    def visa_evidence_review_placeholder_html(self):
        # Keep the existing full explanatory content and truthfully expose provider status.
        full = super().visa_evidence_review_placeholder_html()
        summary = analysis_html({'Summary': 'AI analysis not run / AI 尚未运行', 'Action Required': 'Review submitted documents and confirm findings manually.'})
        return summary.replace('<pre>AI analysis has not been run.</pre>', full)

    def visa_evidence_ownership_context(self, case_id):
        return ownership_context(self.visa_evidence_profile(case_id), self.visa_evidence_asset_items(case_id), self.visa_evidence_financial_items(case_id))
