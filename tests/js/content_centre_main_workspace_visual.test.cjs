const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { spawn } = require("node:child_process");
const { after, before, test } = require("node:test");

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
const EVIDENCE_DIR = path.join(
  ROOT,
  "SAFEBOX",
  "content-centre-main-workspace-safebox-a",
  "evidence",
  "visual"
);
const RUNTIME_DIR = path.join(EVIDENCE_DIR, "runtime");

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
      const match = text.match(/CONTENT_CENTRE_MAIN_WORKSPACE_READY (http:\/\/127\.0\.0\.1:\d+\/)/);
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

async function measure(page) {
  return page.evaluate(() => {
    const root = document.querySelector(".content-centre-page");
    const main = document.querySelector(".main");
    const areas = [
      ".content-centre-tabs",
      ".content-centre-header",
      ".content-centre-stats",
      ".content-centre-overview-grid",
      ".content-centre-toolbar",
      ".content-centre-results",
    ].map((selector) => {
      const el = document.querySelector(selector);
      const rect = el ? el.getBoundingClientRect() : null;
      return { selector, found: Boolean(el), width: rect ? rect.width : 0, height: rect ? rect.height : 0 };
    });
    return {
      documentScrollWidth: document.documentElement.scrollWidth,
      viewportWidth: window.innerWidth,
      rootScrollWidth: root ? root.scrollWidth : 0,
      rootClientWidth: root ? root.clientWidth : 0,
      mainScrollWidth: main ? main.scrollWidth : 0,
      mainClientWidth: main ? main.clientWidth : 0,
      areas,
      background: getComputedStyle(document.documentElement).getPropertyValue("--background").trim(),
      card: getComputedStyle(document.documentElement).getPropertyValue("--card").trim(),
    };
  });
}

before(async () => {
  fs.mkdirSync(EVIDENCE_DIR, { recursive: true });
  server = spawn(PYTHON, [
    "tests/content_centre_main_workspace_browser_harness.py",
    "--port",
    "0",
    "--runtime-dir",
    RUNTIME_DIR,
  ], {
    cwd: ROOT,
    env: { ...process.env, AAIE_CRM_DB_PERF_LOG: "0" },
    stdio: ["ignore", "pipe", "pipe"],
  });
  baseUrl = await waitForReady(server);
  browser = await chromium.launch({ executablePath: CHROME, headless: true });
});

after(async () => {
  if (browser) await browser.close();
  if (server && server.exitCode === null) server.kill("SIGTERM");
});

for (const theme of ["light", "dark"]) {
  for (const viewport of [
    { name: "desktop-1280", width: 1280, height: 900 },
    { name: "mobile-390", width: 390, height: 980 },
  ]) {
    test(`content centre main workspace ${theme} ${viewport.name}`, async () => {
      const page = await browser.newPage({ viewport });
      await page.goto(new URL(theme, baseUrl).toString(), { waitUntil: "networkidle" });
      const snapshotPath = path.join(EVIDENCE_DIR, `content-centre-main-workspace-${theme}-${viewport.name}.png`);
      await page.screenshot({ path: snapshotPath, fullPage: true });
      const state = await measure(page);
      assert.ok(fs.existsSync(snapshotPath), `missing screenshot ${snapshotPath}`);
      for (const area of state.areas) {
        assert.equal(area.found, true, `${area.selector} missing`);
        assert.ok(area.width > 0 && area.height > 0, `${area.selector} has empty layout`);
      }
      assert.ok(
        state.rootScrollWidth <= state.rootClientWidth + 4,
        `content page overflow: ${JSON.stringify(state)}`
      );
      assert.ok(
        state.mainScrollWidth <= state.mainClientWidth + 4,
        `main overflow: ${JSON.stringify(state)}`
      );
      assert.notEqual(state.background, "");
      assert.notEqual(state.card, "");
      await page.close();
    });
  }
}
