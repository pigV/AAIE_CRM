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
    tone = tones.get(label.casefold(), 'gray')
    return f'<span class="visa-overview-tag visa-overview-tag--{tone}">{escape(label)}</span>'
