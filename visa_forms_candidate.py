"""Visa form assignment isolation, actionable validation and traceable staff edits."""
import copy
import html
import json
import re
from urllib.parse import quote


def esc(value):
    return html.escape(str(value or ''), quote=True)


class VisaFormsCandidateMixin:
    def visa_forms_runtime(self):
        # Resolve the actual application module, including when it runs as __main__.
        return super().visa_aic_500_upsert_draft.__func__.__globals__

    def visa_material_profile(self, case):
        # Guardian arrangements on a 500 case describe another person, not subclass.
        for key in ('visa_subclass', 'visa_type', 'case_type'):
            value = str(case[key] or '').lower() if key in case.keys() else ''
            subclasses = re.findall(r'(?<!\d)(500|590|485)(?!\d)', value)
            if subclasses:
                return 'guardian' if subclasses[0] == '590' else 'student'
        primary = ' '.join(str(case[k] or '') for k in ('visa_type', 'case_type') if k in case.keys()).lower()
        return 'guardian' if 'guardian' in primary else 'student'

    def visa_aic_500_collect_repeat_rows(self, form, group, fields, existing_rows):
        prefix = group + '__'
        if not any(str(k).startswith(prefix) for k in form):
            return copy.deepcopy(existing_rows or [])
        indices = sorted({int(str(k).split('__')[1]) for k in form if str(k).startswith(prefix) and len(str(k).split('__')) == 3 and str(k).split('__')[1].isdigit()})
        rows = []
        for index in indices:
            deleted = form.get(f'{group}__{index}__delete', '')
            if deleted in ('1', 'on', ['1'], ['on']):
                continue
            rows.append({name: self.visa_aic_500_field_value(form, f'{group}__{index}__{name}', kind) for name, _, kind in fields})
        return rows

    def visa_aic_500_validation_details(self, payload):
        details = []
        sections = payload.get('sections') or {}
        scalar = self.visa_aic_500_scalar_fields()
        groups = self.visa_aic_500_repeat_groups()
        labels = self.visa_aic_500_section_labels()
        for key, label in self.visa_aic_500_unconfirmed_sections(payload):
            fields = scalar.get(key, ())
            field = fields[0][0] if fields else ''
            if not field:
                group_entry = next(((g, c) for g, c in groups.items() if c['section'] == key), None)
                if group_entry:
                    field = f'{group_entry[0]}__0__{group_entry[1]["fields"][0][0]}'
            details.append(dict(section=key, section_label=label, field=field, label='Section confirmation', reason='请填写或确认本 Section'))
        for section_key, values in sections.items():
            if not isinstance(values, dict):
                continue
            for name, label, kind in scalar.get(section_key, ()):
                value = str(values.get(name) or '').strip()
                if (kind == 'date' or name in self.visa_aic_500_date_field_names()) and value and not self.visa_aic_500_strict_date_ok(value):
                    details.append(dict(section=section_key, section_label=labels.get(section_key, section_key), field=name, label=label, reason='日期无效；请使用 DD/MM/YYYY，例如 10/05/1990'))
            for group, config in groups.items():
                if config['section'] != section_key:
                    continue
                for index, row in enumerate(values.get(group) or []):
                    for name, label, kind in config['fields']:
                        value = str(row.get(name) or '').strip()
                        if (kind == 'date' or name in self.visa_aic_500_date_field_names()) and value and not self.visa_aic_500_strict_date_ok(value):
                            details.append(dict(section=section_key, section_label=labels.get(section_key, section_key), field=f'{group}__{index}__{name}', label=f'{config["label"]} {index+1} / {label}', reason='日期无效；请使用 DD/MM/YYYY，例如 10/05/1990'))
        return details

    def visa_aic_500_review_errors(self, payload):
        return [f'{e["section_label"]} / {e["label"]} [{e["field"]}]: {e["reason"]}' for e in self.visa_aic_500_validation_details(payload)]

    def visa_form_validation_summary_html(self, token, details, form_type='visa_application_info_500_590_485'):
        if not details:
            return ''
        rows = []
        for e in details:
            href = f'/visa-information-collection/{quote(token)}/forms/{quote(form_type)}/{quote(e["section"])}?focus={quote(e["field"])}'
            rows.append(f'<li><a href="{esc(href)}">{esc(e["section_label"])} / {esc(e["label"])}</a>：{esc(e["reason"])}</li>')
        return '<section class="notice error visa-validation-summary" role="alert" tabindex="-1"><h2>提交失败：Section / Field / Error reason</h2><ul>' + ''.join(rows) + '</ul></section>'

    def visa_form_focus_script(self):
        return '<link rel="stylesheet" href="/assets/visa-forms-candidate.css"><script src="/assets/visa-forms-candidate.js" defer></script>'


    def visa_aic_500_wizard_html(self, token, respondent, assignment, payload, section, message='', errors=None, readonly=False):
        body = super().visa_aic_500_wizard_html(token, respondent, assignment, payload, section, message, errors, readonly)
        details = self.visa_aic_500_validation_details(payload) if errors else []
        return self.visa_form_validation_summary_html(token, details) + body + self.visa_form_focus_script()

    def visa_aic_500_admin_edit_html(self, student_id, case_id, respondent, assignment, payload):
        body = super().visa_aic_500_admin_edit_html(student_id, case_id, respondent, assignment, payload)
        for group in ('study_work_history', 'travel_history'):
            config = self.visa_aic_500_repeat_groups()[group]
            rows = (payload.get('sections', {}).get(config['section']) or {}).get(group) or []
            def row_html(index, row):
                cells = ''.join(self.visa_aic_500_admin_edit_input_html(f'{group}__{index}__{name}', label, kind, row.get(name,'')) for name,label,kind in config['fields'])
                return f'<article class="aic-repeat-card"><div class="aic-repeat-row">{cells}</div><label><input type="checkbox" name="{group}__{index}__delete" value="1"> Delete / 删除本条（保存后生效）</label></article>'
            replacement = f'<section class="aic-repeat-group visa-information-admin-repeat-group" data-staff-repeat="{group}" data-next="{len(rows)}"><h4>{esc(config["label"])}</h4><input type="hidden" name="{group}__present" value="1"><div data-rows>{"".join(row_html(i,r) for i,r in enumerate(rows))}</div><template>{row_html("INDEX",{})}</template><button type="button" class="secondary" data-add-row>Add / 添加记录</button></section>'
            pattern = r'<section class="aic-repeat-group visa-information-admin-repeat-group">\s*<h4>' + re.escape(esc(config['label'])) + r'</h4>.*?</section>'
            body = re.sub(pattern, lambda m: replacement, body, flags=re.S)
        body += self.visa_form_focus_script()
        return body + self.visa_form_history_html(assignment['id'])

    def student_visa_case_information_assignment_edit(self, method, student_id, case_id, respondent_id, assignment_id):
        runtime = self.visa_forms_runtime()
        if not runtime['can_access_module'](self.user, 'visa'):
            return self.error_page('你没有访问 Visa Management 的权限。', 403)
        case, respondent, assignment = self.student_visa_case_information_assignment_row(student_id, case_id, respondent_id, assignment_id)
        if not case:
            return
        if assignment and assignment['status'] == 'Cancelled':
            return self.error_page('当前 Case 的表单分配已取消。', 410)
        self._visa_staff_edit = method == 'POST'
        try:
            return super().student_visa_case_information_assignment_edit(method, student_id, case_id, respondent_id, assignment_id)
        finally:
            self._visa_staff_edit = False

    def visa_aic_500_upsert_draft(self, assignment, respondent, payload, status='Draft', submitted=False):
        runtime = self.visa_forms_runtime()
        db = runtime['db']
        stamp = runtime['now_iso']()
        staff = bool(getattr(self, '_visa_staff_edit', False))
        actor = (getattr(self, 'user', None) or {}).get('id') if staff else None
        draft_json = json.dumps(payload, ensure_ascii=False)
        with db.transaction() as conn:
            current = conn.execute('SELECT * FROM visa_form_assignments WHERE id=? AND respondent_id=? AND visa_case_id=?', (assignment['id'], respondent['id'], respondent['visa_case_id'])).fetchone()
            if not current or current['status'] == 'Cancelled':
                raise ValueError('当前 Case 的表单分配已取消或不存在。')
            existing = conn.execute('SELECT * FROM visa_form_drafts WHERE assignment_id=?', (assignment['id'],)).fetchone()
            original = conn.execute('SELECT assignment_id FROM visa_form_submission_originals WHERE assignment_id=?', (assignment['id'],)).fetchone()
            # A legacy effective baseline is explicitly NOT a recovered student original.
            if not original and ((submitted and not staff) or (staff and existing)):
                baseline = existing['draft_json'] if staff else draft_json
                source = 'legacy_effective_baseline' if staff else 'student_submission'
                conn.execute('INSERT INTO visa_form_submission_originals(assignment_id,respondent_id,visa_case_id,original_json,captured_at,source) VALUES(?,?,?,?,?,?)', (assignment['id'], respondent['id'], respondent['visa_case_id'], baseline, stamp, source))
            status = 'Submitted' if submitted or status == 'Submitted' or current['status'] == 'Submitted' else status
            progress = 100 if status == 'Submitted' else self.visa_aic_500_progress(payload)
            submitted_at = stamp if submitted else None
            before = existing['draft_json'] if existing else '{}'
            if staff or submitted:
                conn.execute('INSERT INTO visa_form_change_audit(assignment_id,visa_case_id,actor_id,action,before_json,after_json,created_at) VALUES(?,?,?,?,?,?,?)', (assignment['id'],respondent['visa_case_id'],actor,'staff_edit' if staff else 'student_submission',before,draft_json,stamp))
            conn.execute('''INSERT INTO visa_form_drafts(assignment_id,respondent_id,visa_case_id,draft_json,status,progress_percent,last_saved_at,submitted_at,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(assignment_id) DO UPDATE SET draft_json=excluded.draft_json,status=excluded.status,progress_percent=excluded.progress_percent,last_saved_at=excluded.last_saved_at,submitted_at=COALESCE(excluded.submitted_at,visa_form_drafts.submitted_at),updated_at=excluded.updated_at''', (assignment['id'],respondent['id'],respondent['visa_case_id'],draft_json,status,progress,stamp,submitted_at,stamp,stamp))
            conn.execute('UPDATE visa_form_assignments SET status=?,progress_percent=?,last_saved_at=?,submitted_at=COALESCE(?,submitted_at),updated_at=? WHERE id=?', (status,progress,stamp,submitted_at,stamp,assignment['id']))
        self.visa_information_collection_update_respondent_summary(respondent)
        return stamp, progress

    def student_visa_case_information_assignment_cancel(self, method, student_id, case_id, respondent_id, assignment_id):
        runtime = self.visa_forms_runtime()
        if method != 'POST':
            return self.error_page('Method Not Allowed', 405)
        if not runtime['can_access_module'](self.user, 'visa'):
            return self.error_page('你没有访问 Visa Management 的权限。', 403)
        case, respondent, assignment = self.student_visa_case_information_assignment_row(student_id,case_id,respondent_id,assignment_id)
        if not case:
            return
        if not respondent or not assignment:
            return self.error_page('未找到对应表单。', 404)
        db = runtime['db']; stamp = runtime['now_iso']()
        with db.transaction() as conn:
            current = conn.execute('SELECT * FROM visa_form_assignments WHERE id=? AND respondent_id=? AND visa_case_id=?', (assignment_id,respondent_id,case_id)).fetchone()
            if not current:
                return self.error_page('未找到对应表单。',404)
            if current['status'] != 'Cancelled':
                conn.execute('UPDATE visa_form_assignments SET status=?,updated_at=? WHERE id=? AND respondent_id=? AND visa_case_id=?', ('Cancelled',stamp,assignment_id,respondent_id,case_id))
                conn.execute('INSERT INTO visa_form_change_audit(assignment_id,visa_case_id,actor_id,action,before_json,after_json,created_at) VALUES(?,?,?,?,?,?,?)', (assignment_id,case_id,self.user['id'],'cancel_assignment',json.dumps({'status':current['status']}),json.dumps({'status':'Cancelled'}),stamp))
        self.visa_information_collection_update_respondent_summary(respondent)
        return self.toast_redirect(self.visa_case_return_path(student_id,case_id,'info_collection','applicant-information-collection'),'已取消当前 Case 的表单分配；原始内容和审计记录保留。','success','Applicant Information')

    def visa_form_history_html(self, assignment_id):
        db = self.visa_forms_runtime()['db']
        original = db.one('SELECT * FROM visa_form_submission_originals WHERE assignment_id=?', (assignment_id,))
        changes = db.all('SELECT * FROM visa_form_change_audit WHERE assignment_id=? ORDER BY id DESC', (assignment_id,))
        if original:
            label = 'Student original submission' if original['source'] == 'student_submission' else 'Legacy effective baseline — 历史学生原始提交无法还原'
            original_html = f'<details><summary>{esc(label)}</summary><pre style="white-space:pre-wrap;overflow-wrap:anywhere">{esc(original["original_json"])}</pre></details>'
        else:
            original_html = '<p>Student original submission：尚未采集到原始提交；不会将当前后台数据标为学生原始答案。</p>'
        audits = ''.join(f'<details><summary>{esc(row["created_at"])} · {esc(row["action"])} · Staff {esc(row["actor_id"])}</summary><h4>Before</h4><pre style="white-space:pre-wrap;overflow-wrap:anywhere">{esc(row["before_json"])}</pre><h4>After</h4><pre style="white-space:pre-wrap;overflow-wrap:anywhere">{esc(row["after_json"])}</pre></details>' for row in changes)
        return '<section class="panel"><h3>Original / Current / Audit</h3><p>上方表单为 Current effective value；保存修改时追加审计记录。</p>' + original_html + audits + '</section>'
