const assert = require("node:assert/strict");
const path = require("node:path");
const { after, before, test } = require("node:test");

const { chromium } = require(
  process.env.AAIE_PLAYWRIGHT_MODULE ||
    "/Users/faqiang/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright"
);

const ROOT = path.resolve(__dirname, "../..");
const CHROME =
  process.env.AAIE_CHROME ||
  "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";

let browser;

before(async () => {
  browser = await chromium.launch({ executablePath: CHROME, headless: true });
});

after(async () => {
  if (browser) await browser.close();
});

async function renderHistory(page) {
  await page.setContent(`<!doctype html><html><head>
    <meta name="viewport" content="width=device-width,initial-scale=1">
    <style>body{margin:0;padding:16px;font:14px system-ui}.cards{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}.card{border:1px solid #ddd;border-radius:12px;padding:12px;min-width:0}@media(max-width:760px){.cards{grid-template-columns:1fr}}</style>
    </head><body><main class="cards">
      <section class="card student-transfer-summary">
        <ol class="student-transfer-record-list" aria-label="历次转学记录">
          <li><a href="/students/100/transfer-cases/22"><strong>Transfer #2</strong><span>进行中</span><small>Cycle #3 · Current School → Target School · 当前：等待 COE</small></a></li>
          <li><a href="/students/100/transfer-cases/21"><strong>Transfer #1</strong><span>已完成</span><small>Legacy · Lifecycle 未确认 · Old School → Current School</small></a></li>
        </ol>
      </section>
      <section class="card student-progression-summary">
        <ol class="student-progression-record-list" aria-label="历次升学记录">
          <li><a href="/students/100/progression-cases/32"><strong>Progression #2</strong><span>进行中</span><small>Cycle #4 · Future University · 当前：更新 COE</small></a></li>
          <li><a href="/students/100/progression-cases/31"><strong>Progression #1</strong><span>已完成</span><small>Legacy · Lifecycle 未确认 · Former College</small></a></li>
        </ol>
      </section>
    </main></body></html>`);
  await page.addStyleTag({
    path: path.join(ROOT, "assets/student-lifecycle-phase6.css"),
  });
}

for (const viewport of [
  { name: "mobile", width: 390, height: 844 },
  { name: "desktop", width: 1440, height: 900 },
]) {
  test(`${viewport.name} lifecycle histories are newest-first, enterable and overflow-safe`, async () => {
    const context = await browser.newContext({ viewport });
    const page = await context.newPage();
    await renderHistory(page);

    const transferLabels = await page
      .locator(".student-transfer-record-list strong")
      .allTextContents();
    const progressionLabels = await page
      .locator(".student-progression-record-list strong")
      .allTextContents();
    assert.deepEqual(transferLabels, ["Transfer #2", "Transfer #1"]);
    assert.deepEqual(progressionLabels, ["Progression #2", "Progression #1"]);
    assert.equal(
      await page.locator(".student-transfer-record-list a").nth(1).getAttribute("href"),
      "/students/100/transfer-cases/21"
    );
    assert.equal(
      await page.locator(".student-progression-record-list a").first().getAttribute("href"),
      "/students/100/progression-cases/32"
    );
    assert.equal(
      await page.getByText("Legacy · Lifecycle 未确认", { exact: false }).count(),
      2
    );
    const overflow = await page.evaluate(
      () => document.documentElement.scrollWidth - document.documentElement.clientWidth
    );
    assert.ok(overflow <= 0, `horizontal overflow ${overflow}`);
    await context.close();
  });
}

for (const viewport of [
  { name: "mobile", width: 390, height: 844 },
  { name: "desktop", width: 1440, height: 900 },
]) {
  test(`${viewport.name} business disposition control reveals only relevant readable fields`, async () => {
    const context = await browser.newContext({ viewport });
    const page = await context.newPage();
    await page.setContent(`<!doctype html><html><head>
      <meta name="viewport" content="width=device-width,initial-scale=1">
      </head><body><main style="max-width:760px;margin:auto">
      <div class="workflow-step"><div class="workflow-step-body">
        <strong class="workflow-step-title">付款</strong><small>待处理</small>
        <details class="lifecycle-module-business-control" open>
          <summary>调整本次处理</summary>
          <form data-lifecycle-business-disposition>
            <label>本轮处理<select name="handling" data-handling-select>
              <option value="new">本次需要</option>
              <option value="reuse">沿用现有</option>
              <option value="skip">无需处理</option>
            </select></label>
            <label data-existing-choice hidden>沿用记录<select name="existing_choice">
              <option value="payment:1">学校学费 · AUD 1200 · 2026-03-20</option>
              <option value="payment:2">学校学费 · AUD 900 · 2024-02-10</option>
            </select></label>
            <label data-na-reason hidden>说明<textarea name="reason"></textarea></label>
            <button type="submit">保存</button>
          </form>
        </details>
      </div></div></main></body></html>`);
    await page.addStyleTag({
      path: path.join(ROOT, "assets/student-lifecycle-phase6.css"),
    });
    await page.addScriptTag({
      path: path.join(ROOT, "assets/student-lifecycle-phase6.js"),
    });

    const handling = page.locator('[name="handling"]');
    await handling.selectOption("skip");
    assert.equal(await page.locator("[data-na-reason]").isVisible(), true);
    assert.equal(await page.locator("[data-existing-choice]").isVisible(), false);
    await handling.selectOption("reuse");
    assert.equal(await page.locator("[data-na-reason]").isVisible(), false);
    assert.equal(await page.locator("[data-existing-choice]").isVisible(), true);
    assert.match(
      await page.locator("[data-existing-choice]").innerText(),
      /学校学费 · AUD 1200 · 2026-03-20/
    );
    const text = await page.locator("main").innerText();
    for (const forbidden of ["Required", "Existing Record ID", "source_record_id"]) {
      assert.equal(text.includes(forbidden), false);
    }
    const overflow = await page.evaluate(
      () => document.documentElement.scrollWidth - document.documentElement.clientWidth
    );
    assert.ok(overflow <= 0, `horizontal overflow ${overflow}`);
    await context.close();
  });
}

test("Dashboard active cards activate the shared Lifecycle drill-down filter", async () => {
  const context = await browser.newContext({ viewport: { width: 390, height: 844 } });
  const page = await context.newPage();
  await page.setContent(`<!doctype html><html><body>
    <section data-lifecycle-stage-distribution>
      <div class="lifecycle-stage-filters">
        <button data-lifecycle-stage-filter="all" class="active" aria-pressed="true">ALL</button>
        <button data-lifecycle-stage-filter="transfer" aria-pressed="false">TRANSFER</button>
        <button data-lifecycle-stage-filter="progression" aria-pressed="false">PROGRESSION</button>
      </div>
      <div class="dashboard-lifecycle-active-cards">
        <button data-lifecycle-stage-filter="transfer" aria-pressed="false"><span>正在转学</span><strong>1</strong></button>
        <button data-lifecycle-stage-filter="progression" aria-pressed="false"><span>正在升学</span><strong>1</strong></button>
      </div>
      <details class="dashboard-workflow-distribution-chart__row">
        <summary><strong data-lifecycle-filter-count>2</strong></summary>
        <a data-lifecycle-type="transfer">Transfer Student</a>
        <a data-lifecycle-type="progression">Progression Student</a>
      </details>
    </section></body></html>`);
  await page.addScriptTag({
    path: path.join(ROOT, "assets/student-lifecycle-phase6.js"),
  });

  await page
    .locator('.dashboard-lifecycle-active-cards [data-lifecycle-stage-filter="transfer"]')
    .click();

  assert.equal(
    await page.locator('[data-lifecycle-type="transfer"]').getAttribute("hidden"),
    null
  );
  assert.equal(
    await page.locator('[data-lifecycle-type="progression"]').getAttribute("hidden"),
    ""
  );
  assert.equal(
    await page.locator("[data-lifecycle-filter-count]").textContent(),
    "1"
  );
  for (const control of await page
    .locator('[data-lifecycle-stage-filter="transfer"]')
    .all()) {
    assert.equal(await control.getAttribute("aria-pressed"), "true");
  }
  await context.close();
});
