"""Source-ground claims; supported text is not proof of correct business meaning."""
from datetime import datetime
import re

FIELDS = {'provider', 'school', 'language_school', 'main_school', 'course',
          'course_start_date', 'course_end_date', 'welfare_start_date', 'welfare_end_date',
          'language_start_date', 'language_end_date', 'year_level'}


def compact(value):
    return ' '.join(str(value or '').split()).casefold()


def dates_in(text):
    patterns = [r'\b\d{4}-\d{2}-\d{2}\b', r'\b\d{1,2}[/-]\d{1,2}[/-]\d{4}\b',
                r'\b\d{1,2}[ -][A-Za-z]{3,9},?[ -]\d{4}\b',
                r'\b[A-Za-z]{3,9} +\d{1,2},? +\d{4}\b']
    values = set()
    for pattern in patterns:
        for match in re.finditer(pattern, text):
            for fmt in ('%Y-%m-%d', '%d/%m/%Y', '%d-%m-%Y', '%d-%b-%Y', '%d-%B-%Y', '%d %b %Y', '%d %B %Y', '%d %B, %Y', '%d %b, %Y', '%B %d, %Y', '%b %d, %Y', '%B %d %Y', '%b %d %Y'):
                try:
                    values.add(datetime.strptime(match.group(), fmt).date().isoformat())
                    break
                except ValueError:
                    pass
    return values


def ground_claim(field, claim, blocks):
    rejected = {'value': None, 'status': 'needs_review', 'reason': 'missing_or_invalid_claim'}
    if field not in FIELDS or not isinstance(claim, dict):
        return rejected
    value, quote, block_id = (claim.get(key) for key in ('value', 'quote', 'block_id'))
    if not all(isinstance(v, str) and v.strip() for v in (value, quote, block_id)):
        return rejected
    block = next((b for b in blocks if b['id'] == block_id), None)
    if block is None or compact(quote) not in compact(block['text']):
        return {**rejected, 'reason': 'quote_not_found'}
    if not isinstance(block.get('page'),int) or block['page']<1:
        return {**rejected,'reason':'ambiguous_source_location'}
    if field.endswith('_date'):
        try:
            valid = datetime.strptime(value, '%Y-%m-%d').date().isoformat() == value and value in dates_in(quote)
        except ValueError:
            valid = False
    else:
        valid = compact(value) in compact(quote)
    if not valid:
        return {**rejected, 'reason': 'value_not_supported'}
    return {'value': value.strip(), 'status': 'supported_needs_review', 'block_id': block_id,
            'quote': quote, 'page': block.get('page'), 'bbox': block.get('bbox')}


def validate_result(result, blocks):
    if not isinstance(result, dict) or not isinstance(result.get('fields'), dict) or not isinstance(result.get('programs', []), list):
        raise ValueError('Invalid extraction shape')
    fields = {key: ground_claim(key, value, blocks) for key, value in result['fields'].items() if key in FIELDS}
    issues = []
    for prefix in ('course', 'welfare', 'language'):
        start, end = prefix+'_start_date', prefix+'_end_date'
        if fields.get(start, {}).get('value') and fields.get(end, {}).get('value') and fields[start]['value'] > fields[end]['value']:
            issues.append(prefix+'_dates_reversed')
            for key in (start, end):
                fields[key] = {**fields[key], 'value': None, 'status': 'needs_review', 'reason': 'dates_reversed'}
    programs = []
    if len(result.get('programs', [])) > 20:
        raise ValueError('Too many programs')
    for program in result.get('programs', []):
        if not isinstance(program, dict) or program.get('kind') not in ('language', 'main', 'unknown'):
            issues.append('invalid_program')
            continue
        checked = validate_result({'fields': {k: v for k, v in program.items() if k in FIELDS}, 'programs': []}, blocks)
        programs.append({'kind': program['kind'], 'fields': checked['fields'], 'issues': checked['issues']})
    return {'fields': fields, 'programs': programs, 'issues': issues, 'requires_human_review': True}
