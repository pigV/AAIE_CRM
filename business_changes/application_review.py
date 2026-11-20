"""Explicit Initial Application review command, preserving the existing policy."""
from application_material_completion import load_application_material_completion
from .commands import Mutation
from .contracts import Subject
from .domain import run_domain_write


class MaterialReviewBlocked(ValueError):
    def __init__(self, code, missing_document_types=()):
        super().__init__(code)
        self.code = code
        self.missing_document_types = tuple(missing_document_types)


def review_materials(conn, *, student_id, actor_user_id, occurred_at, after_write):
    student_id = str(student_id)
    def capture(view):
        return load_application_material_completion(view, student_id)
    def operation(view):
        completion = capture(view)
        if completion.cycle_type != 'initial':
            raise MaterialReviewBlocked('no_initial_cycle')
        if not completion.parent_form_submitted:
            raise MaterialReviewBlocked('parent_form_not_submitted')
        if completion.missing_required_document_types:
            raise MaterialReviewBlocked('missing_materials', completion.missing_required_document_types)
        if completion.review_completed:
            return False
        cursor = view.execute('''UPDATE workflow_status SET materials_checked=1,
            materials_checked_by=?,materials_checked_at=? WHERE student_id=?''',
            (actor_user_id, occurred_at, student_id))
        if cursor.rowcount != 1:
            raise ValueError('材料审核记录不存在')
        after_write(view)
        return True
    def describe(view, result, before, after):
        if before.cycle_id != after.cycle_id or after.cycle_id is None:
            raise ValueError('application_review_cycle_changed')
        cursor = view.execute('SELECT * FROM application_cycles WHERE id=? AND student_id=?',
                              (after.cycle_id, student_id))
        row = cursor.fetchone()
        if row is None:
            raise ValueError('application_review_cycle_owner_mismatch')
        cycle = dict(zip((column[0] for column in cursor.description), row))
        affected = [Subject('student', student_id), Subject('module', 'applications'),
                    Subject('dashboard', 'workflow'), Subject('dashboard', 'overview')]
        if cycle.get('source_student_case_id') is not None:
            affected.append(Subject('student_case', str(cycle['source_student_case_id'])))
        return Mutation('application.materials_reviewed', Subject('application_cycle', str(after.cycle_id)), tuple(affected))
    return run_domain_write(conn, event_type='application.materials_reviewed', actor_kind='user',
        actor_ref=str(actor_user_id), capture=capture, operation=operation, describe=describe)
