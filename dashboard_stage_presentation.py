"""Homepage stage labels, ordering and colors; never derives business state."""
from html import escape

STAGE_PALETTE = (
    '#315BEF', '#7A5AF8', '#22A6B3', '#E49B2F', '#D85A62',
    '#2E9B66', '#4C7BD9', '#B55CC8', '#00A6A6', '#D8872D',
    '#6B7C93', '#C36A8D', '#20A66A',
)
_CANONICAL_KEYS = (
    '01_parent_form', '02_contract_creation', 'contract_preparation',
    '03_contract_signature', 'waiting_school_plan',
    '06_school_application_submission', '07_school_response',
    '08_offer_acceptance', 'offer_acceptance_steps', '09_waiting_coe',
    '10_visa_submission', '11_visa_result',
)
_COLORS = dict(zip(_CANONICAL_KEYS, STAGE_PALETTE))
_COLORS.update({
    'materials_preparation': STAGE_PALETTE[12],
    'visa_granted': '#15803D', 'granted': '#15803D',
    'transfer_completed': '#15803D', 'completed': '#15803D',
    'visa_refused': '#BE123C', 'refused': '#BE123C',
    'contract_link_inactive': '#B45309', 'offer_expired': '#B45309',
    'visa_result_needs_review': '#B45309', 'visa_case_cycle_unassigned': '#B45309',
    'workflow_error': '#BE123C',
    'pending_completion': '#0F766E', 'legacy_unconfirmed': '#B45309',
    'acceptance_required': '#8B5CF6', 'payment_required': '#0E7490',
})
_ALIASES = {
    'waiting_parent_form': '01_parent_form',
    'waiting_contract_signature': '03_contract_signature',
    'school_plan': 'waiting_school_plan',
    'waiting_submission': '06_school_application_submission',
    'school_application': '06_school_application_submission',
    'offer_received': '08_offer_acceptance',
    'accepted_and_paid': 'offer_acceptance_steps',
    'offer_accepted': 'offer_acceptance_steps',
    'coe_update': '09_waiting_coe',
    'paid': '09_waiting_coe', 'coe_received': '10_visa_submission',
    'application_complete': 'pending_completion',
    'waiting_school_offer': '07_school_response',
    'waiting_final_school': '08_offer_acceptance',
    'waiting_coe': '09_waiting_coe',
    'visa_preparation': '10_visa_submission',
    'waiting_visa_result': '11_visa_result',
}


def stage_palette_color(index):
    return STAGE_PALETTE[index] if 0 <= index < len(STAGE_PALETTE) else STAGE_PALETTE[0]


def stage_color(key):
    return _COLORS.get(_ALIASES.get(key, key), '#64748B')


def stage_label(key, fallback):
    return '转学资料准备' if key == 'materials_preparation' else str(fallback or '')


def ordered_stage_definitions(definitions):
    result = [(key, stage_label(key, label)) for key, label in definitions]
    transfer = [item for item in result if item[0] == 'materials_preparation']
    if not transfer:
        return result
    result = [item for item in result if item[0] != 'materials_preparation']
    before = next((i for i, (key, _) in enumerate(result)
                   if key in {'waiting_school_plan', 'school_plan', '06_school_application_submission'}), len(result))
    return result[:before] + transfer + result[before:]


def stage_badge_html(key, label):
    key = str(key or 'not-recorded')
    label = stage_label(key, label) or '未记录'
    return (f'<span class="status dashboard-application-stage application-stage-{escape(key, quote=True)}" '
            f'style="--application-stage-color:{stage_color(key)}">{escape(label)}</span>')
