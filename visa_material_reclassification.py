"""Keep an explicit staff category correction in the same Case's checklist."""
import json

TITLES = {
    'health_examination': ('Health Examination',),
    'immi_acknowledgment': ('IMMI Acknowledgment Letter', 'IMMI Acknowledgement Letter'),
    'grant_letter': ('IMMI Grant Letter', 'IMMI Grant Letter / IMMI Refusal Letter'),
    'refusal_letter': ('IMMI Refusal Letter', 'IMMI Grant Letter / IMMI Refusal Letter'),
    'bridging_visa': ('Bridging Visa',),
}


def _items(conn, case_id):
    cursor=conn.execute('SELECT * FROM visa_checklist_items WHERE visa_case_id=?',(case_id,))
    return [dict(zip((c[0] for c in cursor.description),r)) for r in cursor]


def _target(items, category):
    targets=[item for item in items if item.get('category')=='Visa Result / S56'
             and (item.get('material_name') or item.get('document_type')) in TITLES.get(category,())]
    if len(targets)!=1:
        raise ValueError('当前案件没有唯一对应材料项，请先核对材料清单，再重新归类。')
    return targets[0]


def _set_status(conn, case_id, item_id, received):
    cursor=conn.execute('SELECT * FROM visa_checklist_items WHERE id=? AND visa_case_id=?',(item_id,case_id))
    row=cursor.fetchone()
    if row is None:return
    item=dict(zip((c[0] for c in cursor.description),row))
    state='uploaded' if received else 'missing'
    assignments={k:state for k in ('status','document_status') if k in item
                 and item[k] not in ({'reviewed','已审核'} if received else {'not_applicable','不适用'})}
    if assignments:
        conn.execute('UPDATE visa_checklist_items SET '+','.join(k+'=?' for k in assignments)+' WHERE id=? AND visa_case_id=?',
                     (*assignments.values(),item_id,case_id))


def independent_material_rows(conn, case_id, item_id, *, category_override=None):
    """Original upload binding is immutable; staff corrections are audited metadata."""
    cursor=conn.execute("""SELECT d.id,d.original_name,u.item_id,m.manual_category
        FROM visa_only_material_uploads u
        JOIN visa_case_documents d ON d.id=u.document_id AND d.visa_case_id=u.visa_case_id
        JOIN visa_cases c ON c.id=u.visa_case_id AND c.student_id IS NULL AND c.archived_at IS NULL
        LEFT JOIN visa_document_metadata m ON m.document_id=d.id
        WHERE u.visa_case_id=? AND d.archived_at IS NULL ORDER BY d.id""",(case_id,))
    rows=[dict(zip((c[0] for c in cursor.description),r)) for r in cursor]
    items=_items(conn,case_id);result=[]
    for row in rows:
        category=(category_override[1] if category_override and row['id']==category_override[0]
                  else row.get('manual_category'))
        effective_item=_target(items,category)['id'] if category in TITLES else row['item_id']
        if effective_item==int(item_id):result.append({'id':row['id'],'original_name':row['original_name']})
    return result


def sync_material_category(conn, case_id, document_id, category, *, actor_user_id, occurred_at):
    """Called inside metadata's atomic, authorized Case write; never copies files."""
    if category not in TITLES:
        return
    present = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if 'visa_checklist_items' not in present:
        return
    document = conn.execute('SELECT student_document_id FROM visa_case_documents WHERE id=? AND visa_case_id=?',
                            (document_id, case_id)).fetchone()
    if not document:return
    if not document[0]:
        if 'visa_only_material_uploads' not in present:return
        upload=conn.execute('SELECT item_id FROM visa_only_material_uploads WHERE visa_case_id=? AND document_id=?',
                            (case_id,document_id)).fetchone()
        if not upload:return
        items=_items(conn,case_id);target=_target(items,category)
        affected={item['id'] for item in items if any(r['id']==document_id for r in independent_material_rows(conn,case_id,item['id']))}
        if affected=={target['id']}:return
        for item_id in affected|{target['id']}:
            _set_status(conn,case_id,item_id,bool(independent_material_rows(conn,case_id,item_id,category_override=(document_id,category))))
        conn.execute('INSERT INTO visa_document_audit(visa_case_id,document_id,action,before_json,after_json,actor_user_id,occurred_at) VALUES(?,?,?,?,?,?,?)',
            (case_id,document_id,'material_reclassified',json.dumps({'retained_item_id':upload[0],'effective_item_ids':sorted(affected)}),
             json.dumps({'target_item_id':target['id'],'category':category}),actor_user_id,occurred_at))
        return
    if 'visa_case_document_links' not in present:return
    columns = {r[1] for r in conn.execute('PRAGMA table_info(visa_case_document_links)')}
    source_expr = 'coalesce(student_document_id,document_id)' if {'student_document_id','document_id'} <= columns else (
        'student_document_id' if 'student_document_id' in columns else 'document_id')
    cursor = conn.execute('SELECT rowid AS repair_rowid,* FROM visa_case_document_links WHERE visa_case_id=? AND '
        + source_expr + "=? AND document_category='Visa Result / S56' AND used_in_this_case=1 AND status='Linked'",
        (case_id, document[0]))
    links = [dict(zip((c[0] for c in cursor.description), r)) for r in cursor]
    # Draft/history/unused links must never become official result material.
    links = [r for r in links if r.get('document_type') not in {'Draft','Other'}
             and str(r.get('document_role') or '').startswith('material_item:')]
    if not links:
        return
    target = _target(_items(conn,case_id),category)
    if all(r['document_role']==f"material_item:{target['id']}" and r['document_type']==(target.get('document_type') or target.get('material_name')) for r in links):return
    old_ids = {int(r['document_role'].split(':')[1]) for r in links}
    for link in links:
        values = {'document_role': f'material_item:{target["id"]}',
                  'document_category': 'Visa Result / S56',
                  'document_type': target.get('document_type') or target.get('material_name'),
                  'updated_at': occurred_at}
        values = {k:v for k,v in values.items() if k in columns}
        conn.execute('UPDATE visa_case_document_links SET '+','.join(k+'=?' for k in values)+' WHERE rowid=?',
                     (*values.values(),link['repair_rowid']))
    qualified_source = 'coalesce(l.student_document_id,l.document_id)' if {'student_document_id','document_id'} <= columns else ('l.student_document_id' if 'student_document_id' in columns else 'l.document_id')
    for item_id in old_ids | {target['id']}:
        active = conn.execute('SELECT 1 FROM visa_case_document_links l JOIN student_documents d ON d.id='
            + qualified_source
            + " WHERE l.visa_case_id=? AND l.document_role=? AND l.status='Linked' AND l.used_in_this_case=1"
              " AND d.is_active=1 AND lower(coalesce(d.document_status,'active')) NOT IN ('deleted','archived','voided','inactive','pending') LIMIT 1",
            (case_id,f'material_item:{item_id}')).fetchone()
        _set_status(conn,case_id,item_id,bool(active))
    conn.execute('INSERT INTO visa_document_audit(visa_case_id,document_id,action,before_json,after_json,actor_user_id,occurred_at) VALUES(?,?,?,?,?,?,?)',
        (case_id,document_id,'material_reclassified',json.dumps(links),json.dumps({'target_item_id':target['id'],'category':category}),actor_user_id,occurred_at))
