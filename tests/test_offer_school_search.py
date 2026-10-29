"""Word #9: real Offer renderer/initializer, synthetic data and DOM only."""
import ast
import html
import json
from pathlib import Path
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]
NODE = Path('/Users/faqiang/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node')


def render_offer_match(*, school_id=11, transfer=False):
    """Compile the unchanged actual method without application startup/DB imports."""
    tree = ast.parse((ROOT / 'app.py').read_bytes())
    method = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef) and n.name == 'offer_panel')
    schools = [dict(id=11, school_name='Original School', city_name='Melbourne', sector_key='public'),
               dict(id=12, school_name='Alpha & <Academy>', city_name='Sydney', sector_key='private'),
               dict(id=13, school_name='ALPHA College', city_name='Perth', sector_key='public')]

    class Database:
        def one(self, sql, params=()):
            if 'sqlite_master' in sql:
                return {'present': 1} if any(x in sql for x in ("name='schools'", "name='school_cities'")) or transfer else None
            if transfer and 'FROM offer_received_events' in sql:
                return dict(case_id=8, branch_id=9, transfer_is_accepted=False)
            return None

        def all(self, sql, params=()):
            if 'FROM schools' not in sql:
                raise AssertionError('Unexpected synthetic query: ' + sql)
            return schools

    namespace = dict(db=Database(), Path=Path,
        esc=lambda v: html.escape(str(v or ''), quote=True),
        offer_record_value=lambda row, key, default='': row.get(key, default),
        offer_record_document_type=lambda row: row.get('document_type', 'Offer'),
        offer_record_file_path=lambda row: '',
        module_document_type_label=lambda module, kind: kind,
        module_document_types=lambda module: ('Offer',),
        student_file_preview_panel=lambda **kw: '',
        lifecycle_context_badge_html=lambda context: '',
        display_datetime=lambda value: str(value or ''),
        crm_display_date_input=lambda name, value='', form_id='':
            '<input name="' + name + '" value="' + html.escape(value, quote=True) + '">')
    exec(compile(ast.Module(body=[method], type_ignores=[]), str(ROOT / 'app.py'), 'exec'), namespace)
    handler = type('SyntheticOfferHandler', (), {'offer_panel': namespace['offer_panel'],
        'csrf': 'synthetic-not-a-credential',
        'transfer_v2_business_branch_select_html': lambda *a, **kw: ''})()
    offer = dict(id=71, school_name='Original School', offer_status='received', deposit_currency='AUD',
        document_type='Offer', original_filename='synthetic.pdf', uploaded_at='2026-10-24',
        created_at='2026-10-24', deposit_amount=None, offer_date='', offer_expiry_date='',
        school_id=school_id, city_id=1, city_name_snapshot='Melbourne', sector_key='public',
        classification_status='resolved')
    return handler.offer_panel('SYNTHETIC-WORD9', [offer])


def initializer_source():
    source = (ROOT / 'assets/app.js').read_text()
    start = source.index('  const initOfferPanel = ')
    end = source.index('\n  initOfferPanel(document);', start)
    return source[start:end] + '\ninitOfferPanel(document);'


DOM_TEST = r'''
const vm = require('node:vm');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const source = fs.readFileSync(0, 'utf8');
function build(selected = '11') {
  const option = (value, text) => ({value, textContent:text});
  const choices = [option('', '选择 CRM 学校'), option('11', 'Original School · Melbourne · 公校'),
    option('12', 'Alpha & <Academy> · Sydney · 私校'), option('13', 'ALPHA College · Perth · 公校')];
  const listeners = {};
  const input = {value:'', dataset:{}, addEventListener:(key, fn) => {
    (listeners[key] ||= []).push(fn);
  }, closest:() => form};
  const select = {value:selected, options:[...choices], addEventListener:(key,fn)=>{
    (listeners['select-'+key] ||= []).push(fn);
  }, replaceChildren(...items) { this.options = items; this.value = items[0]?.value || ''; }};
  const status = {textContent:''};
  const form = {querySelector(selector) {
    if (selector.includes('select')) return select;
    if (selector.includes('status')) return status;
    if (selector.includes('search')) return input;
    return null;
  }, querySelectorAll:() => []};
  const document = {querySelectorAll(selector) {
    if (selector.includes('offer-school-search')) return [input];
    return [];
  }};
  const context = {document, Array, q:(selector,root=document)=>root.querySelector(selector),
    qa:(selector,root=document)=>Array.from(root.querySelectorAll(selector)),
    saveOfferForm:()=>{throw Error('Unexpected save')}, setTimeout:()=>{throw Error('Unexpected timer')}};
  vm.runInNewContext(source, context);
  const type = text => {input.value=text; for (const fn of listeners.input || []) fn();};
  return {choices, input, select, status, listeners, type, context};
}
const f=build();
assert.equal(f.listeners.input?.length, 1, 'missing actual Offer school-search listener');
let enterPrevented=false;
for (const fn of f.listeners.keydown || []) fn({key:'Enter', preventDefault(){enterPrevented=true;}});
assert.equal(enterPrevented, true, 'search Enter must not implicitly submit the existing school binding');
let tabPrevented=false;
for (const fn of f.listeners.keydown || []) fn({key:'Tab', preventDefault(){tabPrevented=true;}});
assert.equal(tabPrevented, false, 'normal keyboard navigation must remain available');
f.type('  aLpHa  ');
assert.deepEqual(f.select.options.map(x=>x.value), ['', '11', '12', '13']);
assert.equal(f.select.value, '11', 'typing must not replace current school binding');
f.select.value='12'; for (const fn of f.listeners['select-change'] || []) fn();
f.type('original');
assert.deepEqual(f.select.options.map(x=>x.value), ['', '11', '12']);
assert.equal(f.select.value, '12');
f.type('no-such-school');
assert.deepEqual(f.select.options.map(x=>x.value), ['', '12']);
assert.equal(f.select.value, '12');
assert.match(f.status.textContent, /未找到/);
f.type('');
assert.deepEqual(f.select.options.map(x=>x.value), ['', '11', '12', '13']);
assert.equal(f.select.value, '12');
vm.runInNewContext('initOfferPanel(document);', f.context);
assert.equal(f.listeners.input.length, 1, 'dynamic reinitialization must not duplicate listeners');
const empty=build(''); empty.type('alpha');
assert.deepEqual(empty.select.options.map(x=>x.value), ['', '12', '13']);
assert.equal(empty.select.value, '', 'a search is not an authorized selection');
console.log('PASS actual initializer: filter, exact IDs, no-match, clear, preserved selection, dynamic initialization');
'''


class OfferSchoolSearchTests(unittest.TestCase):
    def test_real_renderer_has_search_and_preserves_form_contract(self):
        page = render_offer_match()
        self.assertIn('data-offer-school-search', page)
        self.assertIn('type="search"', page)
        self.assertIn('name="school_id" required', page)
        self.assertIn('value="11" selected', page)
        self.assertIn('action="/students/SYNTHETIC-WORD9/offer/71/classify"', page)
        self.assertIn('name="csrf" value="synthetic-not-a-credential"', page)
        self.assertIn('Alpha &amp; &lt;Academy&gt;', page)
        self.assertNotIn('name="school_search"', page)

    def test_transfer_managed_school_binding_still_has_no_editable_picker(self):
        page = render_offer_match(transfer=True)
        self.assertIn('学校匹配由精确的 Transfer Target Branch 管理', page)
        self.assertNotIn('data-offer-school-search', page)
        self.assertNotIn('/offer/71/classify', page)

    def test_actual_initializer_filters_without_mutation_or_auto_selection(self):
        completed = subprocess.run([str(NODE), '-e', DOM_TEST], input=initializer_source(),
            text=True, capture_output=True, timeout=10)
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)


if __name__ == '__main__':
    unittest.main()
