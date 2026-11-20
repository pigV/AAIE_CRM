"""Read pending upload facts in the caller's snapshot, without touching files."""
from .upload_review import MODULES


def pending_reviews(conn,student_id,*,can_view):
    modules=tuple(module for module in MODULES if can_view(module))
    result={'batches':0,'pending':0,'failed':0,'rows':[]}
    if not modules:return result
    cursor=conn.execute(f'''SELECT s.id,s.session_token,s.module_key,s.uploaded_at,
        count(*) pending,sum(CASE WHEN coalesce(i.error_message,'')!='' THEN 1 ELSE 0 END) failed
        FROM upload_sessions s JOIN upload_review_items i ON i.upload_session_id=s.id
          AND i.student_id=s.student_id AND i.module_key=s.module_key
        WHERE s.student_id=? AND i.status='NeedsReview'
          AND s.module_key IN ({','.join('?' for _ in modules)})
        GROUP BY s.id,s.session_token,s.module_key,s.uploaded_at
        ORDER BY s.uploaded_at DESC,s.id DESC''',(str(student_id),*modules))
    for sid,token,module,uploaded_at,pending,failed in cursor:
        result['batches']+=1;result['pending']+=int(pending);result['failed']+=int(failed or 0)
        if len(result['rows'])<10:
            result['rows'].append({'id':sid,'token':token,'module':module,'uploaded_at':uploaded_at,
                                   'pending':int(pending),'failed':int(failed or 0)})
    return result
