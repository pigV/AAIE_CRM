"""Project unambiguous, source-grounded program claims to summary fields."""
from copy import deepcopy
import argparse
import json
from pathlib import Path
from local_document_lab.evidence import compact, validate_result


def project_program_fields(result):
    result=deepcopy(result)
    fields=result['fields'];issues=result['issues'];programs=result['programs']
    language=[p for p in programs if p['kind']=='language']
    main=[p for p in programs if p['kind']=='main']

    def merge(field,claim):
        if not claim or not claim.get('value') or claim.get('status')!='supported_needs_review':return
        old=fields.get(field,{})
        if old.get('value') and compact(old['value'])!=compact(claim['value']):
            issues.append(field+'_conflict')
            fields[field]={'value':None,'status':'needs_review','reason':'summary_program_conflict'}
        elif not old.get('value'):
            fields[field]={**claim,'derived_from_program':True}

    if len(language)==1:
        # Reuse only source-grounded dates with an exact matching institution.
        school=language[0]['fields'].get('school',{})
        summary_school=fields.get('language_school',{})
        if (school.get('value') and summary_school.get('value')
                and compact(school['value'])==compact(summary_school['value'])):
            for source,target in (('language_start_date','course_start_date'),
                                  ('language_end_date','course_end_date')):
                claim=fields.get(source,{})
                old=language[0]['fields'].get(target,{})
                if claim.get('status')=='supported_needs_review' and claim.get('value') and not old.get('value'):
                    language[0]['fields'][target]={**claim,'derived_from_summary':True}
        for target,source in (('language_school','school'),('language_start_date','course_start_date'),
                              ('language_end_date','course_end_date')):
            merge(target,language[0]['fields'].get(source))
    elif len(language)>1:
        issues.append('multiple_language_programs')
    elif programs and fields.get('language_school',{}).get('value'):
        issues.append('language_school_without_language_program')
    schools={compact(p['fields']['school']['value']):p['fields']['school'] for p in main
             if p['fields'].get('school',{}).get('value')}
    if len(schools)==1:merge('main_school',next(iter(schools.values())))
    elif len(schools)>1:issues.append('multiple_main_schools')
    # Partial claims can become a complete interval only after projection.
    def check_order(values,prefix,where):
        start,end=prefix+'_start_date',prefix+'_end_date'
        if (values.get(start,{}).get('value') and values.get(end,{}).get('value')
                and values[start]['value']>values[end]['value']):
            issues.append(where+prefix+'_dates_reversed_after_projection')
            for field in (start,end):
                values[field]={**values[field],'value':None,'status':'needs_review','reason':'dates_reversed'}
    for prefix in ('course','language','welfare'):check_order(fields,prefix,'')
    for index,program in enumerate(programs):check_order(program['fields'],'course',f'program_{index}:')
    result['requires_human_review']=True
    return result


def main():
    parser=argparse.ArgumentParser()
    for key in ('prediction','packet','output'):parser.add_argument('--'+key,required=True)
    args=parser.parse_args()
    prediction=json.loads(Path(args.prediction).read_text());packet=json.loads(Path(args.packet).read_text())
    text=prediction['raw_response'];raw=json.loads(text[text.index('{'):text.rindex('}')+1])
    selected=[b for b in packet['blocks'] if b['id'] in prediction['selected_block_ids']]
    prediction['result']=project_program_fields(validate_result(raw,selected))
    prediction['projection_version']=1
    Path(args.output).write_text(json.dumps(prediction,ensure_ascii=False,indent=2))
    print(json.dumps({'status':'projected_requires_review','issues':prediction['result']['issues']}))

if __name__=='__main__':main()
