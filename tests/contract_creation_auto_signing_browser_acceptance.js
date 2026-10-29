#!/usr/bin/env node

const assert = require("node:assert/strict");
const { chromium } = require("playwright");

const baseUrl = process.argv[2] || "http://127.0.0.1:8898/";
const exactLink = "https://signing.example.test/contract/authoritative-73";
const initialMessage = "✓ 新合同已生成\n✓ 新签署链接已生成";
const successMessage = `${initialMessage}\n✓ 链接已复制`;
const viewports = {
  desktop: { width: 1440, height: 900 },
  mobile: { width: 390, height: 844 },
};

const queryUrl = () => {
  const url = new URL(baseUrl);
  url.searchParams.set("toast_title", "合同创建完成");
  url.searchParams.set("toast_kind", "success");
  url.searchParams.set("toast_message", initialMessage);
  url.searchParams.set("toast_copy", exactLink);
  url.searchParams.set("toast_copy_success", successMessage);
  return url.toString();
};

async function exercise(viewportName, failFirstClick) {
  const browser = await chromium.launch({
    headless: true,
    executablePath: "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
  });
  try {
    const context = await browser.newContext({ viewport: viewports[viewportName] });
    await context.addInitScript(({ failFirst }) => {
      window.__clipboardWrites = [];
      window.__clipboardValue = "UNCHANGED_SENTINEL";
      window.__legacyExecCommandCalls = 0;
      Object.defineProperty(navigator, "clipboard", {
        configurable: true,
        value: {
          writeText: async (value) => {
            window.__clipboardWrites.push(String(value));
            if (failFirst && window.__clipboardWrites.length === 1) {
              throw new DOMException("Clipboard permission denied", "NotAllowedError");
            }
            window.__clipboardValue = String(value);
          },
        },
      });
      document.execCommand = () => {
        window.__legacyExecCommandCalls += 1;
        return true;
      };
    }, { failFirst: failFirstClick });

    const page = await context.newPage();
    await page.goto(queryUrl(), { waitUntil: "networkidle" });
    const toast = page.locator("[data-crm-toast]");
    await toast.waitFor({ state: "visible" });
    await page.waitForTimeout(250);

    assert.equal(await toast.count(), 1, `${viewportName}: one combined Toast`);
    const initialText = (await toast.innerText()).replaceAll("\r", "");
    for (const line of initialMessage.split("\n")) assert.ok(initialText.includes(line));
    assert.ok(!initialText.includes("链接已复制"), `${viewportName}: page load must not claim copy success`);

    const copyButton = toast.locator("[data-crm-toast-copy]");
    assert.equal(await copyButton.innerText(), "复制链接");
    assert.ok(await copyButton.evaluate((button) => button.classList.contains("primary")), `${viewportName}: copy is primary action`);
    assert.deepEqual(
      await page.evaluate(() => ({
        writes: window.__clipboardWrites,
        value: window.__clipboardValue,
        legacyCalls: window.__legacyExecCommandCalls,
      })),
      { writes: [], value: "UNCHANGED_SENTINEL", legacyCalls: 0 },
      `${viewportName}: redirect page load performs zero Clipboard writes`,
    );

    await copyButton.click();
    if (failFirstClick) {
      assert.equal(await copyButton.innerText(), "复制失败，请重试");
      const failedText = (await toast.innerText()).replaceAll("\r", "");
      assert.ok(!failedText.includes("链接已复制"), `${viewportName}: failed gesture stays truthful`);
      assert.equal(await page.evaluate(() => window.__clipboardValue), "UNCHANGED_SENTINEL");
      assert.equal(await page.evaluate(() => window.__legacyExecCommandCalls), 0,
        `${viewportName}: execCommand=true is never success evidence`);
      await copyButton.click();
    }

    await page.waitForFunction(() => document.querySelector("[data-crm-toast-copy]")?.textContent === "已复制");
    assert.equal(await page.evaluate(() => window.__clipboardValue), exactLink,
      `${viewportName}: explicit gesture writes exact Signing Link`);
    assert.equal(await page.evaluate(() => window.__legacyExecCommandCalls), 0);
    const successText = (await toast.innerText()).replaceAll("\r", "");
    for (const line of successMessage.split("\n")) assert.ok(successText.includes(line));

    const box = await toast.boundingBox();
    assert.ok(box);
    assert.ok(box.x >= 0 && box.x + box.width <= viewports[viewportName].width + 1,
      `${viewportName}: Toast horizontal overflow`);
    assert.ok(box.y >= 0 && box.y + box.height <= viewports[viewportName].height + 1,
      `${viewportName}: Toast vertical overflow`);

    const submitCount = await page.evaluate(() => {
      window.__contractSubmitCount = 0;
      const form = document.querySelector("[data-contract-generation-form]");
      const button = form.querySelector('button[type="submit"]');
      form.addEventListener("submit", (event) => {
        event.preventDefault();
        window.__contractSubmitCount += 1;
      });
      button.click();
      button.click();
      return window.__contractSubmitCount;
    });
    assert.equal(submitCount, 1, `${viewportName}: double click submits once`);
    return { viewport: viewportName, fail_first_click: failFirstClick, result: "pass" };
  } finally {
    await browser.close();
  }
}

(async () => {
  const evidence = [];
  for (const viewportName of Object.keys(viewports)) {
    evidence.push(await exercise(viewportName, false));
    evidence.push(await exercise(viewportName, true));
  }
  process.stdout.write(`${JSON.stringify({ ok: true, evidence }, null, 2)}\n`);
})().catch((error) => {
  console.error(error.stack || error);
  process.exitCode = 1;
});
