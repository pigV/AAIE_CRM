"""Case-local checklist placement, distinct from official document-type authority.

No physical file changes. Initial independent upload bindings remain immutable;
subsequent corrections are read from the retained document audit.
"""
import json


def _rows(conn, sql, args=()):
    cursor=conn.execute(sql,args);names=[c[0] for c in cursor.description]
    return [dict(zip(names,row)) for row in cursor]


def _tables(conn):
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _links(conn,case_id,source_id):
    cols={r[1] for r in conn.execute('PRAGMA table_info(visa_case_document_links)')}
    expr=('coalesce(student_document_id,document_id)' if {'student_document_id','document_id'}<=cols
          else 'student_document_id' if 'student_document_id' in cols else 'document_id')
    where=" AND COALESCE(status,'Linked')='Linked'" if 'status' in cols else ''
    if 'used_in_this_case' in cols:where+=' AND used_in_this_case=1'
    return _rows(conn,'SELECT rowid AS assignment_rowid,* FROM visa_case_document_links WHERE visa_case_id=? AND '+expr+'=?'+where,(case_id,source_id)),cols,expr


def latest_assignment(conn,case_id,document_id):
    row=conn.execute("SELECT after_json FROM visa_document_audit WHERE visa_case_id=? AND document_id=? AND action='material_item_assigned' ORDER BY id DESC LIMIT 1",(case_id,document_id)).fetchone()
    return json.loads(row[0])['target_item_id'] if row else None


def material_context(conn,case_id,*,document_id=None,source_id=None):
    present=_tables(conn)
    if 'visa_checklist_items' not in present:return {}
    items=_rows(conn,'SELECT * FROM visa_checklist_items WHERE visa_case_id=? ORDER BY id',(case_id,))
    if not items:return {}
    options=[{'id':i['id'],'label':i.get('material_name') or i.get('title') or i.get('document_type') or str(i['id']),
              'category':i.get('category') or ''} for i in items]
    ids=set()
    if source_id and 'visa_case_document_links' in present:
        links,_,_=_links(conn,case_id,source_id)
        for row in links:
            role=str(row.get('document_role') or '')
            if role.startswith('material_item:') and role.split(':',1)[1].isdigit():ids.add(int(role.split(':',1)[1]))
    elif document_id and 'visa_only_material_uploads' in present:
        upload=conn.execute('SELECT item_id FROM visa_only_material_uploads WHERE visa_case_id=? AND document_id=?',(case_id,document_id)).fetchone()
        if upload:
            corrected=latest_assignment(conn,case_id,document_id)
            if corrected is None:
                # Compatibility with earlier audited result-type corrections.
                from visa_material_reclassification import independent_material_rows
                ids={i['id'] for i in items if any(r['id']==document_id for r in independent_material_rows(conn,case_id,i['id']))}
            else:ids={int(corrected)}
    valid={o['id']:o for o in options};ids &= valid.keys()
    if not ids:return {}
    selected=next(iter(ids)) if len(ids)==1 else None
    return {'material_item_options':options,'material_item_id':selected,
            'material_item_label':valid[selected]['label'] if selected else ('多项材料关联，请核对' if ids else ''),
            'material_item_ids':sorted(ids)}


def placement_document_category(conn,case_id,target_id,current_category,*,document_id):
    """A combined grant/refusal slot never decides an official result type."""
    if isinstance(target_id,bool) or not str(target_id).isdigit() or int(target_id)<1:
        raise ValueError('请选择当前案件内的有效材料项。')
    rows=_rows(conn,'SELECT * FROM visa_checklist_items WHERE visa_case_id=? AND id=?',(case_id,int(target_id)))
    if not rows:raise ValueError('材料项不存在或不属于当前 Visa Case。')
    item=rows[0]
    if item.get('category')!='Visa Result / S56':return None
    title=item.get('material_name') or item.get('document_type') or ''
    meta=_rows(conn,'SELECT * FROM visa_document_metadata WHERE document_id=?',(document_id,))
    selected=conn.execute("SELECT after_json FROM visa_document_audit WHERE visa_case_id=? AND document_id=? AND action='document_category_selected' ORDER BY id DESC LIMIT 1",(case_id,document_id)).fetchone()
    selected=json.loads(selected[0]) if selected else {}
    from visa_document_metadata import LEGACY
    sources=_rows(conn,'SELECT s.document_type FROM visa_case_documents d JOIN student_documents s ON s.id=d.student_document_id WHERE d.id=? AND d.visa_case_id=?',(document_id,case_id))
    confirmed=(bool(meta and meta[0].get('manual_category')==current_category)
        or (selected.get('confirmed') is True and selected.get('category')==current_category)
        or bool(sources and LEGACY.get(sources[0]['document_type'])==current_category and current_category!='other'))
    if 'Grant' in title and 'Refusal' in title:
        if current_category not in {'grant_letter','refusal_letter'} or not confirmed:
            raise ValueError('请先在签证结果上传确认页核实下签信或拒签信类型，再选择此材料项。')
        return current_category
    from visa_document_metadata import CATEGORIES
    reverse={label:key for key,label in CATEGORIES.items()}
    reverse.update({'IMMI Acknowledge Letter':'immi_acknowledgment','IMMI Acknowledgement Letter':'immi_acknowledgment'})
    selected=reverse.get(title)
    if selected in {'grant_letter','refusal_letter'} and (selected!=current_category or not confirmed):
        raise ValueError('请先通过签证结果流程核实下签或拒签类型；移动材料项不会自动确认签证结果。')
    return selected


def assign_material_item(conn,case_id,document_id,target_id,*,actor_user_id,occurred_at):
    from visa_document_metadata import _document
    if isinstance(target_id,bool) or not str(target_id).isdigit() or int(target_id)<1:
        raise ValueError('请选择当前案件内的有效材料项。')
    target_id=int(target_id);doc=_document(conn,case_id,document_id,True)
    case=_rows(conn,'SELECT * FROM visa_cases WHERE id=?',(case_id,))[0]
    items=_rows(conn,'SELECT * FROM visa_checklist_items WHERE visa_case_id=? AND id=?',(case_id,target_id))
    if not items:raise ValueError('材料项不存在或不属于当前 Visa Case。')
    target=items[0];source=doc.get('student_document_id')
    context=material_context(conn,case_id,document_id=document_id,source_id=source)
    old=set(context.get('material_item_ids',[]))
    if not old:raise ValueError('文件没有当前有效的材料关联，请从上传确认页归类。')
    if old=={target_id}:return
    previous_links=[]
    if source:
        raw=_rows(conn,'SELECT * FROM student_documents WHERE id=?',(source,))
        if (not raw or str(raw[0].get('student_id'))!=str(case.get('student_id')) or not raw[0].get('is_active')
                or str(raw[0].get('document_status') or 'active').lower() in {'pending','deleted','archived','voided','inactive'}):
            raise ValueError('文件不属于当前学生，或已经失效。')
        links,cols,expr=_links(conn,case_id,source)
        links=[r for r in links if str(r.get('document_role') or '').startswith('material_item:')]
        previous_links=[dict(r) for r in links]
        # An explicitly selected historical target may already own the unique role.
        # Reuse that identity, while retaining the old placement as Removed history.
        existing_target=_rows(conn,'SELECT rowid AS assignment_rowid,* FROM visa_case_document_links WHERE visa_case_id=? AND '+expr+'=? AND document_role=?',
            (case_id,source,f'material_item:{target_id}'))
        chosen=existing_target[0] if existing_target else links[0]
        if existing_target and all(r['assignment_rowid']!=chosen['assignment_rowid'] for r in previous_links):
            previous_links.append(dict(chosen))
        values={'document_role':f'material_item:{target_id}','document_category':target.get('category') or '',
                'document_type':target.get('document_type') or target.get('material_name') or '',
                'status':'Linked','used_in_this_case':1,'updated_at':occurred_at}
        values={k:v for k,v in values.items() if k in cols}
        for row in links:
            if row['assignment_rowid']==chosen['assignment_rowid']:continue
            if not {'status','used_in_this_case'}<=cols:raise ValueError('材料关联结构无法保留历史，请联系管理员。')
            conn.execute("UPDATE visa_case_document_links SET status='Removed',used_in_this_case=0 WHERE rowid=?",(row['assignment_rowid'],))
        conn.execute('UPDATE visa_case_document_links SET '+','.join(k+'=?' for k in values)+' WHERE rowid=?',(*values.values(),chosen['assignment_rowid']))
    # The enclosing metadata savepoint also rolls back this audit and all counts.
    conn.execute('INSERT INTO visa_document_audit(visa_case_id,document_id,action,before_json,after_json,actor_user_id,occurred_at) VALUES(?,?,?,?,?,?,?)',
        (case_id,document_id,'material_item_assigned',json.dumps({'item_ids':sorted(old),'links':previous_links}),json.dumps({'target_item_id':target_id}),actor_user_id,occurred_at))
    from visa_material_reclassification import _set_status,independent_material_rows
    for item_id in old|{target_id}:
        if source:
            qualified=('coalesce(l.student_document_id,l.document_id)' if {'student_document_id','document_id'}<=cols else 'l.'+expr)
            where=" AND COALESCE(l.status,'Linked')='Linked'" if 'status' in cols else ''
            if 'used_in_this_case' in cols:where+=' AND l.used_in_this_case=1'
            active=conn.execute('SELECT 1 FROM visa_case_document_links l JOIN student_documents d ON d.id='+qualified+
                " WHERE l.visa_case_id=? AND l.document_role=? AND d.is_active=1 AND lower(coalesce(d.document_status,'active')) NOT IN ('pending','deleted','archived','voided','inactive')"+where+' LIMIT 1',(case_id,f'material_item:{item_id}')).fetchone()
        else:active=independent_material_rows(conn,case_id,item_id)
        _set_status(conn,case_id,item_id,bool(active))
