"""Bind a split CAAW date label to its nearest explicit welfare section heading."""
import re
from local_document_lab.evidence import compact, dates_in
from local_document_lab.layout_dates import rectangle


def welfare_context(field,claim,blocks):
    block=next((b for b in blocks if b.get('id')==claim.get('block_id')),None)
    box=rectangle(block or {})
    if not box or block.get('bbox_precision')=='page' or abs(box['t']-box['b'])>24:return None
    text=compact(block.get('text'))
    role=r'(?:start|commencement)' if field=='welfare_start_date' else r'(?:end|finish|cessation)'
    if (not re.match(role+r'\s+date\s*[:\-]?',text)
            or re.search(r'\b(?:course|program|visa|payment)\b',text)
            or len(dates_in(text))!=1 or claim.get('value') not in dates_in(text)):
        return None
    def vertical(r):
        center=(r['t']+r['b'])/2
        return center if r['coord_origin']=='TOPLEFT' else -center
    candidates=[]
    for header in blocks:
        h=rectangle(header)
        if (not h or header.get('kind')!='section_header' or header.get('bbox_precision')=='page'
                or header.get('page')!=block['page'] or h['coord_origin']!=box['coord_origin']
                or abs(h['t']-h['b'])>36):continue
        gap=vertical(box)-vertical(h)
        # Retain every nearby heading as an ambiguity blocker, including other columns.
        if 0<gap<=72:candidates.append((gap,header))
    if not candidates:return None
    closest=min(gap for gap,_ in candidates)
    near=[header for gap,header in candidates if abs(gap-closest)<1]
    if len(near)!=1 or not re.search(r'\bwelfare\b',compact(near[0].get('text'))):return None
    return near[0]
