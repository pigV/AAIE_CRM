#!/usr/bin/env node
"use strict";

const fs = require("fs");
const path = require("path");
const { chromium } = require(
  "/Users/faqiang/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright"
);

async function readConfig() {
  let raw = "";
  for await (const chunk of process.stdin) raw += chunk;
  const config = JSON.parse(raw);
  for (const key of ["baseUrl", "artifactDir", "runtimeFileDir", "students", "accounts"]) {
    if (!config[key]) throw new Error(`missing ${key}`);
  }
  return config;
}

async function login(page, baseUrl, username, password) {
  await page.goto(`${baseUrl}/login`, { waitUntil: "domcontentloaded" });
  await page.locator('input[name="username"]').fill(username);
  await page.locator('input[name="password"]').fill(password);
  await Promise.all([
    page.waitForURL(url => !url.pathname.endsWith("/login")),
    page.locator('form[action="/login"] button[type="submit"]').click(),
  ]);
}

async function relogin(page, baseUrl, account) {
  await page.context().clearCookies();
  await login(page, baseUrl, account.username, account.password);
}

async function submitForm(page, form, expectedText) {
  await Promise.all([
    page.waitForLoadState("domcontentloaded"),
    form.locator('button[type="submit"],button:not([type])').last().click(),
  ]);
  if (expectedText) await page.getByText(expectedText, { exact: false }).first().waitFor();
}

async function verifySource(page, baseUrl, studentId, staff) {
  await relogin(page, baseUrl, staff);
  await page.goto(`${baseUrl}/students/${studentId}/transfer-cases`, { waitUntil: "domcontentloaded" });
  const source = page.locator('[data-transfer-source-evidence] form');
  await source.waitFor();
  const option = source.locator('select[name="source_existing_evidence"] option').nth(1);
  if (!(await option.getAttribute("value"))) throw new Error("Existing dual-evidence option absent");
  await source.locator('select[name="source_existing_evidence"]').selectOption({ index: 1 });
  await submitForm(page, source, "来源已核验");
  if (page.url().includes("tab=contracts")) throw new Error("Staff was redirected to Contract page");
  await page.getByText("由 Admin/Manager 生成并完成转学合同", { exact: false }).waitFor();
}

async function generateContract(page, baseUrl, studentId, manager) {
  await relogin(page, baseUrl, manager);
  await page.goto(`${baseUrl}/students/${studentId}?tab=contracts`, { waitUntil: "domcontentloaded" });
  const form = page.locator('form[action$="/contracts/study-abroad/generate"]').first();
  await form.waitFor();
  const type = form.locator('input[name="contract_type"][value="TransferApplicationContract"]');
  await type.check();
  // Exercise the same required contact step a Manager must complete in the UI.
  // The synthetic fixture intentionally has no real parent contact details.
  await form.locator('input[name="parent_phone"]').fill("0400000000");
  await form.locator('input[name="parent_email"]').fill(`parent-${studentId}@example.test`);
  const method = form.locator('select[name="service_fee_currency"]');
  await method.selectOption({ label: /Bank Transfer|Australian Company Account/i }).catch(async () => {
    await method.selectOption({ index: 1 });
  });
  const baseFee = await form.locator('[data-contract-published-standard-fee], [data-contract-service-fee]').first().inputValue();
  if (!/1,?200(?:\.00)?/.test(baseFee)) {
    throw new Error(`Existing Transfer UI base fee was not AUD 1,200: ${baseFee}`);
  }
  await submitForm(page, form);
  const transferTitle = /转学申请服务委托合同|转学申请服务合同|K.?12 Transfer/i;
  const row = page.locator('[data-contract-row]').filter({ hasText: transferTitle }).first();
  await row.waitFor();
  let copy = row.locator('[data-copy*="/contract-sign/"]').first();
  if (!(await copy.count())) {
    const generate = row.locator(`form[action*="/students/${studentId}/contracts/"][action$="/signing-link/generate"]`).first();
    await submitForm(page, generate);
    copy = page.locator('[data-contract-row]').filter({ hasText: transferTitle })
      .first().locator('[data-copy*="/contract-sign/"]').first();
  }
  await copy.waitFor();
  return await copy.getAttribute("data-copy");
}

async function parentSign(page, baseUrl, href, studentId) {
  await page.context().clearCookies();
  await page.goto(new URL(href, baseUrl).href, { waitUntil: "domcontentloaded" });
  const loginForm = page.locator('form[action$="/login"]');
  if (await loginForm.count()) {
    await loginForm.locator('input[name="wechat_nickname"]').fill(`Synthetic Parent ${studentId}`);
    await loginForm.locator('input[name="wechat_openid"]').fill(`e2e-openid-${studentId}`);
    await submitForm(page, loginForm, "在线签署确认");
  }
  await page.locator('input[name="declaration"]').check();
  await page.locator('input[name="parent_name"]').fill(`Synthetic Parent ${studentId}`);
  const signature = await page.evaluate(() => {
    const canvas = document.createElement("canvas"); canvas.width = 420; canvas.height = 150;
    const ctx = canvas.getContext("2d"); ctx.strokeStyle = "#142341"; ctx.lineWidth = 9;
    ctx.beginPath(); ctx.moveTo(20, 100); ctx.lineTo(100, 40); ctx.lineTo(180, 110); ctx.lineTo(390, 70); ctx.stroke();
    return canvas.toDataURL("image/png");
  });
  await page.locator('input[name="signature_data"],textarea[name="signature_data"]').evaluate((element, value) => { element.value = value; }, signature);
  await submitForm(page, page.locator('form[action$="/submit"]'), "合同已完成签署");
}

async function createTransfer(page, baseUrl, studentId, staff, includeSecondTarget = true) {
  await relogin(page, baseUrl, staff);
  await page.goto(`${baseUrl}/students/${studentId}/transfer-cases`, { waitUntil: "domcontentloaded" });
  const form = page.locator('[data-transfer-v2-create] form.transfer-v2-create-form');
  await form.waitFor();
  await form.locator('select[name="current_year_level"]').selectOption("Year 8");
  await form.locator('select[name="application_year_level"]').selectOption("Year 9");
  await form.locator('input[name="transfer_year"]').fill("2027");
  await form.locator('select[name="transfer_term"]').selectOption("Term 1");
  const selectSchool = async (row, schoolName) => {
    const input = row.locator('[data-transfer-v2-school-name]');
    await input.fill(schoolName);
    const suggestion = row.locator('[data-transfer-v2-school-suggestions] [role="option"]', {
      hasText: schoolName,
    });
    await suggestion.waitFor({ state: "visible" });
    await suggestion.click();
    const selectedId = await row.locator('[data-transfer-v2-school-id]').inputValue();
    if (!selectedId) throw new Error(`school library selection did not bind ${schoolName}`);
  };
  const target = form.locator('[data-transfer-v2-target-row]').first();
  await selectSchool(target, "E2E Target Grammar");
  if (includeSecondTarget) {
    await form.locator('[data-transfer-v2-add-target]').click();
    const target2 = form.locator('[data-transfer-v2-target-row]').nth(1);
    await selectSchool(target2, "E2E Target High");
  }
  await submitForm(page, form);
  if (!page.url().includes("/transfer-cases/")) throw new Error("Transfer detail was not opened");
  return page.url();
}

async function advanceBranchesAndOffer(page, studentId) {
  await page.locator('[data-transfer-v2-tab="branches"]').click();
  const branches = page.locator('[data-transfer-v2-branch-id]');
  if (await branches.count() !== 2) throw new Error("two target branches required");
  for (let i = 0; i < 2; i += 1) {
    await page.locator('[data-transfer-v2-tab="branches"]').click();
    const form = branches.nth(i).locator('form').filter({ has: page.locator('input[name="new_state"][value="application_submitted"]') }).first();
    await submitForm(page, form);
  }
  await page.goto(new URL(`/students/${studentId}?tab=offer`, page.url()).href, { waitUntil: "domcontentloaded" });
  await page.locator('[data-dialog-open="offer-dialog"]').click();
  const offer = page.locator('#offer-dialog form');
  await offer.locator('select[name="transfer_branch_id"]').selectOption({ index: 1 });
  await offer.locator('input[name="school_name"]').fill("E2E Target Grammar");
  await offer.locator('input[name="offer_date"]').fill("17/09/2026");
  await offer.locator('select[name="offer_status"]').selectOption("signed");
  await submitForm(page, offer);
}

async function rejectedPost(page, url, form, label) {
  const response = await page.request.post(url, { form, maxRedirects: 0 });
  if (response.status() < 400) throw new Error(`${label} did not fail closed (HTTP ${response.status()})`);
  return { label, status: response.status() };
}

async function acceptPayCoeComplete(page, transferUrl, studentId, artifactDir, runtimeFileDir) {
  const negatives = [];
  await page.goto(transferUrl, { waitUntil: "domcontentloaded" });
  await page.locator('[data-transfer-v2-tab="offers"]').click();
  const confirmOffer = page.getByRole("button", { name: /确认录取通知有效/ }).first();
  if (await confirmOffer.count()) {
    await confirmOffer.click();
    await page.waitForLoadState("domcontentloaded");
    await page.locator('[data-transfer-v2-tab="offers"]').click();
  }
  const acceptForm = page.getByRole("button", { name: /接受此录取通知版本/ }).first().locator("xpath=ancestor::form");
  const hidden = async name => await acceptForm.locator(`input[name="${name}"]`).inputValue();
  const branchIds = await page.locator('[data-transfer-v2-branch-id]').evaluateAll(nodes => nodes.map(node => node.getAttribute("data-transfer-v2-branch-id")));
  const acceptedBranch = await hidden("branch_id");
  const completedCycleId = await hidden("cycle_id");
  if (!/^\d+$/.test(completedCycleId)) throw new Error("active Transfer Cycle ID unavailable before completion");
  const wrongBranch = branchIds.find(value => value !== acceptedBranch);
  negatives.push(await rejectedPost(page, transferUrl.replace(/#.*/, "") + "/action", {
    csrf: await hidden("csrf"), action: "accept_offer", cycle_id: completedCycleId,
    branch_id: wrongBranch, offer_event_id: await hidden("offer_event_id"),
    idempotency_key: `wrong-branch-${studentId}`,
  }, "wrong-branch Offer ownership"));
  const accept = page.getByRole("button", { name: /接受此录取通知版本/ }).first();
  await accept.click(); await page.waitForLoadState("domcontentloaded");
  await page.locator('[data-transfer-v2-tab="payments"]').click();
  await page.getByText("等待付款", { exact: false }).waitFor();
  const acceptedAction = page.locator('form').filter({ has: page.locator('input[name="acceptance_id"]') }).first();
  const actionValue = async name => await acceptedAction.locator(`input[name="${name}"]`).inputValue();
  negatives.push(await rejectedPost(page, transferUrl.replace(/#.*/, "") + "/action", {
    csrf: await actionValue("csrf"), action: "confirm_coe", cycle_id: await actionValue("cycle_id"),
    branch_id: await actionValue("branch_id"), acceptance_id: await actionValue("acceptance_id"),
    coe_oshc_id: "999999999", issued_date: "2026-09-17",
    idempotency_key: `coe-before-payment-${studentId}`,
  }, "COE before exact confirmed Payment"));

  fs.mkdirSync(runtimeFileDir, { recursive: true });
  const receipt = path.join(runtimeFileDir, `${studentId}-receipt.pdf`);
  fs.writeFileSync(receipt, Buffer.from("%PDF-1.4\n% synthetic payment proof\n%%EOF\n"));
  await page.goto(new URL(`/students/${studentId}?tab=payments`, page.url()).href, { waitUntil: "domcontentloaded" });
  await page.locator('[data-dialog-open="payment-dialog"]').click();
  const payment = page.locator(`#payment-dialog form`);
  await payment.locator('select[name="transfer_branch_id"]').selectOption({ index: 1 });
  await payment.locator('select[name="payment_category"]').selectOption("Offer Deposit");
  await payment.locator('input[name="amount"]').fill("1200");
  await payment.locator('select[name="currency"]').selectOption("AUD");
  await payment.locator('input[name="paid_date"]').fill("17/09/2026");
  await payment.locator('input[type="file"]').setInputFiles(receipt);
  await submitForm(page, payment);
  const review = page.locator('form[action*="/upload-review/"][action$="/confirm"]').first();
  if (await review.count()) {
    await review.locator('input[name^="confirm_item_"]').first().check();
    await review.locator('select[name^="document_type_"]').first().selectOption("OfferDepositReceipt");
    await Promise.all([
      page.waitForLoadState("domcontentloaded"),
      review.locator("button.upload-review-confirm-action").click(),
    ]);
  }
  const appendProof = page.getByRole("button", { name: /补交凭证/ }).first();
  if (await appendProof.count()) {
    const appendForm = appendProof.locator("xpath=ancestor::form");
    await appendForm.locator('input[name="proof"]').setInputFiles(receipt);
    await submitForm(page, appendForm, "凭证已补交");
  }
  const confirmPaymentButton = page.getByRole("button", { name: /确认.*付款凭证/ }).first();
  if (await confirmPaymentButton.count()) {
    await submitForm(
      page,
      confirmPaymentButton.locator("xpath=ancestor::form"),
      "付款凭证已确认",
    );
  }

  await page.goto(new URL(`/students/${studentId}?tab=coe`, page.url()).href, { waitUntil: "domcontentloaded" });
  await page.locator('[data-dialog-open="coe-dialog"]').click();
  const coe = page.locator('#coe-dialog form');
  const coeBranch = coe.locator('select[name="transfer_branch_id"]');
  if (await coeBranch.count()) await coeBranch.selectOption({ index: 1 });
  await coe.locator('select[name="document_kind"]').selectOption("COE");
  await coe.locator('input[name="coe_provider"]').fill("E2E Target Grammar");
  await coe.locator('input[name="coe_course"]').fill("Year 9");
  await coe.locator('input[name="coe_start_date"]').fill("27/01/2027");
  await coe.locator('input[name="coe_end_date"]').fill("15/12/2027");
  await submitForm(page, coe);

  await page.goto(transferUrl, { waitUntil: "domcontentloaded" });
  await page.locator('[data-transfer-v2-tab="coe"]').click();
  const confirmCoeButton = page.getByRole("button", { name: /确认已取得入学确认|确认入学确认|确认 COE/ }).first();
  const confirmCoeForm = confirmCoeButton.locator("xpath=ancestor::form");
  await confirmCoeForm.locator('select[name="coe_oshc_id"]').selectOption({ index: 1 });
  await confirmCoeForm.locator('input[name="issued_date"]').fill("2026-09-17");
  await submitForm(page, confirmCoeForm, "Transfer V2 已更新");
  const completeApplication = page.getByRole("button", { name: /确认学校申请完成/ }).first();
  if (await completeApplication.count()) { await completeApplication.click(); await page.waitForLoadState("domcontentloaded"); }
  const completion = page.locator('form[data-transfer-v2-action]');
  await completion.locator('select[name="update_current_year_level"]').selectOption("1");
  await completion.locator('select[name="close_unfinished_branches"]').selectOption("1");
  await submitForm(page, completion, "转学完成");

  // Idempotent refresh/new-session persistence.
  await page.reload({ waitUntil: "domcontentloaded" });
  await page.getByText("转学完成", { exact: false }).first().waitFor();
  negatives.push(await rejectedPost(page, transferUrl.replace(/#.*/, "") + "/action", {
    csrf: await page.locator('input[name="csrf"]').first().inputValue(), action: "branch_transition",
    cycle_id: completedCycleId,
    branch_id: acceptedBranch, new_state: "awaiting_result", idempotency_key: `completed-mutation-${studentId}`,
  }, "completed-cycle mutation"));
  await page.reload({ waitUntil: "domcontentloaded" });
  await page.getByText("转学完成", { exact: false }).first().waitFor();
  return negatives;
}

async function runJourney(browser, config, studentId, mode) {
  const viewport = mode === "mobile" ? { width: 390, height: 844 } : { width: 1440, height: 1000 };
  const context = await browser.newContext({ viewport });
  const page = await context.newPage();
  try {
    await verifySource(page, config.baseUrl, studentId, config.accounts.staff);
    const link = await generateContract(page, config.baseUrl, studentId, config.accounts.manager);
    await parentSign(page, config.baseUrl, link, studentId);
    const transferUrl = await createTransfer(page, config.baseUrl, studentId, config.accounts.staff);
    await advanceBranchesAndOffer(page, studentId);
    const negativeChecks = await acceptPayCoeComplete(
      page, transferUrl, studentId, config.artifactDir, config.runtimeFileDir
    );
    // Begin a second Transfer through the same formal Staff -> Manager -> Parent handoff.
    await verifySource(page, config.baseUrl, studentId, config.accounts.staff);
    const secondLink = await generateContract(page, config.baseUrl, studentId, config.accounts.manager);
    await parentSign(page, config.baseUrl, secondLink, `${studentId}-second`);
    await createTransfer(page, config.baseUrl, studentId, config.accounts.staff, false);
    await page.screenshot({ path: path.join(config.artifactDir, `${mode}-${studentId}-completed.png`), fullPage: true });
    return { passed: true, studentId, viewport, negativeChecks, finalUrl: page.url() };
  } catch (error) {
    await page.screenshot({ path: path.join(config.artifactDir, `${mode}-${studentId}-failure.png`), fullPage: true }).catch(() => {});
    return { passed: false, studentId, viewport, error: String(error && error.stack || error), finalUrl: page.url() };
  } finally {
    await context.close();
  }
}

(async () => {
  const config = await readConfig();
  fs.mkdirSync(config.artifactDir, { recursive: true });
  const browser = await chromium.launch({
    headless: true,
    executablePath: "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
  });
  try {
    const desktop = await runJourney(browser, config, config.students[0], "desktop");
    const mobile = await runJourney(browser, config, config.students[1], "mobile");
    const result = { desktop, mobile, passed: desktop.passed && mobile.passed };
    process.stdout.write(JSON.stringify(result));
    process.exitCode = result.passed ? 0 : 1;
  } finally {
    await browser.close();
  }
})().catch(error => {
  process.stdout.write(JSON.stringify({ passed: false, fatal: String(error && error.stack || error) }));
  process.exitCode = 1;
});
