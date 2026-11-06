// Synthetic REVIEW UI, real browser and actual development script; no CRM database.
const fs = require('fs');
const path = require('path');
const assert = require('assert/strict');
const {chromium} = require('/Users/faqiang/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
const root = path.resolve(__dirname,'..');
const script = fs.readFileSync(path.join(root,'assets/local-document-review.js'),'utf8');
(async()=>{
 const browser = await chromium.launchPersistentContext(path.join(root,'STAGING_DATA/tmp/review-browser-profile'),{headless:true,executablePath:'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',args:['--disable-background-networking','--disable-component-update','--no-first-run']});
 try {
  const page = await browser.newPage();
  page.setDefaultTimeout(8000);
  await page.route('**/*',r=>r.abort());
  await page.setContent(`<article class="review-card" data-local-review-url="http://review.test/local-status"><select data-document-type-select><option>Offer</option><option>CAAW</option><option>COE</option></select><form>
   <div data-extraction-field-row data-source-page="1" data-source-snippet="Old"><input name="extract_school_name_0" data-extraction-field value="Old school"><span class="extraction-status"></span><blockquote>Old</blockquote></div>
   <div data-extraction-field-row data-source-page="1"><input name="extract_course_start_date_0" data-extraction-field data-date-input value=""><span class="extraction-status"></span><blockquote></blockquote></div>
   <div data-extraction-field-row><input name="extract_language_school_name_0" data-extraction-field value=""><span class="extraction-status"></span><blockquote></blockquote></div>
   <div data-extraction-field-row><input name="extract_coe_provider_0" data-extraction-field value=""><span class="extraction-status"></span><blockquote></blockquote></div>
   <div data-extraction-field-row><output data-local-informational-field="secondary_main_course_school"></output><span class="extraction-status"></span><blockquote></blockquote></div>
  </form></article>`);
  await page.evaluate(()=>{window.responses=[];window.fetch=()=>new Promise(resolve=>window.responses.push(resolve));});
  await page.addScriptTag({content:script});
  await page.waitForFunction(()=>window.responses.length===1);
  // Typing/clearing before a late response must retain the user's value.
  await page.locator('[name=extract_language_school_name_0]').fill('Human IEC');
  await page.evaluate(()=>window.responses.shift()({ok:true,json:async()=>({status:'completed',fields:{school_name:{value:'New <College>',snippet:'School: New <College>',page:3},course_start_date:{value:'2027-07-19',snippet:'19-Jul-2027',page:2},language_school_name:{value:'Model IEC',snippet:'Model IEC',page:2}}})}));
  await page.waitForFunction(()=>document.querySelector('[name=extract_course_start_date_0]').value==='19/07/2027');
  assert.equal(await page.locator('[name=extract_school_name_0]').inputValue(),'Old school');
  assert.equal(await page.locator('[name=extract_language_school_name_0]').inputValue(),'Human IEC');
  assert.equal(await page.locator('[data-local-candidate]').count(),1);
  await page.locator('[data-local-candidate]').click();
  assert.equal(await page.locator('[name=extract_school_name_0]').inputValue(),'New <College>');
  assert.equal(await page.locator('[data-extraction-field-row]').first().getAttribute('data-source-page'),'3');
  assert.equal(await page.locator('college').count(),0); // textContent, never HTML injection.
  // Switch twice while the first response is still in flight.
  await page.locator('select').selectOption('CAAW');
  await page.waitForFunction(()=>window.responses.length===1);
  await page.locator('select').selectOption('Offer');
  await page.waitForFunction(()=>window.responses.length===2);
  await page.evaluate(()=>window.responses.shift()({ok:true,json:async()=>({status:'completed',fields:{school_name:{value:'STALE',snippet:'STALE',page:9}}})}));
  await page.waitForTimeout(40);
  assert.equal(await page.locator('[name=extract_school_name_0]').inputValue(),'New <College>');
  await page.evaluate(()=>window.responses.shift()({ok:false,json:async()=>({})}));
  await page.waitForTimeout(40);
  assert.equal(await page.locator('[name=extract_school_name_0]').inputValue(),'New <College>');
  await page.locator('select').selectOption('CAAW');
  await page.waitForFunction(()=>window.responses.length===1);
  await page.evaluate(()=>window.responses.shift()({ok:true,json:async()=>({status:'completed',fields:{coe_provider:{value:'Provider A',snippet:'Provider A',page:1},secondary_main_course_school:{value:'Sample Secondary',snippet:'Sample Secondary',page:2}}})}));
  await page.waitForFunction(()=>document.querySelector('[name=extract_coe_provider_0]').value==='Provider A');
  assert.equal(await page.locator('[data-local-informational-field]').textContent(),'Sample Secondary');
  await page.locator('select').selectOption('COE');
  await page.waitForFunction(()=>window.responses.length===1);
  await page.evaluate(()=>window.responses.shift()({ok:true,json:async()=>({status:'completed',fields:{coe_provider:{value:'Provider B',snippet:'Provider B',page:2}}})}));
  await page.waitForFunction(()=>Array.from(document.querySelectorAll('[data-local-candidate]')).some(b=>b.textContent.includes('Provider B')));
  await page.locator('[data-local-candidate]').click();
  assert.equal(await page.locator('[name=extract_coe_provider_0]').inputValue(),'Provider B');
  await page.screenshot({path:path.join(root,'STAGING_DATA/results/local-review-synthetic-browser.png')});
  const fallbackPage = await browser.newPage();
  await fallbackPage.setContent(`<article class="review-card" data-local-review-url="http://review.test/local-status"><select data-document-type-select><option>Offer</option></select><div data-extraction-field-row><input name="extract_school_name_0" data-extraction-field value="Old"><button type="button" data-local-candidate data-value="Ready school" data-snippet="Ready school" data-page="2">Ready</button></div></article>`);
  await fallbackPage.evaluate(()=>{window.fetch=async()=>({ok:false});});
  await fallbackPage.addScriptTag({content:script});
  await fallbackPage.waitForFunction(()=>document.querySelector('[data-local-review-status]').textContent.includes('暂不可用'));
  assert.equal(await fallbackPage.locator('[data-local-candidate]').count(),1);
  await fallbackPage.locator('[data-local-candidate]').click();
  assert.equal(await fallbackPage.locator('input').inputValue(),'Ready school');
  await fallbackPage.close();
  console.log('PASS: typed fields, explicit conflict selection, date display, source page, HTML escaping, stale type responses, failure fallback, ready candidate retained');
 } finally {await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
