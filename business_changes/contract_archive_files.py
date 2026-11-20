"""Prepare generated PDF bytes before the final contract transaction."""
import os
from pathlib import Path
import uuid

from .files import persist_verified_file


def prepare_pdf(g,student,payload,document_type,original_name,audit_action,owned):
    destination,version=g['next_version_path'](g['nas'].category_folder(student,'Applications'),original_name)
    item={'path':destination,'version':version,'document_type':document_type,'size':len(payload),
          'audit_action':audit_action,'scratch':[],'promoted':[]}
    def save(path,digest):
        item['sha256']=digest;owned.append(item)
        key='archive_'+uuid.uuid4().hex
        for size in ('thumb','large'):
            target=g['preview_target'](key,size);item['scratch'].append(target)
            rendered=Path(g['make_preview'](key,path.name,payload,'application/pdf',size=size))
            if rendered.resolve()!=target.resolve() or not target.is_file():raise ValueError('归档预览未保存到预期位置。')
        return item
    return persist_verified_file(destination,payload,save)


def insert_pdf(g,view,student_id,item,user,stamp):
    path=item['path']
    did=int(view.execute('''INSERT INTO student_documents(student_id,folder_category,document_type,original_name,
        stored_name,file_path,version,file_size,uploaded_by,uploaded_at,is_active,document_status)
        VALUES(?,'Applications',?,?,?,?,?,?,?,?,1,'active')''',
        (student_id,item['document_type'],path.name,path.name,str(path),item['version'],item['size'],
         user['id'] if user else None,stamp)).lastrowid)
    for size,source in zip(('thumb','large'),item['scratch']):
        target=g['preview_target'](did,size);os.link(source,target);item['promoted'].append(target)
    view.execute('UPDATE student_documents SET preview_path=? WHERE id=?',(str(g['preview_target'](did,'thumb')),did))
    import json
    g['db'].audit(user,item['audit_action'],'Applications',did,str(path),new_value=json.dumps({
        'document_type':item['document_type'],'original_name':path.name,'stored_name':path.name,'version':item['version']},
        ensure_ascii=False),student_id=student_id)
    return {'document_id':did,'path':str(path),'version':item['version'],'file_name':path.name}


def cleanup(items,*,committed):
    failed=[]
    for item in reversed(items):
        paths=item['scratch'] if committed else [*item['scratch'],*item['promoted'],item['path']]
        for path in reversed(paths):
            try:Path(path).unlink(missing_ok=True)
            except OSError:failed.append(str(path))
    if failed:raise RuntimeError('归档缓存清理未完成：'+'；'.join(failed))
