const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const { chromium } = require(
  process.env.AAIE_PLAYWRIGHT_MODULE ||
    "/Users/faqiang/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright"
);

const baseUrl = process.env.AAIE_ACCEPTANCE_URL || "http://127.0.0.1:8898";
const username = process.env.AAIE_ACCEPTANCE_USERNAME || "admin";
const password = process.env.AAIE_ACCEPTANCE_PASSWORD || "TransferV2Browser!2026";
const activeStudent = process.env.AAIE_TRANSFER_ACTIVE_STUDENT || "99982828";
const activeCase = process.env.AAIE_TRANSFER_ACTIVE_CASE || "29";
const completedStudent = process.env.AAIE_TRANSFER_COMPLETED_STUDENT || "99990401";
const completedCase = process.env.AAIE_TRANSFER_COMPLETED_CASE || "27";
const evidenceDir = path.resolve(
  process.env.AAIE_ACCEPTANCE_EVIDENCE ||
    "SAFEBOX/transfer-v2-final-schema-implementation-20260904/evidence/browser"
);
const chromePath =
  process.env.AAIE_CHROME ||
  "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
const requestedViewport = process.env.AAIE_ACCEPTANCE_VIEWPORT || "";

fs.mkdirSync(evidenceDir, { recursive: true });

async function login(page) {
  await page.goto(`${baseUrl}/login`, { waitUntil: "networkidle" });
  await page.locator('input[name="username"]').fill(username);
  await page.locator('input[name="password"]').fill(password);
  await Promise.all([
    page.waitForLoadState("networkidle"),
    page.locator('button[type="submit"]').click(),
  ]);
  assert.equal(page.url().endsWith("/login"), false, `login failed: ${page.url()}`);
}

async function horizontalOverflow(page) {
  return page.evaluate(
    () => document.documentElement.scrollWidth - document.documentElement.clientWidth
  );
}

async function dismissReminder(page, checkpoint = () => {}) {
  const reminderClose = page.locator("[data-reminder-toast-close]");
  if ((await reminderClose.count()) > 0 && await reminderClose.isVisible()) {
    await reminderClose.click();
    assert.equal(await page.locator("[data-reminder-toast]").isVisible(), false);
    checkpoint("reminder-dismiss-pass");
  }
}

async function checkAutocomplete(page, checkpoint = () => {}) {
  const addTarget = page.locator(".transfer-v2-add-target summary");
  await addTarget.click();
  checkpoint("autocomplete-opened");
  const form = page.locator("form[data-transfer-v2-school-form]");
  const selector = form.locator("[data-transfer-v2-school-selector]");
  const input = selector.locator("[data-transfer-v2-school-name]");
  const identifier = selector.locator("[data-transfer-v2-school-id]");
  const suggestions = selector.locator(
    '[data-transfer-v2-school-suggestions] [role="option"]'
  );

  await input.fill("Mel");
  await page.waitForTimeout(350);
  checkpoint("autocomplete-mouse-results");
  assert.ok((await suggestions.count()) > 0, "two-character fuzzy search has no result");
  await suggestions.first().click();
  checkpoint("autocomplete-mouse-selected");
  assert.notEqual(await identifier.inputValue(), "", "mouse selection did not bind school_id");
  assert.notEqual(
    await selector.locator("[data-transfer-v2-school-state]").inputValue(),
    "",
    "mouse selection did not bind canonical state"
  );

  await input.press("End");
  await input.type(" edited");
  checkpoint("autocomplete-edit-cleared");
  assert.equal(await identifier.inputValue(), "", "editing selected text did not clear school_id");
  for (const fact of ["state", "region", "type"]) {
    assert.equal(
      await selector.locator(`[data-transfer-v2-school-${fact}]`).inputValue(),
      "",
      `editing selected text did not clear canonical ${fact}`
    );
  }

  await input.fill("Ca");
  await page.waitForTimeout(350);
  assert.ok((await suggestions.count()) > 0, "keyboard fuzzy search has no result");
  await input.press("ArrowDown");
  await input.press("Enter");
  checkpoint("autocomplete-keyboard-selected");
  assert.notEqual(await identifier.inputValue(), "", "Arrow/Enter did not bind school_id");

  await input.fill("Ma");
  await page.waitForTimeout(350);
  assert.ok((await suggestions.count()) > 0, "Escape precondition has no result");
  await input.press("Escape");
  checkpoint("autocomplete-escape-pass");
  assert.equal(
    await selector.locator("[data-transfer-v2-school-suggestions]").isHidden(),
    true,
    "Escape did not close suggestions"
  );

  await input.fill("No Existing Manual School");
  await page.waitForTimeout(350);
  assert.equal(await identifier.inputValue(), "", "manual school retained an identifier");
  assert.equal(await input.inputValue(), "No Existing Manual School");
  checkpoint("autocomplete-manual-pass");

  await input.fill("Mel");
  await page.waitForTimeout(350);
  assert.ok((await suggestions.count()) > 0, "outside-click precondition has no result");
  await page.locator(".lifecycle-workspace__header h1").click();
  assert.equal(
    await selector.locator("[data-transfer-v2-school-suggestions]").isHidden(),
    true,
    "outside click did not close suggestions"
  );
  checkpoint("autocomplete-outside-pass");
}

async function acceptViewport(browser, viewport) {
  const checkpoint = (name) => process.stdout.write(`[${viewport.name}] ${name}\n`);
  const context = await browser.newContext({
    viewport: { width: viewport.width, height: viewport.height },
  });
  const page = await context.newPage();
  page.setDefaultTimeout(8000);
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
  checkpoint("login-pass");

  await page.goto(`${baseUrl}/students/${activeStudent}`, { waitUntil: "networkidle" });
  const profileText = await page.locator("body").innerText();
  for (const label of [
    "资料准备", "学校方案", "学校申请", "已收到 Offer", "已接受并付款",
    "更新 COE", "待结束", "已完成",
  ]) {
    assert.ok(profileText.includes(label), `Student Profile missing lifecycle label: ${label}`);
  }
  assert.equal(await page.locator(".lifecycle-stepper").count(), 1);
  assert.ok((await horizontalOverflow(page)) <= 0, "Student Profile overflows horizontally");
  await page.screenshot({
    path: path.join(evidenceDir, `${viewport.name}-active-student-profile.png`),
    fullPage: true,
  });
  checkpoint("profile-pass");

  await page.goto(
    `${baseUrl}/students/${activeStudent}/transfer-cases/${activeCase}`,
    { waitUntil: "networkidle" }
  );
  checkpoint("transfer-loaded");
  await dismissReminder(page, checkpoint);
  const workspace = page.locator("[data-transfer-v2-workspace]");
  assert.equal(await workspace.count(), 1);
  assert.equal(await workspace.locator(".lifecycle-stepper").count(), 0);
  const workspaceText = await workspace.innerText();
  for (const forbidden of [
    "Branch", "Overview", "History", "materials_preparation",
    "application_in_progress", "resolver", "projection", "canonical", "UID",
  ]) {
    assert.equal(workspaceText.includes(forbidden), false, `internal term exposed: ${forbidden}`);
  }
  for (const tab of ["overview", "branches", "files", "offers", "payments", "coe", "history"]) {
    await workspace.locator(`[data-transfer-v2-tab="${tab}"]`).click();
    assert.equal(
      await workspace.locator(`[data-transfer-v2-panel="${tab}"]`).isVisible(),
      true,
      `tab did not activate: ${tab}`
    );
    checkpoint(`tab-${tab}-pass`);
  }
  await workspace.locator('[data-transfer-v2-tab="branches"]').click();
  await checkAutocomplete(page, checkpoint);
  checkpoint("autocomplete-pass");
  const override = workspace.locator("[data-transfer-v2-missing-override]");
  assert.ok((await override.count()) >= 1, "missing-document override is absent");
  const missingText = await override.first().locator("xpath=..").innerText();
  assert.match(missingText, /以下材料仍待补/);
  assert.match(missingText, /仍有材料待补，继续申请/);
  let dialogSeen = false;
  page.once("dialog", async (dialog) => {
    dialogSeen = true;
    assert.match(dialog.message(), /材料尚未齐全/);
    await dialog.dismiss();
  });
  await override.first().locator('button[type="submit"]').click();
  assert.equal(dialogSeen, true, "missing-document confirmation dialog was not shown");
  assert.ok((await horizontalOverflow(page)) <= 0, "Transfer workspace overflows horizontally");
  await page.screenshot({
    path: path.join(evidenceDir, `${viewport.name}-active-transfer-targets.png`),
    fullPage: true,
  });
  checkpoint("active-transfer-pass");

  await page.goto(
    `${baseUrl}/students/${completedStudent}/transfer-cases/${completedCase}`,
    { waitUntil: "networkidle" }
  );
  checkpoint("completed-transfer-loaded");
  await dismissReminder(page, checkpoint);
  const completed = page.locator("[data-transfer-v2-workspace]");
  const completedText = await completed.innerText();
  assert.match(completedText, /转学完成/);
  assert.match(completedText, /Melbourne High School/);
  await completed.locator('[data-transfer-v2-tab="payments"]').click();
  assert.match(await completed.innerText(), /付款记录 #51/);
  await completed.locator('[data-transfer-v2-tab="coe"]').click();
  assert.match(await completed.innerText(), /入学确认记录 #230/);
  assert.ok((await horizontalOverflow(page)) <= 0, "Completed Transfer overflows horizontally");
  checkpoint("completed-transfer-pass");

  await page.goto(baseUrl, { waitUntil: "networkidle" });
  const dashboardText = await page.locator("body").innerText();
  assert.ok(
    dashboardText.includes("资料准备") || dashboardText.includes("学校方案"),
    "Dashboard does not show the active new Transfer stage"
  );
  assert.ok((await horizontalOverflow(page)) <= 0, "Dashboard overflows horizontally");
  await page.screenshot({
    path: path.join(evidenceDir, `${viewport.name}-dashboard.png`),
    fullPage: true,
  });
  checkpoint("dashboard-pass");

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
  checkpoint("viewport-pass");
  return {
    viewport,
    horizontal_overflow: 0,
    profile_stepper_count: 1,
    transfer_stepper_count: 0,
    autocomplete: "mouse-keyboard-edit-clear-manual-escape-outside-pass",
    tabs: "pass",
    missing_document_confirmation: "pass",
    completed_payment_record: 51,
    completed_coe_record: 230,
    unexpected_console: unexpectedConsole,
    request_failures: requestFailures,
  };
}

(async () => {
  const browser = await chromium.launch({ executablePath: chromePath, headless: true });
  try {
    const results = [];
    const viewports = [
      { name: "desktop-1280x720", width: 1280, height: 720 },
      { name: "desktop-1440x900", width: 1440, height: 900 },
      { name: "desktop-1920x1080", width: 1920, height: 1080 },
      { name: "mobile-390x844", width: 390, height: 844 },
    ].filter((viewport) => !requestedViewport || viewport.name === requestedViewport);
    assert.ok(viewports.length > 0, `unknown viewport: ${requestedViewport}`);
    for (const viewport of viewports) {
      results.push(await acceptViewport(browser, viewport));
    }
    const evidence = {
      passed: true,
      base_url: baseUrl,
      active_student: activeStudent,
      active_case: Number(activeCase),
      completed_student: completedStudent,
      completed_case: Number(completedCase),
      results,
    };
    fs.writeFileSync(
      path.join(
        evidenceDir,
        `transfer-v2-final-schema-browser-acceptance${requestedViewport ? `-${requestedViewport}` : ""}.json`
      ),
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
