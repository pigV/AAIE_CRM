"""Applied filters are removable independently, including exact student search."""
from html.parser import HTMLParser
import unittest
from urllib.parse import parse_qs, urlsplit


class ChipLinks(HTMLParser):
    def __init__(self, markup):
        super().__init__()
        self.links = {}
        self.feed(markup)

    def handle_starttag(self, tag, attributes):
        attrs = dict(attributes)
        if tag == 'a':
            self.links[attrs['aria-label']] = parse_qs(urlsplit(attrs['href']).query)


class WorkbenchFilterChipTests(unittest.TestCase):
    def render(self, filters, selected_student_id=''):
        from student_management_filters import render_applied_filters

        return render_applied_filters(filters, selected_student_id=selected_student_id)

    def test_removing_one_filter_preserves_the_other_applied_filters(self):
        markup = self.render((
            ('q', '20260069', '搜索：20260069'),
            ('stage', 'granted', '阶段：已下签'),
            ('created_from', '2027-01-01', '创建从：2027-01-01'),
            ('sort', 'student_id', '排序：Student ID'),
        ), '20260069')
        links = ChipLinks(markup).links
        self.assertEqual({
            'q': ['20260069'], 'selected_student_id': ['20260069'],
            'stage': ['granted'], 'sort': ['student_id'],
        }, links['清除筛选：创建从：2027-01-01'])
        self.assertEqual({
            'stage': ['granted'], 'created_from': ['2027-01-01'], 'sort': ['student_id'],
        }, links['清除筛选：搜索：20260069'])

    def test_empty_default_filters_render_nothing_and_markup_is_escaped(self):
        self.assertEqual('', self.render((('q', '', '搜索'), ('stage', '', '阶段'))))
        raw = '<b>姓名 & "样例"</b>'
        markup = self.render((('q', raw, raw), ('alert', 'risk', '提醒：风险优先')))
        self.assertNotIn(raw, markup)
        self.assertEqual({'q': [raw]}, ChipLinks(markup).links['清除筛选：提醒：风险优先'])
