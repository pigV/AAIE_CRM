"""Presentation-only labels for the Visa overview; never changes case facts."""
from html import escape


_STATE_TONES = {
    'draft': 'gray',
    'information collection': 'blue',
    'documents preparing': 'amber',
    'ready to lodge': 'blue',
    'lodged': 'blue',
    's56 requested': 'amber',
    'pending': 'amber',
    'granted': 'green',
    'refused': 'red',
    'withdrawn': 'gray',
    'closed': 'gray',
}
# Translations and material values are display aliases only; never persist them.
_STATE_ALIASES = {
    'not started': 'amber', '未开始': 'amber', '草稿': 'gray',
    'in progress': 'blue', '填写中': 'blue',
    'submitted': 'green', '已提交': 'green',
    'returned for update': 'amber', '已退回补充': 'amber',
    'reviewed': 'green', '已审核': 'green', 'locked': 'gray', '已锁定': 'gray',
    'expired': 'red', '已过期': 'red', 'active': 'green', '有效': 'green',
    'manually revoked': 'gray', '已手动失效': 'gray',
    'missing': 'amber', '未收到': 'amber', 'uploaded': 'green', '已收到': 'green',
    'not_applicable': 'gray', '不适用': 'gray',
    '签证材料需核实': 'amber',
}


def visa_state_tone(value):
    """One presentation lookup for case status, Decision and collection status."""
    label = str(value or '').strip().casefold()
    return _STATE_TONES.get(label, _STATE_ALIASES.get(label, 'gray'))


_TYPE_TONES = {
    'student visa': 'blue', '500 student visa': 'blue',
    'guardian visa': 'purple', '590 guardian visa': 'purple',
    'visitor visa': 'cyan', '600 visitor visa': 'cyan',
    '485': 'indigo',
}
_SUBCLASS_TONES = {'500': 'blue', '590': 'purple', '600': 'cyan', '485': 'indigo'}


def visa_overview_badge(value, kind):
    """Keep displayed values intact; normalize only the presentation lookup.

    Status and Decision intentionally use the same mapping and component.
    Unrecognised values stay visible in a neutral tag, with escaped text.
    """
    label = str(value or '').strip() or '—'
    tones = {'status': _STATE_TONES, 'decision': _STATE_TONES,
             'type': _TYPE_TONES, 'subclass': _SUBCLASS_TONES}.get(kind, {})
    tone = visa_state_tone(label) if kind in {'status', 'decision'} else tones.get(label.casefold(), 'gray')
    state_attr = " data-visa-state" if kind in {"status", "decision"} else ""
    return f'<span class="visa-overview-tag visa-overview-tag--{tone}"{state_attr}>{escape(label)}</span>'
