"""Recover only uniquely labelled, same-page table roles and date intervals."""
from copy import deepcopy
import re
from local_document_lab.evidence import compact, dates_in, ground_claim

ABSENT = {'', 'not applicable', 'n/a', 'na', 'none', 'nil', '-', '—'}
HEADINGS = {'english language centre', 'english language center', 'host school', 'school'}
LABELS = {
    'english language centre': 'language_school', 'english language center': 'language_school',
    'host school': 'main_school',
    'english language program start date': 'language_start_date',
    'english language program end date': 'language_end_date',
    'host school start date': 'main_start_date', 'host school end date': 'main_end_date',
}


def vertical_programs(rows, table):
    """Read explicit Year Level / School / dates sections without row-order roles."""
    groups = []
    current = None
    keys = {'school': 'school', 'start date': 'course_start_date',
            'end date': 'course_end_date', 'course name': 'course'}
    for line, values in rows:
        if len(values) < 2:
            continue
        label = compact(values[0]).rstrip(':').strip()
        if label == 'year level':
            if current is not None:
                groups.append(current)
            current = {'level': values[1], 'fields': {}, 'invalid': False}
        elif current is not None and label in keys:
            key = keys[label]
            if key in current['fields']:
                current['invalid'] = True
            value = values[1]
            if key.endswith('_date'):
                dates = dates_in(value)
                value = next(iter(dates)) if len(dates) == 1 else ''
            current['fields'][key] = {'value': value, 'block_id': table['id'], 'quote': line}
    if current is not None:
        groups.append(current)
    if not groups:
        return []
    programs = []
    for group in groups:
        fields = group['fields']
        if group['invalid'] or any(not fields.get(key, {}).get('value') for key in ('school','course','course_start_date','course_end_date')):
            return []
        if compact(fields['school']['value']) in ABSENT | HEADINGS:
            return []
        if fields['course_start_date']['value'] > fields['course_end_date']['value']:
            return []
        level = compact(group['level'])
        course = compact(fields['course']['value'])
        if (re.search(r'\b(?:isec|iec|elicos)\b', level+' '+course)
                or re.fullmatch(r'(?:intensive )?english(?: language)?', level)):
            role = 'language'
        elif re.search(r'\byears?\s+\d+', level) and re.search(r'\b(?:primary|secondary)\b', course):
            role = 'main'
        else:
            return []
        programs.append({'kind': role, **fields})
    return programs


def resolve_table_roles(kind, raw, blocks):
    """Return repaired raw claims plus dates whose course-column role was verified.

    Model-supplied flags never authorize a date. Every generated quote is an actual
    row (or its table) from one page. Repeated/competing sources stay uncollapsed.
    """
    result = deepcopy(raw)
    if kind not in ('offer', 'confirmation') or not isinstance(result, dict):
        return result, set()
    fields = result.setdefault('fields', {})
    programs = result.setdefault('programs', [])
    if not isinstance(fields, dict) or not isinstance(programs, list):
        return result, set()
    original_programs = deepcopy(programs)
    original_fields = deepcopy(fields)
    blocked_roles = set()
    names = {'language_school': [], 'main_school': []}
    absent = set()
    periods = {'language': [], 'main': []}
    course_periods = []
    ambiguous_roles = set()
    invalid_course_period = False
    program_tables = []

    def source_claim(value, table, quote):
        return {'value': value.strip(), 'block_id': table['id'], 'quote': quote}

    def remember_name(role, value, table, quote):
        if compact(value) in ABSENT | HEADINGS:
            absent.add(role)
            return None
        claim = source_claim(value, table, quote)
        names[role].append(claim)
        return claim

    for table in blocks:
        if table.get('kind') != 'table' or type(table.get('page')) is not int or table['page'] < 1:
            continue
        candidates = table.get('page_candidates', [table['page']])
        if candidates != [table['page']]:
            continue
        rows = []
        for line in table.get('text', '').splitlines():
            if not line.strip().startswith('|') or not line.strip().endswith('|'):
                continue
            values = [part.strip() for part in line.strip().strip('|').split('|')]
            if values and all(not value or set(value) <= set('-: ') for value in values):
                continue
            rows.append((line, values))
        explicit_programs = vertical_programs(rows, table)
        if explicit_programs:
            program_tables.append(explicit_programs)
            for program in explicit_programs:
                role = 'language_school' if program['kind'] == 'language' else 'main_school'
                names[role].append(program['school'])
        facts = {}
        for line, values in rows:
            if len(values) != 2:
                continue
            label = compact(re.sub(r'\([^)]*\)', '', values[0])).rstrip(':').strip()
            field = LABELS.get(label)
            if not field:
                continue
            if field.endswith('_school'):
                item = remember_name(field, values[1], table, line)
            else:
                dates = dates_in(values[1])
                item = source_claim(next(iter(dates)), table, line) if len(dates) == 1 else None
            facts.setdefault(field, []).append(item)
        for role in ('language', 'main'):
            keys = (role+'_school', role+'_start_date', role+'_end_date')
            if all(len(facts.get(key, [])) == 1 and facts[key][0] for key in keys):
                school, start, end = (facts[key][0] for key in keys)
                if start['value'] <= end['value']:
                    periods[role].append({'school': school, 'course_start_date': start, 'course_end_date': end})
                else:
                    ambiguous_roles.add(role)
            elif any(key in facts for key in keys[1:]):
                ambiguous_roles.add(role)
        if kind != 'offer':
            continue
        date_column = None
        course_column = None
        for line, values in rows:
            columns = [i for i, value in enumerate(values) if re.search(r'\bcourse dates\b', compact(value))]
            if columns:
                date_column = columns[0] if len(columns) == 1 else None
                courses = [i for i, value in enumerate(values) if re.search(r'(?:^| - )course$', compact(value))]
                course_column = courses[0] if len(courses) == 1 else None
                continue
            if not values:
                continue
            row_names = {}
            if re.fullmatch(r'\s*(?:english language cent(?:re|er)|host school|school)\s*:\s*', values[0], re.I):
                continue  # Vertical label: its value is in the adjacent cell.
            for pattern, role in ((r'english language cent(?:re|er)', 'language_school'),
                                  (r'(?:host school|school)', 'main_school')):
                match = re.search(r'\b'+pattern+r'\s*:\s*(.*?)(?=\s+(?:host school|school|address|year level)\s*:|$)', values[0], re.I)
                if match:
                    row_names[role] = remember_name(role, match.group(1), table, line)
            if date_column is None or date_column >= len(values) or not row_names.get('main_school'):
                continue
            if course_column is None or course_column >= len(values):
                continue
            course = compact(values[course_column])
            if (re.search(r'\b(?:english|elicos|iec|language|preparation)\b', course)
                    or not re.search(r'\b(?:primary|secondary|bachelor|master|diploma|doctor|certificate|undergraduate|postgraduate)\b', course)):
                continue
            pattern = (r'\b(?:\d{4}-\d{2}-\d{2}|\d{1,2}[/-]\d{1,2}[/-]\d{4}|'
                       r'\d{1,2}[ -][A-Za-z]{3,9},?[ -]\d{4}|[A-Za-z]{3,9} +\d{1,2},? +\d{4})\b')
            matches = list(re.finditer(pattern, values[date_column]))
            dates = [dates_in(match.group()) for match in matches]
            if (len(dates) == 2 and all(len(date) == 1 for date in dates)
                    and re.search(r'\bto\b|\s[-–—]\s', values[date_column][matches[0].end():matches[1].start()], re.I)):
                first, last = (next(iter(date)) for date in dates)
                if first <= last:
                    course_periods.append((source_claim(first, table, table['text']),
                                           source_claim(last, table, table['text'])))
                else:
                    invalid_course_period = True

    accepted = set()
    def clear(role):
        blocked_roles.add(role)
        fields[role] = None
        if role == 'language_school':
            fields['language_start_date'] = fields['language_end_date'] = None
        elif kind == 'offer':
            fields['course_start_date'] = fields['course_end_date'] = None
        program_kind = 'language' if role == 'language_school' else 'main'
        programs[:] = [p for p in programs if not isinstance(p, dict) or p.get('kind') != program_kind]

    for role, items in names.items():
        old = fields.get(role)
        old_value = old.get('value') if isinstance(old, dict) else None
        if role in absent:
            clear(role)
            continue
        if compact(old_value) in HEADINGS | (ABSENT - {''}):
            # A model placeholder is invalid evidence, not proof that an actual
            # named source role is absent. Remove only that placeholder program.
            fields[role] = None
            program_kind = 'language' if role == 'language_school' else 'main'
            programs[:] = [program for program in programs if not (
                isinstance(program, dict) and program.get('kind') == program_kind
                and isinstance(program.get('school'), dict)
                and compact(program['school'].get('value')) in HEADINGS | ABSENT)]
            old_value = None
        values = {compact(item['value']) for item in items}
        if len(values) != 1:
            if len(values) > 1:
                clear(role)
            continue
        item = items[0]
        if (old_value and compact(old_value) != compact(item['value'])
                and ground_claim(role, old, blocks).get('value')):
            clear(role)
            continue
        fields[role] = item
        accepted.add(role)

    if program_tables:
        selected = program_tables[0]
        source_schools = {compact(program['school']['value']) for program in selected}
        source_dates = {program[key]['value'] for program in selected for key in ('course_start_date','course_end_date')}
        conflict = len(program_tables) != 1
        for role in ('main_school','language_school'):
            original = ground_claim(role, original_fields.get(role), blocks)
            if (original.get('value') and compact(original['value']) not in HEADINGS | ABSENT
                    and compact(original['value']) not in source_schools):
                conflict = True
        for key in ('language_start_date','language_end_date','course_start_date','course_end_date'):
            original = ground_claim(key, original_fields.get(key), blocks)
            if original.get('value') and original['value'] not in source_dates:
                conflict = True
        for program in original_programs:
            if not isinstance(program, dict):
                continue
            school = ground_claim('school', program.get('school'), blocks)
            valid_school = school.get('value') and compact(school['value']) not in HEADINGS | ABSENT
            if valid_school and compact(school['value']) not in source_schools:
                conflict = True
            if not valid_school:
                continue
            for key in ('course_start_date','course_end_date'):
                date = ground_claim(key, program.get(key), blocks)
                if date.get('value') and date['value'] not in source_dates:
                    conflict = True
        if conflict:
            clear('main_school')
            clear('language_school')
        else:
            programs[:] = selected
            languages = [program for program in selected if program['kind'] == 'language']
            if len(languages) == 1:
                fields['language_start_date'] = languages[0]['course_start_date']
                fields['language_end_date'] = languages[0]['course_end_date']

    if kind == 'confirmation':
        for role, intervals in periods.items():
            if role+'_school' in blocked_roles:
                continue
            same = [p for p in programs if isinstance(p, dict) and p.get('kind') == role]
            if role in ambiguous_roles or len(intervals) > 1 or (intervals and len(same) > 1):
                for program in same:
                    program['course_start_date'] = program['course_end_date'] = None
                if role == 'language':
                    fields['language_start_date'] = fields['language_end_date'] = None
                continue
            if len(intervals) != 1 or role+'_school' not in accepted:
                continue
            period = intervals[0]
            if not same:
                programs.append({'kind': role, **period})
            elif len(same) == 1:
                school = ground_claim('school', same[0].get('school'), blocks)
                if not school.get('value') or compact(school['value']) == compact(period['school']['value']):
                    same[0].update(period)
                else:
                    same[0]['course_start_date'] = same[0]['course_end_date'] = None
            if role == 'language':
                fields['language_start_date'] = period['course_start_date']
                fields['language_end_date'] = period['course_end_date']
    anchored = set()
    if kind == 'offer' and (invalid_course_period or len(course_periods) > 1):
        fields['course_start_date'] = fields['course_end_date'] = None
    elif kind == 'offer' and len(course_periods) == 1 and 'main_school' in accepted:
        fields['course_start_date'], fields['course_end_date'] = course_periods[0]
        anchored.add('course_start_date')
    # A later recovery must never resurrect an explicitly absent/conflicting role.
    for role in tuple(blocked_roles):
        clear(role)
    return result, anchored
