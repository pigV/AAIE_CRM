const assert = require("node:assert/strict");
const fs = require("node:fs");
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
const CSS_STACK = [
  "assets/style.css",
  "assets/ui-premium-v2.css",
  "assets/student-followup-notes-v1.css",
  "assets/student-detail-summary-cards-v1.css",
  "assets/student-lifecycle-phase6.css",
];

let browser;

before(async () => {
  browser = await chromium.launch({ executablePath: CHROME, headless: true });
});

after(async () => {
  if (browser) await browser.close();
});

async function renderContractForm(page) {
  await page.setContent(`
    <!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1"></head>
    <body class="ui-premium-v2"><main class="main">
      <section class="student-summary-card panel"></section><div class="tabs" data-tabs></div>
      <div class="tab-panel active" data-panel="contracts">
        <section class="contract-grid" id="contracts-section"><div class="panel contract-card active-contract">
          <form class="stack-form contract-form">
            <section class="contract-section"><h3>合同类型</h3>
              <div class="choices contract-type-choices">
                <label class="choice wide contract-type-choice"><input type="radio" name="contract_type" value="NewInternationalStudentContract" checked><span><strong>新留学生合同</strong><small>澳洲留学咨询及签证服务委托合同</small></span></label>
                <label class="choice wide contract-type-choice"><input type="radio" name="contract_type" value="TransferApplicationContract"><span><strong>转学申请合同</strong><small>转学申请合同</small></span></label>
                <label class="choice wide contract-type-choice"><input type="radio" name="contract_type" value="LocalStudentApplicationContract"><span><strong>本地生申请合同</strong><small>本地生申请合同</small></span></label>
              </div>
            </section>
            <section class="contract-section"><h3>乙方签署人</h3>
              <div class="choices contract-signer-choices">
                <label class="choice wide"><input type="radio" name="parent_signer_source" value="mother" checked><span>母亲</span></label>
                <label class="choice wide"><input type="radio" name="parent_signer_source" value="father"><span>父亲</span></label>
              </div>
            </section>
          </form>
        </div></section>
      </div>
    </main></body></html>
  `);
  for (const relativePath of CSS_STACK) {
    await page.addStyleTag({ path: path.join(ROOT, relativePath) });
  }
}

async function optionEvidence(page) {
  return page.evaluate(() => {
    const describe = (choice) => {
      const input = choice.querySelector('input[type="radio"]');
      const text = choice.querySelector("span");
      const choiceRect = choice.getBoundingClientRect();
      const inputRect = input.getBoundingClientRect();
      const textRect = text.getBoundingClientRect();
      const textStyle = getComputedStyle(text);
      return {
        value: input.value,
        choice: { width: choiceRect.width, height: choiceRect.height },
        input: { width: inputRect.width, height: inputRect.height },
        text: { width: textRect.width, height: textRect.height },
        writingMode: textStyle.writingMode,
        whiteSpace: textStyle.whiteSpace,
        wordBreak: textStyle.wordBreak,
        overflowWrap: textStyle.overflowWrap,
      };
    };
    return {
      contractTypes: [...document.querySelectorAll(".contract-type-choices .contract-type-choice")].map(describe),
      signers: [...document.querySelectorAll(".contract-signer-choices .choice")].map(describe),
      overflow: document.documentElement.scrollWidth - document.documentElement.clientWidth,
    };
  });
}

test("iPhone-width contract type and signer radios keep readable horizontal labels", async () => {
  const context = await browser.newContext({
    viewport: { width: 390, height: 844 },
    userAgent:
      "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 Version/18.0 Mobile/15E148 Safari/604.1",
  });
  const page = await context.newPage();
  await renderContractForm(page);
  const evidence = await optionEvidence(page);

  assert.equal(evidence.contractTypes.length, 3);
  assert.equal(evidence.signers.length, 2);
  for (const option of [...evidence.contractTypes, ...evidence.signers]) {
    assert.ok(option.input.width >= 16 && option.input.width <= 20, `${option.value}: radio width`);
    assert.ok(option.text.width >= 200, `${option.value}: readable text width ${option.text.width}`);
    assert.equal(option.writingMode, "horizontal-tb", `${option.value}: horizontal writing mode`);
    assert.equal(option.whiteSpace, "normal", `${option.value}: natural wrapping`);
    assert.equal(option.wordBreak, "normal", `${option.value}: no forced Chinese character breaks`);
    assert.ok(option.choice.height < 110, `${option.value}: no empty vertical card space`);
  }
  assert.ok(evidence.contractTypes[0].choice.height > evidence.signers[0].choice.height);
  assert.ok(evidence.overflow <= 0, `horizontal overflow ${evidence.overflow}`);

  await page.locator('.contract-type-choice:has(input[value="TransferApplicationContract"])').click();
  assert.equal(
    await page.locator('input[name="contract_type"]:checked').getAttribute("value"),
    "TransferApplicationContract"
  );
  await page.locator('.contract-signer-choices .choice:has(input[value="father"])').click();
  assert.equal(
    await page.locator('input[name="parent_signer_source"]:checked').getAttribute("value"),
    "father"
  );

  const evidenceDirectory = path.join(
    ROOT,
    "SAFEBOX",
    "contract-creation-mobile-radio-layout-20260823",
    "evidence"
  );
  fs.mkdirSync(evidenceDirectory, { recursive: true });
  await page.screenshot({
    path: path.join(evidenceDirectory, "iphone-contract-radio-options.png"),
    fullPage: true,
  });
  await context.close();
});

test("Desktop contract radio layout remains compact and selectable", async () => {
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  const page = await context.newPage();
  await renderContractForm(page);
  const evidence = await optionEvidence(page);

  for (const option of [...evidence.contractTypes, ...evidence.signers]) {
    assert.ok(option.input.width >= 16 && option.input.width <= 20, `${option.value}: desktop radio width`);
    assert.ok(option.text.width > 0, `${option.value}: desktop label visible`);
    assert.equal(option.writingMode, "horizontal-tb");
  }
  assert.ok(evidence.overflow <= 0, `desktop horizontal overflow ${evidence.overflow}`);
  await page.locator('.contract-type-choice:has(input[value="LocalStudentApplicationContract"])').click();
  assert.equal(
    await page.locator('input[name="contract_type"]:checked').getAttribute("value"),
    "LocalStudentApplicationContract"
  );
  await context.close();
});
