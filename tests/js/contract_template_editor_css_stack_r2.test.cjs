const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { spawn } = require("node:child_process");
const { before, after, test } = require("node:test");

const { chromium } = require(
  process.env.AAIE_PLAYWRIGHT_MODULE ||
    "/Users/faqiang/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright"
);

const ROOT = path.resolve(__dirname, "../..");
const PYTHON =
  process.env.AAIE_PYTHON ||
  "/Users/faqiang/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3";
const CHROME =
  process.env.AAIE_CHROME ||
  "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
const RUNTIME =
  process.env.AAIE_CSS_STACK_RUNTIME ||
  path.join(
    path.dirname(path.dirname(ROOT)),
    "SAFEBOX",
    "contract-template-editor-ui-fix-css-stack-r2-20260821",
    "runtime"
  );

let server;
let baseUrl;
let browser;

function waitForReady(child) {
  return new Promise((resolve, reject) => {
    let stderr = "";
    const timeout = setTimeout(() => reject(new Error(`harness timeout: ${stderr}`)), 120000);
    child.stderr.on("data", (chunk) => {
      stderr += chunk.toString();
    });
    child.stdout.on("data", (chunk) => {
      const text = chunk.toString();
      const match = text.match(/CSS_STACK_HARNESS_READY (http:\/\/127\.0\.0\.1:\d+\/)/);
      if (match) {
        clearTimeout(timeout);
        resolve(match[1]);
      }
    });
    child.on("exit", (code) => {
      clearTimeout(timeout);
      reject(new Error(`harness exited ${code}: ${stderr}`));
    });
  });
}

before(async () => {
  server = spawn(PYTHON, ["tests/contract_ui_css_stack_r2_browser_harness.py", "--port", "0"], {
    cwd: ROOT,
    env: { ...process.env, AAIE_CSS_STACK_RUNTIME: RUNTIME },
    stdio: ["ignore", "pipe", "pipe"],
  });
  baseUrl = await waitForReady(server);
  browser = await chromium.launch({ executablePath: CHROME, headless: true });
});

after(async () => {
  if (browser) await browser.close();
  if (server && server.exitCode === null) server.kill("SIGTERM");
});

async function browserState(page, contractType) {
  await page.locator(`[data-contract-type-radio][value="${contractType}"]`).check();
  await page.waitForTimeout(50);
  return page.evaluate(() => {
    const form = document.querySelector("form.contract-form");
    const services = [...form.querySelectorAll('[name="service_scope"]')].map((input) => {
      const choice = input.closest(".choice");
      const rect = choice.getBoundingClientRect();
      input.focus();
      const focusAccepted = document.activeElement === input;
      if (focusAccepted) input.blur();
      return {
        id: input.value,
        allowed: !input.disabled,
        hidden: choice.hidden,
        display: getComputedStyle(choice).display,
        width: rect.width,
        height: rect.height,
        disabled: input.disabled,
        checked: input.checked,
        tabIndex: input.tabIndex,
        focusAccepted,
      };
    });
    const successful = new FormData(form).getAll("service_scope");
    return {
      services,
      successful,
      overflow: document.documentElement.scrollWidth - document.documentElement.clientWidth,
      contractTypeChoiceDisplay: getComputedStyle(
        form.querySelector(".contract-type-choice")
      ).display,
      ordinaryHiddenInputDisplay: getComputedStyle(
        form.querySelector('input[type="hidden"]')
      ).display,
    };
  });
}

async function cascadeEvidence(page) {
  return page.evaluate(() => {
    const element = [...document.querySelectorAll('[name="service_scope"]')]
      .find((input) => input.disabled)
      .closest(".choice");
    const matches = [];
    let sourceOrder = 0;
    const visit = (rules, href, media = "") => {
      for (const rule of rules) {
        if (rule.type === CSSRule.STYLE_RULE) {
          if (rule.selectorText && rule.style?.display) {
            for (const selector of rule.selectorText.split(",")) {
              const clean = selector.trim();
              try {
                if (element.matches(clean)) {
                  matches.push({
                    href,
                    selector: clean,
                    display: rule.style.display,
                    priority: rule.style.getPropertyPriority("display"),
                    media,
                    sourceOrder,
                  });
                }
              } catch (_error) {}
            }
          }
        } else if (rule.cssRules) {
          const active = !rule.conditionText || matchMedia(rule.conditionText).matches;
          if (active) visit(rule.cssRules, href, rule.conditionText || media);
        }
        sourceOrder += 1;
      }
    };
    for (const sheet of document.styleSheets) {
      visit(sheet.cssRules, new URL(sheet.href).pathname);
    }
    return {
      hidden: element.hidden,
      computedDisplay: getComputedStyle(element).display,
      rect: element.getBoundingClientRect().toJSON(),
      matches,
    };
  });
}

for (const viewport of [
  { name: "desktop", width: 1440, height: 900 },
  { name: "mobile", width: 390, height: 844 },
]) {
  test(`Production stylesheet stack hides contract services on ${viewport.name}`, { timeout: 120000 }, async () => {
    const context = await browser.newContext({ viewport });
    const page = await context.newPage();
    const consoleMessages = [];
    page.on("console", (message) => {
      if (["warning", "error"].includes(message.type())) {
        consoleMessages.push(`${message.type()}: ${message.text()}`);
      }
    });
    await page.goto(baseUrl, { waitUntil: "networkidle" });

    const stylesheets = await page.evaluate(() =>
      [...document.styleSheets].map((sheet) => new URL(sheet.href).pathname)
    );
    assert.deepEqual(stylesheets, [
      "/assets/style.css",
      "/assets/ui-premium-v2.css",
      "/assets/student-followup-notes-v1.css",
      "/assets/student-detail-summary-cards-v1.css",
      "/assets/student-lifecycle-phase6.css",
    ]);

    for (const type of ["TransferApplicationContract", "LocalStudentApplicationContract"]) {
      const state = await browserState(page, type);
      if (type === "TransferApplicationContract") {
        const evidence = await cascadeEvidence(page);
        console.log(`CSS_CASCADE_${viewport.name.toUpperCase()}=${JSON.stringify(evidence)}`);
      }
      const rejected = state.services.filter((item) => !item.allowed);
      assert.ok(rejected.length > 0);
      rejected.forEach((item) => {
        assert.equal(item.hidden, true, `${type}/${item.id}: hidden attribute`);
        assert.equal(item.display, "none", `${type}/${item.id}: computed display`);
        assert.equal(item.width, 0, `${type}/${item.id}: width`);
        assert.equal(item.height, 0, `${type}/${item.id}: height`);
        assert.equal(item.disabled, true, `${type}/${item.id}: disabled`);
        assert.equal(item.checked, false, `${type}/${item.id}: unchecked`);
        assert.equal(item.focusAccepted, false, `${type}/${item.id}: focus rejected`);
        assert.ok(!state.successful.includes(item.id), `${type}/${item.id}: successful control`);
      });
      assert.ok(state.contractTypeChoiceDisplay !== "none");
      assert.equal(state.ordinaryHiddenInputDisplay, "none");
      assert.ok(state.overflow <= 0, `${type}: horizontal overflow ${state.overflow}`);
    }

    const international = await browserState(page, "NewInternationalStudentContract");
    const visible = international.services.filter((item) => item.allowed);
    assert.equal(visible.length, 12);
    visible.forEach((item) => {
      assert.equal(item.hidden, false, item.id);
      assert.notEqual(item.display, "none", item.id);
      assert.ok(item.width > 0 && item.height > 0, item.id);
    });
    for (const id of [
      "subclass_590_guardian_visa_lodgement",
      "subclass_590_guardian_visa_consulting",
    ]) {
      const item = international.services.find((service) => service.id === id);
      assert.ok(item && item.allowed && !item.checked, `${id}: visible and default off`);
    }

    await page.locator('[data-contract-type-radio][value="TransferApplicationContract"]').check();
    const evidence = await cascadeEvidence(page);
    assert.equal(evidence.hidden, true);
    assert.equal(evidence.computedDisplay, "none");
    assert.ok(evidence.matches.some((rule) => rule.href.endsWith("/style.css") && rule.display === "none"));
    assert.ok(evidence.matches.some((rule) => rule.href.endsWith("/ui-premium-v2.css") && rule.display === "flex"));

    const restored = await page.evaluate(() => {
      const input = [...document.querySelectorAll('[name="service_scope"]')].find((item) => item.disabled);
      const choice = input.closest(".choice");
      choice.hidden = false;
      const rect = choice.getBoundingClientRect();
      const result = { display: getComputedStyle(choice).display, width: rect.width, height: rect.height };
      choice.hidden = true;
      return result;
    });
    assert.notEqual(restored.display, "none");
    assert.ok(restored.width > 0 && restored.height > 0);
    assert.deepEqual(consoleMessages, []);
    const evidenceDirectory = path.join(path.dirname(RUNTIME), "evidence");
    fs.mkdirSync(evidenceDirectory, { recursive: true });
    await page.screenshot({
      path: path.join(evidenceDirectory, `css-stack-${viewport.name}.png`),
      fullPage: true,
    });
    await context.close();
  });
}
