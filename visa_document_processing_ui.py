"""Escaped, server-rendered controls for case-owned metadata and processing dates."""
from html import escape

FIELDS = {'acknowledge_sent_on':'Acknowledge Sent On Date', 'health_exam_date':'Health Exam Date',
          'grant_sent_on':'Grant Sent On Date', 'medical_completed_date':'Medical Completed Date'}

def _form(case_id, csrf, action, body):
    confirm = ' onsubmit="return confirm(\'Delete this document from the active Case? The original file is retained.\')"' if action == 'archive-document' else ''
    return f'<form method="post" action="/visa-management/cases/{int(case_id)}/workspace" class="stack-form"{confirm}><input type="hidden" name="csrf" value="{escape(csrf,quote=True)}"><input type="hidden" name="action" value="{action}">{body}</form>'

def _options(categories, selected):
    return ''.join(f'<option value="{escape(k,quote=True)}"'+(' selected' if k==selected else '')+f'>{escape(v)}</option>' for k,v in categories.items())

def document_edit_html(case_id, csrf, document):
    from visa_document_metadata import CATEGORIES
    d=document; did=int(d['id'])
    label=CATEGORIES.get(d.get('effective_category'),d.get('effective_category') or 'Not Detected')
    source='Manual / 人工' if d.get('manual_override') else 'Auto / 自动'
    badges=f'<div class="visa-document-badges"><span class="status-pill">{escape(label)}</span><span class="status-pill">{source}</span></div>'
    if d.get('archived_at'):return badges
    hidden=f'<input type="hidden" name="document_id" value="{did}">'
    edit=hidden+f'<label>Display Name / 显示名称<input name="display_name" required maxlength="240" value="{escape(d.get("display_name") or d.get("original_name") or "",quote=True)}"></label><label>Current Category / 当前分类<select name="category">'+_options(CATEGORIES,d.get('effective_category'))+'</select></label><button>Save / 保存</button>'
    detected=CATEGORIES.get(d.get('auto_category'),d.get('auto_category') or 'Not Detected')
    evidence=f'<details class="visa-document-audit"><summary>Details / Audit</summary><p>Auto Detected Category: {escape(str(detected))}</p></details>'
    delete=_form(case_id,csrf,'archive-document',hidden+'<button class="danger" type="submit">Delete / 删除</button>')
    return badges+'<details class="visa-document-edit"><summary>Edit / 编辑</summary>'+_form(case_id,csrf,'edit-document',edit)+_form(case_id,csrf,'reclassify',hidden+'<button>Run automatic classification / 重新自动识别</button>')+evidence+delete+'</details>'

def processing_dates_html(case_id, csrf, summary, documents):
    cards=''.join('<p>'+label+': <strong>'+('Pending' if summary.get(key) is None else str(summary[key])+' days')+'</strong></p>' for key,label in [('total_processing_days','Total Processing Days'),('medical_completed_to_grant_days','Medical Completed to Grant Days')])
    validation=''.join('<p role="alert">'+escape(str(v))+'</p>' for v in summary.get('validation',[]))
    fields=[]
    for key,label in FIELDS.items():
        state=summary.get('fields',{}).get(key,{})
        control=f'<label>{label}<input type="date" name="{key}" value="{escape(str(state.get("value") or ""),quote=True)}"></label><p>{escape(str(state.get("status") or "Not Detected"))} · {escape(str(state.get("source") or ""))}</p>'
        if key!='medical_completed_date':
            control+=f'<label>Source for {label}<select name="source_{key}"><option value="">Manual entry / current source</option>'
            for d in documents:
                if d.get('archived_at'):continue
                selected=' selected' if d['id']==state.get('document_id') else ''
                control+=f'<option value="{int(d["id"])}"{selected}>{escape(str(d.get("display_name") or d.get("original_name") or d["id"]))}</option>'
            control+='</select></label>'
        fields.append(control)
    return '<section class="panel" id="visa-processing-dates"><h2>Processing speed</h2><p>Calendar days. Grant Sent On is separate from Date of Grant. Medical Completed is entered by Staff.</p><p>Health Exam Date uses only an explicitly completed examination date. Manual corrections are retained.</p>'+cards+validation+_form(case_id,csrf,'correct-processing-dates',''.join(fields)+'<button>Save processing dates</button>')+'</section>'

def keyword_rules_html(case_id, csrf, rules):
    from visa_document_metadata import CATEGORIES
    forms=[]
    for rule in [*rules,{}]:
        rid=rule.get('id','')
        body=f'<input type="hidden" name="rule_id" value="{rid}"><label>Keyword<input name="keyword" required maxlength="240" value="{escape(str(rule.get("keyword") or ""),quote=True)}"></label><label>Target category<select name="category">'+_options(CATEGORIES,rule.get('category'))+'</select></label>'+f'<label>Priority<input type="number" name="priority" value="{int(rule.get("priority",100))}"></label><label>Active<select name="active"><option value="1"'+(' selected' if rule.get('active',True) else '')+'>Active</option><option value="0"'+(' selected' if not rule.get('active',True) else '')+f'>Inactive</option></select></label><button>{"Save rule" if rid else "Add rule"}</button>'
        forms.append(_form(case_id,csrf,'save-keyword-rule',body))
    return '<section class="panel"><details><summary>Filename keyword rules</summary><p>Rules apply to Visa documents. Manual categories are retained when rules change.</p>'+''.join(forms)+'</details></section>'
