"""Student-facing context labels; never alter the student's legal name."""
from html import escape


def student_education_label(student):
    values=dict(student)
    if values.get('application_stream') != 'higher_education':
        return ''
    course_type=str(values.get('higher_education_course_type') or '').strip().casefold()
    return 'TAFE' if course_type=='tafe' else '大学'


def student_type_badge_html(student):
    label=student_education_label(student)
    if not label:
        return ''
    return f'<span class="status student-education-name-badge" data-student-education-kind>{escape(label)}</span>'
