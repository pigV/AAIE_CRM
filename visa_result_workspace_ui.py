"""Result-workspace presentation only. No persistence or state transitions."""
from collections import OrderedDict
from html import escape
from urllib.parse import quote, urlencode

GROUPS = (
    ('lodgement', '递交记录', ('Draft', 'Receipt', 'IMMI Acknowledgment Letter', 'IMMI Acknowledgement Letter')),
    ('s56', '补件 / S56', ('Supplement',)),
    ('medical', '体检 / Health Examination', ('Health Examination',)),
    ('bridging', '过桥签证 / Bridging Visa', ('Bridging Visa',)),
    ('outcome', '签证结果文件 / Grant · Refusal', ('IMMI Grant Letter / IMMI Refusal Letter',)),
    ('other', '其他结果文件', ()),
)


def result_stage(case, result):
    """Do not infer lodgement/medical completion from an attachment's presence."""
    case, result = dict(case), dict(result or {})
    value = str(result.get('effective_result') or 'Pending')
    if result.get('conflict'):
        return dict(key='conflict', label='结果待核对', action='核对冲突结果与原始文件',
                    target='visa-result-dates', hint='检测到结果证据冲突，请核对来源后处理。', tone='warning')
    if value in {'Granted', 'Refused', 'Withdrawn', 'Closed'}:
        labels={'Granted':'已获签','Refused':'已拒签','Withdrawn':'已撤回','Closed':'已关闭'}
        return dict(key='result', label=labels[value], action='核对官方结果及有效期', target='visa-result-dates',
                    hint='结果记录已更新。请核对官方信件、日期与有效期；收到文件不等于完成正式确认。', tone='info')
    if str(case.get('status') or '').lower() in {'s56 requested','s56 requested / 补件','s56'}:
        return dict(key='followup', label='补件跟进中', action='查看补件通知与回复材料', target='visa-result-group-s56',
                    hint='按当前案件的补件要求处理，保留通知与回复文件。', tone='warning')
    if case.get('lodgement_recorded_at'):
        return dict(key='waiting', label='等待结果', action='查看递交记录及后续通知', target='visa-result-group-lodgement',
                    hint='已记录正式递交。收到补件、体检或结果通知后，在对应分组处理。', tone='info')
    return dict(key='prepare', label='递交准备', action='核对并确认正式递交', target='visa-result-lodgement',
                hint='上传回执与正式递交分别记录。请按实际办理情况核对递交信息。', tone='info')


def result_group(name):
    for key, _label, names in GROUPS:
        if any(name == candidate or name.startswith(candidate + ' / ') for candidate in names):
            return key
    return 'other'


def result_processing_display(summary, result):
    """Keep calculation/audit data intact; Pending is not a completed duration."""
    display=dict(summary)
    if result == 'Pending':
        display.update(total_processing_days=None,medical_completed_to_grant_days=None)
    return display


def official_review_url(case):
    base=f'/visa-management/cases/{int(case["id"])}/workspace'
    return base+('#visa-result' if dict(case).get('student_id') else '?visa_tab=visa_result#visa-official-review')


def render_result_materials(handler, student_id, case, rows):
    h=handler; cid=int(case['id']); sid=quote(str(student_id)); form_id=f'visa-material-save-{cid}-result'
    base=(f'/students/{sid}/visa-cases/{cid}' if student_id else f'/visa-management/cases/{cid}/applicant')
    batch=base+'/material-batch-upload?visa_tab=visa_result'
    grouped=OrderedDict((key,[]) for key,_label,_names in GROUPS)
    for item in rows: grouped[result_group(str(item['material_name']))].append(item)
    groups=[]
    for key,label,_names in GROUPS:
        items=grouped[key]
        if key=='other' and not items:continue
        cards=[]; count=0
        for item in items:
            iid=int(item['id']); name=h.visa_material_name_label(item['material_name'])
            files=(h.visa_material_file_rows(cid,iid,item,student_id) if student_id else h.visa_only_material_file_rows(cid,iid))
            count+=len(files)
            files_html=h.visa_material_file_area_html(student_id,cid,item)
            if student_id:
                upload=f'''<details class="visa-result-upload"><summary>上传到此项</summary>
                  <form method="post" enctype="multipart/form-data" action="{base}/material-items/{iid}/files" data-result-upload>
                    <input type="hidden" name="csrf" value="{escape(h.csrf)}"><input type="hidden" name="visa_tab" value="visa_result">
                    <input type="hidden" name="return_anchor" value="visa-result-item-{iid}">
                    <label>选择{escape(name)}文件<input type="file" name="files" accept=".pdf,.doc,.docx,.jpg,.jpeg,.png,.heic" multiple required></label>
                    <button class="primary" type="submit">确认上传</button></form></details>'''
            else:
                upload=f'<a class="secondary button-link compact" href="{escape(batch+"&"+urlencode({"target_material_item":iid}))}" data-result-resume>上传到此项</a>'
            cards.append(f'''<article class="visa-result-item" id="visa-result-item-{iid}" data-result-item="{iid}">
              <div class="visa-result-item-head"><div><strong>{escape(name)}</strong><small>{escape(h.visa_material_owner_type_label(item['owner_type']))} · {len(files)} 个文件</small></div>
              <label>材料状态{h.visa_material_status_select_html(item,form_id)}</label></div>
              <div class="visa-result-item-actions">{upload}</div>
              <details class="visa-result-item-details" data-result-item-details><summary>查看文件与备注</summary>
                {files_html}<label>备注<textarea rows="2" name="notes_{iid}" form="{form_id}" aria-label="{escape(name)}备注">{escape(str(item['notes'] or ''))}</textarea></label>
                <small>{escape(h.visa_material_purpose_label(item['purpose']))} · {escape(h.visa_material_required_status_label(item['required_status']))}</small>
              </details></article>''')
        empty='<p class="visa-result-empty">当前案件没有此类材料项。按实际办理情况使用批量上传归类。</p>' if not cards else ''
        opened=' open' if key=='lodgement' else ''
        groups.append(f'''<details class="visa-result-group" id="visa-result-group-{key}" data-result-group="{key}"{opened}>
          <summary><strong>{label}</strong><span>{count} 个文件</span></summary>
          <div class="visa-result-group-body">{''.join(cards)}{empty}</div></details>''')
    return f'''<section class="visa-result-materials" id="visa-result-s56" data-result-materials>
      <div class="visa-result-heading"><div><h2>Visa Result / S56 / 签证结果</h2><p>按办理阶段整理文件。已收到不代表已审核或已确认结果。</p></div>
        <a class="secondary button-link" data-result-resume href="{escape(batch)}">Smart Batch Upload / 智能批量上传</a></div>
      <form id="{form_id}" method="post" action="{base}/material-items" data-result-save>
        <input type="hidden" name="csrf" value="{escape(h.csrf)}"><input type="hidden" name="visa_tab" value="visa_result">
        <input type="hidden" name="return_anchor" value="visa-result-s56"></form>
      {''.join(groups)}
      <div class="visa-result-save"><span data-result-save-status role="status" aria-live="polite">状态和备注需保存后生效</span><button class="primary" type="submit" form="{form_id}" data-result-save-button>保存材料状态</button></div>
    </section>'''


def render_result_workspace(case, result, materials_html, processing_html, lodgement_html='', case_documents=''):
    stage=result_stage(case,result)
    target=stage['target']
    if not dict(case).get('student_id') and target=='visa-result-lodgement':target='visa-result-case-documents'
    navigation=''.join(f'<a href="#visa-result-group-{key}" data-result-jump>{label}</a>' for key,label,_ in GROUPS if key!='other')
    extra=(f'<details class="visa-result-case-documents" id="visa-result-case-documents"><summary>案件文件与递交版本 / Prepared · IMMI Submitted</summary>{case_documents}</details>' if case_documents else '')
    return f'''<link rel="stylesheet" href="/assets/visa-material-workspace.css?v=1"><link rel="stylesheet" href="/assets/visa-result-workspace.css?v=1"><script src="/assets/visa-result-workspace.js?v=1" defer></script>
      <div class="visa-result-workspace" data-visa-result-workspace data-case-id="{int(case['id'])}" data-stage="{stage['key']}">
        <section class="visa-result-current visa-result-{stage['tone']}"><div><span>当前结果记录</span><h2>{escape(stage['label'])}</h2><p>{escape(stage['hint'])}</p></div><a class="primary button-link" href="#{target}" data-result-jump>{escape(stage['action'])}</a></section>
        <nav class="visa-result-nav" aria-label="结果页分区">{navigation}<a href="#visa-result-dates" data-result-jump>关键日期与结果核对</a></nav>
        <p class="visa-result-feedback" data-result-feedback role="status" aria-live="polite"></p>
        <button class="secondary compact" type="button" data-result-reset hidden>撤销当前区域未保存修改</button>
        <div class="visa-result-columns"><div class="visa-result-main"><div id="visa-result-lodgement">{lodgement_html}</div>{materials_html}{extra}</div>
          <aside class="visa-result-aside" id="visa-result-dates"><h2>关键日期与官方结果</h2>{processing_html}</aside></div>
        <p class="visa-result-legend">补件、体检和过桥签证按实际案件情况处理；没有文件不表示必缺。正式递交与官方结果继续分别确认。</p>
      </div>'''
