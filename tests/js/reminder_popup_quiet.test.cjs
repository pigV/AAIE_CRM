const assert = require("node:assert/strict");
const fs = require("node:fs");
const { test } = require("node:test");
const { chromium } = require("playwright");

test("closing a reminder silences both tabs and a reload until two hours elapse", async () => {
  const browser = await chromium.launch({
    executablePath: process.env.CRM_TEST_CHROME,
  });
  try {
    const context = await browser.newContext();
    const toast = '<aside data-reminder-toast><button data-reminder-toast-close>×</button></aside>';
    const markup = '<span hidden data-reminder-sync-enabled="aaie_reminder_quiet_7"></span>';
    const js = fs.readFileSync("assets/app.js", "utf8");
    const reminderCode = js.slice(
      js.indexOf("  const reminderQuietCookie ="),
      js.indexOf('  document.addEventListener("submit", async (event)', js.indexOf("  const reminderQuietCookie =")),
    );
    const script = 'const q = (selector, root = document) => root.querySelector(selector);\n' + reminderCode;
    let requests = 0;
    await context.route("http://reminder.test/**", async (route) => {
      if (new URL(route.request().url()).pathname === "/api/reminders/actionable") {
        requests += 1;
        return route.fulfill({ json: { ok: true, toast_html: toast } });
      }
      return route.fulfill({
        contentType: "text/html",
        body: '<!doctype html><body>' + markup + toast + '<script>' + script + '</script>',
      });
    });
    const first = await context.newPage();
    const second = await context.newPage();
    await first.clock.install();
    await second.clock.install();
    await first.goto("http://reminder.test/");
    await second.goto("http://reminder.test/");
    await second.locator("[data-reminder-toast]").waitFor({ state: "visible" });
    await first.locator("[data-reminder-toast-close]").click();
    await second.locator("[data-reminder-toast]").waitFor({ state: "detached" });
    const cookie = (await context.cookies()).find(({ name }) => name === "aaie_reminder_quiet_7");
    assert.ok(cookie);
    const remaining = Number(cookie.value) - await first.evaluate(() => Date.now());
    assert.ok(remaining <= 7_200_000 && remaining > 7_195_000);

    await first.reload();
    assert.equal(await first.locator("[data-reminder-toast]").count(), 0);
    await first.evaluate(() => window.dispatchEvent(new Event("focus")));
    assert.equal(await first.locator("[data-reminder-toast]").count(), 0);
    const requestsBeforeExpiry = requests;
    await first.clock.fastForward(7_201_000);
    await first.locator("[data-reminder-toast]").waitFor({ state: "visible" });
    assert.ok(requests > requestsBeforeExpiry);
  } finally {
    await browser.close();
  }
});
