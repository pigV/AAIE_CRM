"""Explicit shared master changes and their actual Higher Education dependants."""
from functools import wraps
from inspect import signature

from .commands import Mutation
from .contracts import Subject
from .domain import run_domain_write
from .runtime import enabled


PORTS={
    'create_provider':('provider_created','he_provider','higher_education_providers',None),
    'review_provider':('provider_reviewed','he_provider','higher_education_providers','provider_id'),
    'update_university':('university_updated','he_provider','higher_education_providers','provider_id'),
    'link_pathway':('provider_relationship_created','he_provider_relationship','higher_education_provider_relationships',None),
    'create_pathway_combination':('pathway_created','he_pathway','higher_education_pathway_combinations',None),
    'add_pathway_segment':('pathway_segment_added','he_pathway_segment','higher_education_pathway_segments',None),
    'create_or_get_course':('course_created','he_course','higher_education_courses',None),
    'merge_course':('course_merged','he_course','higher_education_courses','duplicate_course_id'),
    'set_course_status':('course_status_changed','he_course','higher_education_courses','course_id'),
}


def _selector(name,params):
    if name=='create_provider':return 'normalized_name=?',(' '.join(str(params['name']).strip().casefold().split()),)
    if name in {'review_provider','update_university'}:return 'id=?',(params['provider_id'],)
    if name=='link_pathway':return 'university_id=? AND pathway_provider_id=?',(params['university_id'],params['pathway_provider_id'])
    if name=='create_pathway_combination':return 'university_id=? AND pathway_code=?',(params['university_id'],str(params['pathway_code']).strip())
    if name=='add_pathway_segment':return 'pathway_combination_id=?',(params['pathway_combination_id'],)
    if name=='create_or_get_course':return 'university_id=? AND normalized_name=? AND course_level=?',(
        params['university_id'],' '.join(str(params['course_name']).strip().casefold().split()),params['course_level'])
    if name=='merge_course':return 'id IN (?,?)',(params['duplicate_course_id'],params['canonical_course_id'])
    return 'id=?',(params['course_id'],)


def _dependants(connection,kind,rows,params):
    ids={int(row['id']) for row in rows}
    if kind=='he_course':
        column='course_id'
        ids.update(int(params[key]) for key in ('course_id','duplicate_course_id','canonical_course_id') if params.get(key) is not None)
    elif kind in {'he_pathway','he_pathway_segment'}:
        column='pathway_combination_id'
        if kind=='he_pathway_segment':ids={int(params['pathway_combination_id'])}
    else:
        providers={int(params[key]) for key in ('provider_id','university_id','pathway_provider_id') if params.get(key) is not None}
        if kind=='he_provider':providers.update(ids)
        if not providers:return ()
        marks=','.join('?' for _ in providers);values=tuple(sorted(providers))
        paths={int(row[0]) for row in connection.execute(
            f'''SELECT id FROM higher_education_pathway_combinations WHERE university_id IN ({marks})
                UNION SELECT pathway_combination_id FROM higher_education_pathway_segments WHERE provider_id IN ({marks})''',values+values)}
        return _referenced(connection,'provider_id',providers)+_referenced(connection,'pathway_combination_id',paths)
    return _referenced(connection,column,ids)


def _referenced(connection,column,ids):
    if not ids:return ()
    values=tuple(sorted(ids));marks=','.join('?' for _ in values);subjects=set()
    queries=(('he_plan_item',f'''SELECT i.id,p.id,p.application_cycle_id,c.student_id
        FROM higher_education_plan_items i JOIN higher_education_plans p ON p.id=i.plan_id
        JOIN application_cycles c ON c.id=p.application_cycle_id WHERE i.{column} IN ({marks})'''),
        ('he_application',f'''SELECT a.id,i.plan_id,a.application_cycle_id,c.student_id
        FROM higher_education_applications a JOIN application_cycles c ON c.id=a.application_cycle_id
        LEFT JOIN higher_education_plan_items i ON i.id=a.source_plan_item_id WHERE a.{column} IN ({marks})'''))
    if column in {'course_id','provider_id'}:
        child_column='teaching_provider_id' if column=='provider_id' else column
        queries+=(('he_application',f'''SELECT a.id,i.plan_id,a.application_cycle_id,c.student_id
            FROM higher_education_package_segments s JOIN higher_education_applications a ON a.id=s.application_id
            JOIN application_cycles c ON c.id=a.application_cycle_id
            LEFT JOIN higher_education_plan_items i ON i.id=a.source_plan_item_id WHERE s.{child_column} IN ({marks})'''),)
    for kind,query in queries:
        for row in connection.execute(query,values):
            subjects.update((Subject(kind,str(row[0])),Subject('application_cycle',str(row[2])),Subject('student',str(row[3]))))
            if row[1] is not None:subjects.add(Subject('he_plan',str(row[1])))
    if subjects:subjects.update((Subject('dashboard','workflow'),Subject('dashboard','overview')))
    return tuple(sorted(subjects))


def synchronized_higher_education_master(function):
    name=function.__name__;event,kind,table,source_parameter=PORTS[name];sig=signature(function)
    @wraps(function)
    def wrapped(repository,*args,**kwargs):
        connection=repository.conn
        if not enabled(connection):return function(repository,*args,**kwargs)
        params=sig.bind(repository,*args,**kwargs).arguments
        where,values=_selector(name,params)
        def capture(view):
            cursor=view.execute('SELECT * FROM '+table+' WHERE '+where+' ORDER BY id',values)
            columns=[column[0] for column in cursor.description]
            rows=[dict(zip(columns,row)) for row in cursor]
            return tuple(tuple(row.values()) for row in rows),_dependants(view,kind,rows,params)
        def describe(view,result,before,after):
            identity=params[source_parameter] if source_parameter else result[0] if name=='create_or_get_course' else result
            source=Subject(kind,str(identity))
            return Mutation('higher_education.'+event,source,
                (Subject('module','higher_education'),Subject('module','school_library'),*after[1]),previous_subjects=before[1])
        return run_domain_write(connection,event_type='higher_education.'+event,actor_kind='user',
            actor_ref=str(params['actor_user_id']),capture=capture,
            operation=lambda view:function(type(repository)(view),*args,**kwargs),describe=describe)
    return wrapped
