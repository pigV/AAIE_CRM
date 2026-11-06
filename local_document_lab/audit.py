"""Report candidate completeness and observable contradictions, not semantic truth."""
import re
from local_document_lab.evidence import compact, ground_claim

REQUIRED = {
    'coe':('provider','course','course_start_date','course_end_date'),
    'caaw':('provider','welfare_start_date','welfare_end_date'),
    'offer':('main_school',),
    'confirmation':('main_school',),
}


def audit_result(kind, result, blocks):
    required=list(REQUIRED.get(kind,()))
    if not isinstance(result,dict) or not isinstance(result.get('fields'),dict):
        return {'issues':['invalid_result'],'missing_fields':required,'candidate_complete':False,
                'semantic_verified':False,'production_ready':False}
    fields=result['fields'];programs=result.get('programs',[])
    issues=list(result.get('issues',[]))
    missing=[]
    if kind in ('offer','confirmation') and fields.get('language_school',{}).get('value'):
        required.extend(('language_start_date','language_end_date'))
    for field in required:
        if not fields.get(field,{}).get('value'):missing.append(field)

    def check_claim(field, claim, path):
        if not isinstance(claim,dict) or not claim.get('value'):return
        checked=ground_claim(field,claim,blocks)
        if (checked.get('value')!=claim['value'] or checked.get('page')!=claim.get('page')
                or checked.get('bbox')!=claim.get('bbox')):
            issues.append('invalid_evidence:'+path)
        quote=compact(claim.get('quote'))
        if field.startswith('welfare_') and 'welfare' not in quote and re.search(r'\bcourse\s+(?:start|end|commencement|completion)\b',quote):
            issues.append(field+'_uses_course_evidence')

    for field,claim in fields.items():check_claim(field,claim,field)
    for index,program in enumerate(programs):
        issues.extend('program_'+str(index)+':'+issue for issue in program.get('issues',[]))
        values=program.get('fields',{})
        for field,claim in values.items():check_claim(field,claim,'program_'+str(index)+'.'+field)
        if kind in ('offer','confirmation'):
            for field in ('school','course_start_date','course_end_date'):
                if not values.get(field,{}).get('value'):missing.append('program_'+str(index)+'.'+field)
    main_names={compact(p['fields']['school']['value']) for p in programs
                if p.get('kind')=='main' and p.get('fields',{}).get('school',{}).get('value')}
    main=fields.get('main_school',{}).get('value')
    if main_names and main and compact(main) not in main_names:issues.append('main_school_not_in_main_programs')
    language_names={compact(p['fields']['school']['value']) for p in programs
                    if p.get('kind')=='language' and p.get('fields',{}).get('school',{}).get('value')}
    language=fields.get('language_school',{}).get('value')
    if language_names and language and compact(language) not in language_names:issues.append('language_school_not_in_language_programs')
    if programs and language and not any(p.get('kind')=='language' for p in programs):
        issues.append('language_school_without_language_program')
    issues=sorted(set(issues))
    return {'issues':issues,'missing_fields':sorted(set(missing)),'candidate_complete':not issues and not missing,
            'semantic_verified':False,'production_ready':False}
