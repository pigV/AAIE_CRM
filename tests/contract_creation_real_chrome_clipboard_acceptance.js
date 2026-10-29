#!/usr/bin/env node

const assert = require("node:assert/strict");
const { execFileSync } = require("node:child_process");
const { chromium } = require("playwright");
const {
  requireHostClipboardAuthority,
  waitForExactHostClipboard,
} = require("./contract_clipboard_host_reader");

const baseUrl = process.argv[2] || "http://127.0.0.1:8898/";
const exactLink = "https://signing.example.test/contract/real-chrome-91";
const initialMessage = "✓ 新合同已生成\n✓ 新签署链接已生成";
const successMessage = `${initialMessage}\n✓ 链接已复制`;
const viewports = {
  desktop: { width: 1440, height: 900 },
  mobile: { width: 390, height: 844 },
};

let clipboardAuthority;
try {
  clipboardAuthority = requireHostClipboardAuthority();
} catch (error) {
  console.error(error.message);
  process.exit(2);
}

if (process.argv[2] === "--authority-preflight") {
  process.stdout.write(`${JSON.stringify({ ok: true, clipboardAuthority })}\n`);
  process.exit(0);
}

const readHostSystemClipboard = () => execFileSync("/usr/bin/pbpaste", { encoding: "utf8" });
const writeSystemClipboard = (value) => {
  execFileSync("/usr/bin/pbcopy", { input: String(value), encoding: "utf8" });
};

function redirectUrl() {
  const url = new URL(baseUrl);
  url.searchParams.set("toast_title", "合同创建完成");
  url.searchParams.set("toast_kind", "success");
  url.searchParams.set("toast_message", initialMessage);
  url.searchParams.set("toast_copy", exactLink);
  url.searchParams.set("toast_copy_success", successMessage);
  return url.toString();
}

(async () => {
  const originalClipboard = readHostSystemClipboard();
  const browser = await chromium.launch({
    headless: false,
    executablePath: "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
  });
  const evidence = [];
  try {
    for (const [viewportName, viewport] of Object.entries(viewports)) {
      const sentinel = `CRM_TEST_MANUAL_COPY_SENTINEL_${viewportName}`;
      writeSystemClipboard(sentinel);
      const context = await browser.newContext({ viewport });
      await context.addInitScript(() => {
        window.__legacyExecCommandCalls = 0;
        document.execCommand = () => {
          window.__legacyExecCommandCalls += 1;
          return true;
        };
      });
      const page = await context.newPage();
      await page.goto(redirectUrl(), { waitUntil: "networkidle" });
      const toast = page.locator("[data-crm-toast]");
      await toast.waitFor({ state: "visible" });
      await page.waitForTimeout(1000);

      const beforeClickText = (await toast.innerText()).replaceAll("\r", "");
      for (const line of initialMessage.split("\n")) assert.ok(beforeClickText.includes(line));
      assert.ok(!beforeClickText.includes("链接已复制"), `${viewportName}: redirect must not claim copy success`);
      const copyButton = toast.locator("[data-crm-toast-copy]");
      assert.equal(await copyButton.innerText(), "复制链接");
      assert.ok(await copyButton.evaluate((button) => button.classList.contains("primary")));
      assert.equal(readHostSystemClipboard(), sentinel, `${viewportName}: redirect leaves macOS Clipboard unchanged`);
      assert.equal(await page.evaluate(() => window.__legacyExecCommandCalls), 0);

      await copyButton.click();
      await page.waitForFunction(() => document.querySelector("[data-crm-toast-copy]")?.textContent === "已复制");
      const hostClipboardEvidence = await waitForExactHostClipboard({
        expected: exactLink,
        read: readHostSystemClipboard,
      });
      assert.equal(await page.evaluate(() => window.__legacyExecCommandCalls), 0);
      const afterClickText = (await toast.innerText()).replaceAll("\r", "");
      for (const line of successMessage.split("\n")) assert.ok(afterClickText.includes(line));

      evidence.push({
        browser: "Google Chrome",
        viewport: viewportName,
        clipboard_reader_authority: clipboardAuthority,
        redirect_clipboard: "unchanged",
        explicit_click_os_clipboard: "exact-link",
        host_clipboard_attempts: hostClipboardEvidence.attempts,
        host_clipboard_observed_at: hostClipboardEvidence.observedAt,
        exec_command_calls: 0,
      });
      await context.close();
    }
  } finally {
    await browser.close();
    writeSystemClipboard(originalClipboard);
  }
  process.stdout.write(`${JSON.stringify({ ok: true, evidence }, null, 2)}\n`);
})().catch((error) => {
  console.error(error.stack || error);
  process.exitCode = 1;
});
