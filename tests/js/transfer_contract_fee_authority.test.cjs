const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');

test('source-authoritative Transfer fee is never overwritten by generic 2400 default', () => {
  const source = fs.readFileSync('assets/app.js', 'utf8');
  assert.match(source, /contractFeeAuthority/);
  assert.match(source, /if \(!editable && !authoritative\)/);
  assert.match(source, /serviceFee\.readOnly = authoritative \|\| !editable/);
  assert.match(source, /genericFee\.value = form\.dataset\.contractTransferFeeAuthority \|\| ""/);
});
