const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');

const source = fs.readFileSync(
  path.join(__dirname, '../../assets/transfer-v2-ui.js'),
  'utf8'
);

test('multi-school control clones a target row without inline scripts', () => {
  assert.match(source, /data-transfer-v2-add-target/);
  assert.match(source, /cloneNode\(true\)/);
  assert.match(source, /data-transfer-v2-school-selector/);
});

test('school selector treats edited library text as manual and clears identifier metadata', () => {
  assert.match(source, /data-transfer-v2-school-selector/);
  assert.match(source, /addEventListener\('input'/);
  assert.match(source, /writeSelectorFact\(schoolState,\s*''\)/);
  assert.match(source, /writeSelectorFact\(schoolRegion,\s*''\)/);
  assert.match(source, /writeSelectorFact\(schoolType,\s*''\)/);
  assert.match(source, /setAttribute\('value',\s*value\)/);
  assert.match(source, /selectedSchoolName/);
  assert.match(source, /schoolName\.value\s*===\s*selector\.dataset\.selectedSchoolName/);
  assert.match(source, /data-transfer-v2-school-library-result/);
  assert.match(source, /addEventListener\('change'/);
  assert.match(source, /option\.hidden/);
  assert.match(source, /option\.hidden\s*=\s*false/);
});

test('school autocomplete debounces fuzzy results and supports keyboard and outside close', () => {
  assert.match(source, /AUTOCOMPLETE_DELAY_MS\s*=\s*250/);
  assert.match(source, /query\.length\s*<\s*2/);
  assert.match(source, /setTimeout/);
  assert.match(source, /data-transfer-v2-school-suggestions/);
  assert.match(source, /ArrowDown/);
  assert.match(source, /ArrowUp/);
  assert.match(source, /Enter/);
  assert.match(source, /Escape/);
  assert.match(source, /document\.addEventListener\('click'/);
  assert.match(source, /aria-expanded/);
  assert.match(source, /data-school-id/);
});

test('missing-document override requires an explicit browser confirmation', () => {
  assert.match(source, /data-transfer-v2-missing-override/);
  assert.match(source, /data-transfer-v2-confirm-message/);
  assert.match(source, /window\.confirm/);
  assert.match(source, /preventDefault/);
});

test('create form disables submit after the first accepted submission', () => {
  assert.match(source, /addEventListener\('submit'/);
  assert.match(source, /submitter\.disabled\s*=\s*true/);
});

test('tabs use accessible selected and hidden state', () => {
  assert.match(source, /aria-selected/);
  assert.match(source, /\.hidden\s*=/);
});

test('reminder deep links activate the exact transfer tab and highlight key', () => {
  assert.match(source, /location\.hash\.startsWith\('#transfer-v2-'\)/);
  assert.match(source, /URLSearchParams\(location\.search\)/);
  assert.match(source, /transferV2Highlight/);
});
