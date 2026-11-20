"""Explicit Higher Education transaction ports, with exact cycle ownership."""
from functools import wraps
from inspect import signature

from .commands import Mutation
from .contracts import Subject
from .domain import run_domain_write
from .runtime import enabled


def _cycle(connection,cycle_id):
    from higher_education_repository import HigherEducationValidationError
    cursor=connection.execute('SELECT * FROM application_cycles WHERE id=?',(cycle_id,))
    row=cursor.fetchone()
    if row is None:raise HigherEducationValidationError('higher education cycle is required')
    cycle=dict(zip((column[0] for column in cursor.description),row))
    columns={row[1] for row in connection.execute('PRAGMA table_info(students)')}
    active=" AND coalesce(deleted_at,'')=''" if 'deleted_at' in columns else ''
    if not cycle.get('student_id') or not connection.execute(
            'SELECT 1 FROM students WHERE student_id=?'+active,(cycle['student_id'],)).fetchone():
        raise HigherEducationValidationError('student is missing or deleted')
    return cycle


PORTS={
    'create_plan':('plan_created','he_plan',None,'cycle_id'),
    'add_plan_item':('plan_item_added','he_plan_item',None,'plan_id'),
    'confirm_plan':('plan_confirmed','he_plan','plan_id','plan_id'),
    'remove_draft_plan_item':('plan_item_removed','he_plan_item','item_id','item_id'),
    'create_application':('application_submitted','he_application',None,'cycle_id'),
    'record_offer':('offer_recorded','he_offer',None,'application_id'),
    'accept_offer':('offer_accepted','he_offer','offer_id','offer_id'),
    'link_coe':('coe_linked','he_coe_link',None,'application_id'),
}


def synchronized_higher_education_write(function):
    event,kind,source_parameter,owner_parameter=PORTS[function.__name__]
    event_type='higher_education.'+event;sig=signature(function)
    @wraps(function)
    def wrapped(repository,*args,**kwargs):
        connection=repository.conn
        if not enabled(connection):return function(repository,*args,**kwargs)
        params=sig.bind(repository,*args,**kwargs).arguments
        parent=None;application=None
        if owner_parameter=='cycle_id':cycle_id=int(params['cycle_id'])
        else:
            if owner_parameter=='item_id':
                row=connection.execute('''SELECT p.application_cycle_id,p.id FROM higher_education_plan_items i
                    JOIN higher_education_plans p ON p.id=i.plan_id WHERE i.id=?''',(params['item_id'],)).fetchone()
            elif owner_parameter in {'application_id','offer_id'}:
                if owner_parameter=='offer_id':
                    row=connection.execute('''SELECT a.application_cycle_id,i.plan_id,a.id FROM higher_education_offers o
                        JOIN higher_education_applications a ON a.id=o.application_id
                        LEFT JOIN higher_education_plan_items i ON i.id=a.source_plan_item_id WHERE o.id=?''',(params['offer_id'],)).fetchone()
                else:row=connection.execute('''SELECT a.application_cycle_id,i.plan_id,a.id FROM higher_education_applications a
                    LEFT JOIN higher_education_plan_items i ON i.id=a.source_plan_item_id WHERE a.id=?''',(params['application_id'],)).fetchone()
                if row is not None:application=int(row[2])
            else:row=connection.execute('SELECT application_cycle_id,id FROM higher_education_plans WHERE id=?',(params['plan_id'],)).fetchone()
            if row is None:
                from higher_education_repository import HigherEducationValidationError
                raise HigherEducationValidationError('higher education plan or item does not exist')
            cycle_id,parent=int(row[0]),int(row[1]) if row[1] is not None else None
        cycle=_cycle(connection,cycle_id)
        if params.get('source_plan_item_id') is not None:
            source_plan=connection.execute('''SELECT i.plan_id,p.application_cycle_id FROM higher_education_plan_items i
                JOIN higher_education_plans p ON p.id=i.plan_id WHERE i.id=?''',(params['source_plan_item_id'],)).fetchone()
            if source_plan and int(source_plan[1])==cycle_id:parent=int(source_plan[0])
        def capture(view):
            plans=tuple(tuple(row) for row in view.execute(
                'SELECT * FROM higher_education_plans WHERE application_cycle_id=? ORDER BY id',(cycle_id,)))
            items=tuple(tuple(row) for row in view.execute('''SELECT i.* FROM higher_education_plan_items i
                JOIN higher_education_plans p ON p.id=i.plan_id WHERE p.application_cycle_id=? ORDER BY i.id''',(cycle_id,)))
            applications=tuple(tuple(row) for row in view.execute(
                'SELECT * FROM higher_education_applications WHERE application_cycle_id=? ORDER BY id',(cycle_id,)))
            offers=tuple(tuple(row) for row in view.execute(
                'SELECT * FROM higher_education_offers WHERE application_cycle_id=? ORDER BY id',(cycle_id,)))
            coe=tuple(tuple(row) for row in view.execute('''SELECT l.* FROM higher_education_coe_links l
                JOIN higher_education_applications a ON a.id=l.application_id WHERE a.application_cycle_id=? ORDER BY l.id''',(cycle_id,)))
            return plans,items,applications,offers,coe
        def describe(view,result,before,after):
            current=_cycle(view,cycle_id)
            if str(current['student_id'])!=str(cycle['student_id']):
                raise ValueError('higher education cycle ownership changed during save')
            source=Subject(kind,str(params[source_parameter] if source_parameter else result))
            affected=(
                Subject('application_cycle',str(cycle_id)),Subject('student',str(current['student_id'])),
                Subject('module','higher_education'),Subject('dashboard','workflow'),Subject('dashboard','overview'))
            if parent is not None:affected+= (Subject('he_plan',str(parent)),)
            if application is not None:affected+= (Subject('he_application',str(application)),)
            if params.get('source_plan_item_id') is not None:
                affected+=(Subject('he_plan_item',str(params['source_plan_item_id'])),)
            if params.get('coe_oshc_id') is not None:affected+=(Subject('coe',str(params['coe_oshc_id'])),)
            return Mutation(event_type,source,affected)
        actor=params.get('actor_user_id')
        return run_domain_write(connection,event_type=event_type,actor_kind='user' if actor is not None else 'system',
            actor_ref=str(actor) if actor is not None else 'higher_education_domain',capture=capture,
            operation=lambda view:function(type(repository)(view),*args,**kwargs),describe=describe)
    return wrapped
