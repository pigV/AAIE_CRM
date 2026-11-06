"""Resolve explicit language periods from named prose or same-row PDF labels.

A source string alone is insufficient: role, institution, page and label/value
positions must agree. Ambiguous layouts preserve the existing abstention.
"""
from copy import deepcopy
import re
from local_document_lab.evidence import compact, dates_in, ground_claim

LANGUAGE = re.compile(r'\b(?:english language|intensive english|high school preparation|iec)\b',re.I)
DATE = r'\d{1,2}\s+[A-Za-z]{3,9},?\s+\d{4}'
RANGE = re.compile(r'\bfrom\s+('+DATE+r')\s+to\s+('+DATE+r')',re.I)


def rectangle(block):
    box=block.get('bbox')
    if not isinstance(box,dict) or not all(isinstance(box.get(k),(int,float)) for k in ('l','r','t','b')):
        return None
    if box.get('coord_origin') not in ('BOTTOMLEFT','TOPLEFT'):
        return None
    if not isinstance(block.get('page'),int) or block['page']<1:return None
    return box



def names_language_period(prefix,name):
    text=compact(prefix);name=compact(name)
    # A complete named institution must begin the clause or directly follow "at".
    # Continuation with another proper-name word is not an exact institution match.
    suffix=r'(?=$|[\s]*[,(:;.]|\s+(?:for|from|runs|offers|provides|has|english|intensive|will|is|commencing|starting|begins|study|studies)\b)'
    # An explicitly named language clause can follow an admission/main-school clause.
    # Judge its own continuation, rather than rejecting a school word elsewhere.
    explicit=list(re.finditer(r'\b(?:english language|intensive english|high school preparation)'
                             r'(?:\s+(?:studies|course|program|programme|preparation))?'
                             r'\s+(?:at|provided by)\s+'+re.escape(name)
                             +r'(?=$|\s*[,(:;.]|\s+(?:(?:located|situated)\s+at|at|for|from|runs|will|is|commencing|starting|begins)\b)',text))
    if explicit:
        if len(explicit)!=1:return False
        tail=text[explicit[0].end():]
        if tail:
            # Between the named language institution and "from", allow only a
            # locative continuation. A following course must never donate dates.
            if not re.match(r'^\s*(?:[,(:-]\s*)?(?:(?:located|situated)\s+at|at)\s+',tail):
                return False
            if (len(tail)>180 or ';' in tail or re.search(
                    r'\b(?:main|secondary|primary|welfare|accommodation|homestay|holiday|payment|visa|if|may|then|before|after|followed|following|leading|program|programme|course|studies|training|diploma|degree)\b',tail)):
                return False
        return True
    named=re.search(r'(?:^|\bat\s+)'+re.escape(name)+suffix,text)
    if not named or ';' in text:return False
    without_name=text[:named.start()]+text[named.end():]
    if re.search(r'\b(?:main|secondary|primary|welfare|accommodation|homestay|holiday|payment|visa)\b',without_name):
        return False
    return bool(LANGUAGE.search(text))


def reject_language_dates(result,reason):
    for key in ('language_start_date','language_end_date'):result['fields'].pop(key,None)
    for program in result.get('programs',[]):
        if isinstance(program,dict) and program.get('kind')=='language':
            for key in ('course_start_date','course_end_date'):program.pop(key,None)
    result['_layout_resolution']={'basis':reason,'abstained':True}
    return result


def resolve_language_dates(raw, blocks):
    result=deepcopy(raw)
    if not isinstance(result,dict) or not isinstance(result.get('fields'),dict):return result
    school=result['fields'].get('language_school')
    grounded=ground_claim('language_school',school,blocks)
    if not grounded.get('value'):return result
    programs=result.get('programs',[])
    if not isinstance(programs,list):return result
    language=[p for p in programs if isinstance(p,dict) and p.get('kind')=='language']
    if len(language)>1:return result
    if language and language[0].get('school'):
        other=ground_claim('school',language[0]['school'],blocks)
        if other.get('value') and compact(other['value'])!=compact(grounded['value']):return result
    pairs=[]
    for block in blocks:
        text=block.get('text','')
        if block.get('kind') in ('table','table_cell') or not isinstance(block.get('page'),int):continue
        for sentence in re.split(r'(?<=[.!?])\s+',text):
            if compact(grounded['value']) not in compact(sentence) or not LANGUAGE.search(sentence):continue
            for match in RANGE.finditer(sentence):
                prefix=sentence[:match.start()]
                if (not names_language_period(prefix,grounded['value'])
                        or re.search(r'\b(?:if|may)\b|\bfor example\b',prefix,re.I)):continue
                start,end=dates_in(match[1]),dates_in(match[2])
                if len(start)==len(end)==1 and next(iter(start))<=next(iter(end)):
                    pairs.append((next(iter(start)),next(iter(end)),block,block,'named_language_period'))
    # If named prose has competing periods, do not choose a geometrical fallback.
    if not pairs:
        anchor=next((b for b in blocks if b['id']==grounded['block_id']),None)
        box=rectangle(anchor or {})
        if (box and anchor.get('kind') not in ('table','table_cell')
                and abs(box['t']-box['b'])<=24 and LANGUAGE.search(anchor['text'])
                and re.search(r'\bstart\s+date\b',anchor['text'],re.I)):
            def row(b):
                r=rectangle(b)
                return bool(r and b.get('page')==anchor['page'] and r['coord_origin']==box['coord_origin']
                            and abs((r['t']+r['b'])/2-(box['t']+box['b'])/2)<=3)
            ends=[b for b in blocks if row(b) and re.fullmatch(r'\s*(?:finish|end)\s+date\s*',b['text'],re.I)
                  and b['bbox']['l']>box['r']]
            if len(ends)==1:
                label=ends[0];endbox=label['bbox']
                # Only standalone date cells/text, unique within the labelled columns.
                values=[b for b in blocks if row(b) and len(dates_in(b['text']))==1
                        and len(b['text'].strip())<=24]
                starts=[b for b in values if box['r']<b['bbox']['l']<endbox['l']]
                finishes=[b for b in values if b['bbox']['l']>endbox['r']]
                if len(starts)==len(finishes)==1:
                    start,end=next(iter(dates_in(starts[0]['text']))),next(iter(dates_in(finishes[0]['text'])))
                    if start<=end:pairs.append((start,end,starts[0],finishes[0],'same_row_labels'))
    if len(pairs)>1:return reject_language_dates(result,'ambiguous_or_duplicate_language_period')
    if len(pairs)!=1:return result
    start,end,start_block,end_block,basis=pairs[0]
    def date_claim(value,block):return {'value':value,'quote':block['text'],'block_id':block['id']}
    a,b=date_claim(start,start_block),date_claim(end,end_block)
    # Both dates are re-grounded, including real positive source pages.
    if not ground_claim('language_start_date',a,blocks).get('value') or not ground_claim('language_end_date',b,blocks).get('value'):
        return result
    result['fields'].update(language_start_date=a,language_end_date=b)
    if not language:
        language=[{'kind':'language','school':deepcopy(school)}]
        result.setdefault('programs',[]).extend(language)
    language[0].update(course_start_date=deepcopy(a),course_end_date=deepcopy(b))
    language[0].setdefault('school',deepcopy(school))
    result['_layout_resolution']={'basis':basis,'date_block_ids':[start_block['id'],end_block['id']],
                                  'institution_block_id':grounded['block_id']}
    return result
