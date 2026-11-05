const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { chromium } = require('playwright');
const root = path.resolve(__dirname, '../..');
(async () => {
  const browser = await chromium.launch({ headless: true, ...(process.env.PREVIEW_BROWSER_EXECUTABLE ? {executablePath: process.env.PREVIEW_BROWSER_EXECUTABLE} : {}) });
  try {
    const page = await browser.newPage({ viewport: { width: 1000, height: 800 } });
    await page.route('http://preview.test/**', route=>route.fulfill({contentType:'text/html',body:'<html></html>'}));
    await page.goto('http://preview.test/');
    const python = fs.readFileSync(path.join(root, 'app.py'), 'utf8');
    const html = python.slice(python.indexOf('<dialog class="preview-dialog"'), python.indexOf('</dialog>', python.indexOf('<dialog class="preview-dialog"')) + 9);
    const css = fs.readFileSync(path.join(root, 'assets/style.css'), 'utf8');
    await page.setContent(`<style>${css}\n.preview-dialog {width:900px;height:650px} .preview-layout {height:480px} .preview-canvas {height:440px} .preview-thumbnail-sidebar {height:440px}</style><button id="open" data-preview-open data-preview-src="/documents/1/preview" data-preview-title="Study Plan" data-download-url="/documents/1/download">预览</button>${html}`);
    await page.addScriptTag({content: `
      const q = (s,r=document)=>r.querySelector(s);
      const qa = (s,r=document)=>Array.from(r.querySelectorAll(s));
      window.requests=[];
      window.fetch=(url,options)=>new Promise((resolve,reject)=>{
        const request={url,resolve,reject,done:false}; window.requests.push(request);
        options.signal?.addEventListener('abort',()=>{request.done=true;reject(new DOMException('Aborted','AbortError'));});
      });
      window.respond=(i,status='ok',total=34,current=null)=>{
        const r=requests[i]; r.done=true;
        const bytes=Uint8Array.from(atob('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aHZkAAAAASUVORK5CYII='),c=>c.charCodeAt(0));
        r.resolve(new Response(bytes,{headers:{'Content-Type':'image/png','X-Preview-Status':status,'X-Preview-Error-Code':status==='ok'?'':'pdf_render_failed','X-Preview-Page-Count':String(total),'X-Preview-Page':current || new URL(r.url,'http://preview.test').searchParams.get('page')}}));
      };
    `});
    // Execute the actual dialog code, independent of unrelated CRM modules.
    const js = fs.readFileSync(path.join(root, 'assets/app.js'), 'utf8');
    await page.addScriptTag({content: js.slice(js.indexOf('  const previewDialog ='), js.indexOf('  qa("[data-manual-placement]"'))});
    await page.click('#open');
    await page.waitForFunction(()=>requests.length===1);
    await page.evaluate(()=>respond(0));
    await page.waitForTimeout(150);
    const active = await page.evaluate(()=>requests.filter(r=>!r.done).length);
    assert.ok(active<=2, `preview requests must be bounded, saw ${active}`);
    assert.equal(await page.textContent('[data-preview-total-pages]'), '34');
    assert.equal(await page.locator('[data-preview-page="1"] .preview-page-placeholder').isVisible(), false, 'loaded page must hide its placeholder');
    const next = await page.evaluate(()=>requests.findIndex(r=>!r.done && r.url.includes('size=large')));
    assert.ok(next>=0);
    const firstImage = await page.locator('[data-preview-page="1"] img').getAttribute('src');
    await page.evaluate(i=>respond(i,'error',1,1),next);
    await page.waitForTimeout(80);
    assert.equal(await page.textContent('[data-preview-total-pages]'), '34', 'page failure must not collapse the document to one page');
    assert.equal(await page.locator('[data-preview-page="1"] img').getAttribute('src'),firstImage,'failed page must not replace the successful first page');
    // Close with work pending and reopen another document; late replies must not mutate it.
    await page.click('[data-preview-close]');
    await page.evaluate(()=>{document.querySelector('#open').dataset.previewSrc='/documents/2/preview';});
    await page.click('#open');
    await page.waitForFunction(()=>requests.some(r=>r.url.startsWith('/documents/2/')&&!r.done));
    const latest=await page.evaluate(()=>requests.findIndex(r=>r.url.startsWith('/documents/2/')&&!r.done));
    await page.evaluate(i=>respond(i,'ok',2),latest);
    await page.waitForTimeout(100);
    assert.equal(await page.textContent('[data-preview-total-pages]'),'2');
    assert.ok(await page.evaluate(()=>requests.filter(r=>!r.done && r.url.startsWith('/documents/1/')).length===0),'close must abort old requests');
    await page.screenshot({path:path.join(root,'tmp/study-plan-preview-tests/preview-browser.png')});
    console.log('PASS: bounded requests, hidden placeholders, stable page count, session cancellation');
    if (process.env.CRM_PREVIEW_IMAGE_DIR) {
      const images = {};
      for (let n=1;n<=34;n++) {
        for (const size of ['large','thumb']) images[`${n}_${size}`]=fs.readFileSync(path.join(process.env.CRM_PREVIEW_IMAGE_DIR,`1_${size}_p${n}.png`)).toString('base64');
      }
      await page.click('[data-preview-close]');
      await page.evaluate(images=>{
        window.fetch=async (url)=>{
          const query=new URL(url,'http://preview.test').searchParams;
          const page=query.get('page');
          const data=Uint8Array.from(atob(images[`${page}_${query.get('size')}`]),c=>c.charCodeAt(0));
          return new Response(data,{headers:{'Content-Type':'image/png','X-Preview-Status':'ok','X-Preview-Page-Count':'34','X-Preview-Page':page}});
        };
        document.querySelector('#open').dataset.previewSrc='/documents/real/preview';
      },images);
      await page.click('#open');
      await page.waitForFunction(()=>document.querySelector('[data-preview-page="1"] img')?.complete);
      await page.waitForFunction(()=>document.querySelector('[data-preview-thumbnail-page="1"] img')?.naturalWidth>0);
      await page.screenshot({path:path.join(root,'tmp/study-plan-preview-tests/real-pdf-browser-first.png')});
      await page.fill('[data-preview-jump-input]','34');
      await page.click('[data-preview-jump-button]');
      await page.waitForFunction(()=>{
        const img=document.querySelector('[data-preview-page="34"] img');
        return img?.complete && !img.hidden && img.naturalWidth>0;
      });
      assert.equal(await page.textContent('[data-preview-total-pages]'),'34');
      assert.equal(await page.textContent('[data-preview-current-page]'),'34');
      await page.click('[data-preview-zoom-in]');
      assert.equal(await page.inputValue('[data-preview-zoom]'),'1.25');
      await page.click('[data-preview-fit-width]');
      assert.equal(await page.inputValue('[data-preview-zoom]'),'1');
      await page.click('[data-preview-thumbnail-toggle]');
      assert.ok(await page.locator('[data-preview-layout]').evaluate(el=>el.classList.contains('thumbnails-collapsed')));
      await page.screenshot({path:path.join(root,'tmp/study-plan-preview-tests/real-pdf-browser-last.png')});
      console.log('PASS: real PDF first/last page, jump, zoom, fit-width and thumbnail toggle');
    }
  } finally { await browser.close(); }
})().catch(error=>{console.error(error);process.exitCode=1;});
