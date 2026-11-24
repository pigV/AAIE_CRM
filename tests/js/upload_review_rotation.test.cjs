const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {chromium} = require('playwright');
const root = path.resolve(__dirname, '../..');
// Real workbench controls and both production stylesheets: a rotated image must
// remain inside its scrollable layout bounds, including on narrow screens.
(async () => {
  const browser = await chromium.launch({headless:true, executablePath:process.env.PREVIEW_BROWSER_EXECUTABLE || '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'});
  try {
    for (const viewport of [{width:1440,height:1000},{width:390,height:844}]) {
      const page = await browser.newPage({viewport});
      await page.route('http://preview.test/**', route => route.fulfill({body:'<html></html>',contentType:'text/html'}));
      await page.goto('http://preview.test/');
      const python = fs.readFileSync(path.join(root,'app.py'),'utf8');
      let panel = python.slice(python.indexOf('<section class="review-preview-workspace upload-review-preview-panel"'), python.indexOf('</article>',python.indexOf('<section class="review-preview-workspace upload-review-preview-panel"')));
      panel = panel.replaceAll('{preview_src}', '/fixture').replaceAll('{esc(file_meta["original_name"])}','Passport fixture');
      await page.setContent(`<style>${fs.readFileSync(path.join(root,'assets/style.css'),'utf8')}\n${fs.readFileSync(path.join(root,'assets/ui-premium-v2.css'),'utf8')}\nbody{margin:0}.upload-review-page{padding:0!important} [data-upload-review-workbench]{display:block!important;width:100%;max-width:900px;margin:0 auto}</style><main class="ui-premium-v2"><div class="upload-review-page"><article class="upload-review-workbench upload-review-file-card" data-upload-review-workbench>${panel}</article></div></main>`);
      await page.addScriptTag({content:`window.fetch=async()=>new Response(new Blob(['<svg xmlns="http://www.w3.org/2000/svg" width="1200" height="1800"><rect width="1200" height="1800" fill="white"/><rect x="8" y="8" width="1184" height="1784" fill="none" stroke="red" stroke-width="16"/></svg>'],{type:'image/svg+xml'}),{headers:{'X-Preview-Page-Count':'1','Content-Type':'image/svg+xml'}}); const q=(s,r=document)=>r.querySelector(s);const qa=(s,r=document)=>Array.from(r.querySelectorAll(s));`});
      await page.addScriptTag({path:path.join(root,'assets/local-document-review.js')});
      const js = fs.readFileSync(path.join(root,'assets/app.js'),'utf8');
      await page.addScriptTag({content:js.slice(js.indexOf('  const initUploadReviewWorkbench ='),js.indexOf('  qa("[data-extraction-copy]"'))});
      await page.waitForFunction(()=>document.querySelector('[data-review-preview-image]').naturalWidth===1200);
      const geometry = () => page.evaluate(()=>{
        const canvas=document.querySelector('[data-review-preview-canvas]'), image=canvas.querySelector('img');
        const c=canvas.getBoundingClientRect(), i=image.getBoundingClientRect();
        return {x:i.x-c.x+canvas.scrollLeft,y:i.y-c.y+canvas.scrollTop,w:i.width,h:i.height,cw:canvas.clientWidth,ch:canvas.clientHeight,sw:canvas.scrollWidth,sh:canvas.scrollHeight,overflow:getComputedStyle(canvas).overflowX};
      });
      let initial=await geometry();
      assert.ok(Math.abs(initial.h/initial.w-1.5)<0.01,'initial preview keeps the complete portrait aspect ratio');
      assert.ok(initial.sh>=initial.h,'initial full-page natural height must not be cropped');
      for(let angle=0;angle<360;angle+=90) {
        if(angle) await page.click('[data-review-preview-rotate]');
        await page.click('[data-review-preview-fit-page]');
        await page.waitForTimeout(40);
        let box=await geometry();
        assert.ok(box.x>=-1 && box.y>=-1,`${viewport.width}px ${angle}° must not be clipped at negative coordinates: ${JSON.stringify(box)}`);
        assert.ok(box.x+box.w<=box.cw+1 && box.y+box.h<=box.ch+1,`fit-page must show every edge at ${angle}°: ${JSON.stringify(box)}`);
        assert.ok(Math.abs(box.x+box.w/2-box.cw/2)<2,`fit-page must be centered at ${angle}°`);
        await page.click('[data-review-preview-fit-width]');
        await page.selectOption('[data-review-preview-zoom]','2');
        await page.waitForTimeout(40);
        box=await geometry();
        assert.ok(box.sw>box.cw && ['auto','scroll'].includes(box.overflow),`zoom must retain horizontal scrolling at ${angle}°: ${JSON.stringify(box)}`);
        assert.ok(box.x>=-1 && box.y>=-1 && box.x+box.w<=box.sw+1 && box.y+box.h<=box.sh+1,`zoomed edges must be reachable at ${angle}°`);
        await page.click('[data-review-preview-zoom-out]');
        assert.equal(await page.inputValue('[data-review-preview-zoom]'),'1.75');
        await page.click('[data-review-preview-zoom-in]');
        assert.equal(await page.inputValue('[data-review-preview-zoom]'),'2');
      }
      await page.click('[data-review-preview-fit-page]');
      await page.click('[data-review-preview-thumbnail-toggle]');
      await page.waitForTimeout(40);
      let resized=await geometry();
      assert.ok(resized.x>=0 && resized.x+resized.w<=resized.cw+1,'hiding thumbnails recalculates width');
      await page.setViewportSize({width:viewport.width-50,height:viewport.height-100});
      await page.waitForTimeout(40);
      resized=await geometry();
      assert.ok(resized.x>=0 && resized.x+resized.w<=resized.cw+1,'window resizing keeps the entire image visible');
      assert.ok(Math.abs(resized.x+resized.w/2-resized.cw/2)<2,'resizing keeps image centered');
      await page.screenshot({path:path.join(root,`reports/upload-review-rotation-${viewport.width}.png`)});
      await page.close();
    }
    console.log('PASS: desktop/mobile 0/90/180/270 fit-page, fit-width, 200% scroll and zoom controls');
  } finally {await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
