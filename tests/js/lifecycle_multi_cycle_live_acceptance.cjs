const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const { chromium } = require(
  process.env.AAIE_PLAYWRIGHT_MODULE ||
    "/Users/faqiang/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright"
);

const baseUrl = process.env.AAIE_ACCEPTANCE_URL || "http://127.0.0.1:8897";
const username = process.env.AAIE_ACCEPTANCE_USERNAME || "admin";
const password = process.env.AAIE_ACCEPTANCE_PASSWORD || "LifecycleGate!2026";
const transferStudent = process.env.AAIE_TRANSFER_STUDENT || "99982401";
const progressionStudent = process.env.AAIE_PROGRESSION_STUDENT || "99982402";
const evidenceDir = path.resolve(
  process.env.AAIE_ACCEPTANCE_EVIDENCE ||
    "SAFEBOX/lifecycle-multi-cycle-convergence-20260824/runtime/browser"
);
const chromePath =
  process.env.AAIE_CHROME ||
  "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";

fs.mkdirSync(evidenceDir, { recursive: true });

async function login(page) {
  await page.goto(`${baseUrl}/login`, { waitUntil: "networkidle" });
  await page.locator('input[name="username"]').fill(username);
  await page.locator('input[name="password"]').fill(password);
  await Promise.all([
    page.waitForLoadState("networkidle"),
    page.locator('button[type="submit"]').click(),
  ]);
  assert.ok(!page.url().endsWith("/login"), `login failed: ${page.url()}`);
}

async function overflow(page) {
  return page.evaluate(
    () => document.documentElement.scrollWidth - document.documentElement.clientWidth
  );
}

async function acceptViewport(browser, viewport) {
  const context = await browser.newContext({
    viewport: { width: viewport.width, height: viewport.height },
  });
  const page = await context.newPage();
  const consoleProblems = [];
  const requestFailures = [];
  page.on("console", (message) => {
    if (["error", "warning"].includes(message.type())) {
      consoleProblems.push(`${message.type()}: ${message.text()}`);
    }
  });
  page.on("requestfailed", (request) => {
    requestFailures.push(`${request.method()} ${request.url()}: ${request.failure()?.errorText}`);
  });
  await login(page);

  await page.goto(`${baseUrl}/students/${transferStudent}`, {
    waitUntil: "networkidle",
  });
  const transferRecords = page.locator(".student-transfer-record-list li");
  assert.equal(await transferRecords.count(), 2);
  assert.match(await transferRecords.first().innerText(), /Transfer #2/);
  assert.match(await transferRecords.nth(1).innerText(), /Transfer #1/);
  const renderedDispositions = await page
    .locator("[data-module-disposition]")
    .evaluateAll((items) => items.map((item) => item.dataset.moduleDisposition));
  assert.ok(
    (await page.locator('[data-module-disposition="existing"]').count()) >= 1,
    `Existing disposition is absent from current Transfer rail: ${JSON.stringify(renderedDispositions)}`
  );
  assert.ok(
    (await page.locator('[data-module-disposition="not_applicable"]').count()) >= 1,
    "N/A disposition is absent from current Transfer rail"
  );
  const transferWorkflowText = await page.locator("#workflow-progress").innerText();
  assert.match(transferWorkflowText, /沿用现有签证/);
  assert.match(transferWorkflowText, /无需处理/);
  for (const forbidden of [
    "设置处理方式", "记录处理方式", "Existing来源", "Existing记录ID",
  ]) {
    assert.equal(
      transferWorkflowText.includes(forbidden),
      false,
      `technical field leaked: ${forbidden}`
    );
  }
  assert.equal(
    await page.locator('[name="source_record_id"], [name="source_record_type"]').count(),
    0
  );
  assert.ok((await overflow(page)) <= 0, "Transfer Profile overflows horizontally");
  await page.screenshot({
    path: path.join(evidenceDir, `${viewport.name}-transfer-profile.png`),
    fullPage: true,
  });
  const transferHref = await transferRecords.first().locator("a").getAttribute("href");
  assert.match(transferHref, new RegExp(`/students/${transferStudent}/transfer-cases/\\d+`));
  await Promise.all([
    page.waitForLoadState("networkidle"),
    transferRecords.first().locator("a").click(),
  ]);
  assert.match(page.url(), /\/transfer-cases\/\d+$/);

  await page.goto(`${baseUrl}/students/${progressionStudent}`, {
    waitUntil: "networkidle",
  });
  const progressionRecords = page.locator(".student-progression-record-list li");
  assert.equal(await progressionRecords.count(), 2);
  assert.match(await progressionRecords.first().innerText(), /Progression #2/);
  assert.match(await progressionRecords.nth(1).innerText(), /Progression #1/);
  assert.ok(
    (await overflow(page)) <= 0,
    "Progression Profile overflows horizontally"
  );
  const progressionHref = await progressionRecords
    .first()
    .locator("a")
    .getAttribute("href");
  assert.match(
    progressionHref,
    new RegExp(`/students/${progressionStudent}/progression-cases/\\d+`)
  );
  await page.screenshot({
    path: path.join(evidenceDir, `${viewport.name}-progression-profile.png`),
    fullPage: true,
  });

  await page.goto(baseUrl, { waitUntil: "networkidle" });
  const transferCard = page.locator(
    '.dashboard-lifecycle-active-card[data-lifecycle-stage-filter="transfer"]'
  );
  const progressionCard = page.locator(
    '.dashboard-lifecycle-active-card[data-lifecycle-stage-filter="progression"]'
  );
  assert.ok(Number(await transferCard.locator("strong").textContent()) >= 1);
  assert.ok(Number(await progressionCard.locator("strong").textContent()) >= 1);
  await transferCard.click();
  assert.equal(await transferCard.getAttribute("aria-pressed"), "true");
  assert.ok(
    (await page.locator('[data-lifecycle-type="transfer"]:not([hidden])').count()) >= 1
  );
  assert.equal(
    await page.locator('[data-lifecycle-type="progression"]:not([hidden])').count(),
    0
  );
  assert.ok((await overflow(page)) <= 0, "Dashboard overflows horizontally");
  await page.screenshot({
    path: path.join(evidenceDir, `${viewport.name}-dashboard-transfer.png`),
    fullPage: true,
  });

  const knownBaselineConsole = consoleProblems.filter(
    (item) =>
      item.includes("Applying inline style violates") ||
      item.includes("Failed to load resource: the server responded with a status of 404")
  );
  const unexpectedConsole = consoleProblems.filter(
    (item) => !knownBaselineConsole.includes(item)
  );
  assert.deepEqual(unexpectedConsole, []);
  assert.deepEqual(requestFailures, []);
  await context.close();
  return {
    viewport,
    transfer_records: 2,
    progression_records: 2,
    transfer_href: transferHref,
    progression_href: progressionHref,
    console_problems: unexpectedConsole,
    known_preexisting_console_baseline: knownBaselineConsole,
    request_failures: requestFailures,
    horizontal_overflow: 0,
  };
}

(async () => {
  const browser = await chromium.launch({
    executablePath: chromePath,
    headless: true,
  });
  try {
    const results = [];
    for (const viewport of [
      { name: "mobile-390x844", width: 390, height: 844 },
      { name: "desktop-1440x900", width: 1440, height: 900 },
    ]) {
      results.push(await acceptViewport(browser, viewport));
    }
    const evidence = {
      passed: true,
      base_url: baseUrl,
      transfer_student: transferStudent,
      progression_student: progressionStudent,
      results,
    };
    fs.writeFileSync(
      path.join(evidenceDir, "browser-acceptance.json"),
      `${JSON.stringify(evidence, null, 2)}\n`,
      "utf8"
    );
    process.stdout.write(`${JSON.stringify(evidence)}\n`);
  } finally {
    await browser.close();
  }
})().catch((error) => {
  process.stderr.write(`${error.stack || error}\n`);
  process.exitCode = 1;
});
