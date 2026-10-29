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
  "content-centre-final-consistency-safebox-c",
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
      const match = text.match(/CONTENT_CENTRE_FINAL_CONSISTENCY_READY (http:\/\/127\.0\.0\.1:\d+\/)/);
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

const pages = [
  {
    name: "dashboard",
    path: "dashboard",
    root: ".content-centre-page",
    areas: [
      ".content-centre-tabs",
      ".content-centre-header",
      ".content-centre-stats",
      ".content-centre-overview-grid",
      ".content-centre-toolbar",
      ".content-centre-results",
    ],
  },
  {
    name: "create",
    path: "create",
    root: ".content-create-page",
    areas: [".content-worktab-tabs", ".content-worktab-header", ".content-worktab-form"],
  },
  {
    name: "extract",
    path: "extract",
    root: ".content-extract-page",
    areas: [
      ".content-extract-sync-tabs",
      ".content-extract-header",
      ".content-extract-source-options",
      ".content-extract-detail-panel",
      ".content-extract-job-table",
    ],
  },
  {
    name: "sync",
    path: "sync",
    root: ".content-sync-page",
    areas: [".content-extract-sync-tabs", ".google-drive-update-page", ".drive-service-control", ".drive-control-actions"],
  },
  {
    name: "tracking",
    path: "tracking",
    root: ".content-tracking-page",
    areas: [".content-worktab-tabs", ".content-worktab-header", ".content-tracking-form", ".content-tracking-table"],
  },
  {
    name: "analysis",
    path: "analysis",
    root: ".content-analysis-page",
    areas: [".content-worktab-tabs", ".content-worktab-header", ".content-analysis-kpi-grid", ".content-analysis-table"],
  },
  {
    name: "topic",
    path: "topic",
    root: ".content-topic-detail-page",
    areas: [
      ".content-detail-workspace__header",
      ".content-detail-workspace__layout",
      ".content-detail-workspace__context",
      ".content-detail-workspace__main",
      ".content-detail-workspace__side",
    ],
  },
  {
    name: "staging",
    path: "staging",
    root: ".content-staging-detail-page",
    areas: [
      ".content-detail-workspace__header",
      ".content-detail-workspace__layout",
      ".content-detail-workspace__context",
      ".content-detail-workspace__main",
      ".content-detail-workspace__side",
    ],
  },
  {
    name: "duplicate",
    path: "duplicate",
    root: ".content-duplicate-detail-page",
    areas: [
      ".content-detail-workspace__header",
      ".content-detail-workspace__layout",
      ".content-detail-workspace__context",
      ".content-detail-workspace__main",
      ".content-detail-workspace__side",
    ],
  },
  {
    name: "script",
    path: "script",
    root: ".content-script-detail-page",
    areas: [
      ".content-detail-workspace__header",
      ".content-detail-workspace__layout",
      ".content-detail-workspace__context",
      ".content-detail-workspace__main",
      ".content-detail-workspace__side",
    ],
  },
];

async function measure(page, spec, theme) {
  return page.evaluate(({ pageSpec, activeTheme }) => {
    const root = document.querySelector(pageSpec.root);
    const main = document.querySelector(".main");
    const luminance = (value) => {
      const match = String(value || "").match(/rgba?\((\d+),\s*(\d+),\s*(\d+)/);
      if (!match) return 0;
      return Number(match[1]) * 0.2126 + Number(match[2]) * 0.7152 + Number(match[3]) * 0.0722;
    };
    const areas = pageSpec.areas.map((selector) => {
      const el = document.querySelector(selector);
      const rect = el ? el.getBoundingClientRect() : null;
      return { selector, found: Boolean(el), width: rect ? rect.width : 0, height: rect ? rect.height : 0 };
    });
    const lightPanelsInDark = activeTheme === "dark" && root
      ? Array.from(
          root.querySelectorAll(
            ".panel, .metric, .content-script-card, .drive-service-grid > div, .drive-summary-grid > div, .drive-item-grid > div, .drive-log-card, .drive-item-card"
          )
        )
          .filter((el) => {
            const rect = el.getBoundingClientRect();
            const background = getComputedStyle(el).backgroundColor;
            return rect.width > 0 && rect.height > 0 && luminance(background) > 150;
          })
          .map((el) => ({
            className: el.className,
            background: getComputedStyle(el).backgroundColor,
          }))
      : [];
    const clippedControls = Array.from(
      document.querySelectorAll(`${pageSpec.root} button, ${pageSpec.root} .button-link, ${pageSpec.root} .status`)
    )
      .filter((el) => {
        const rect = el.getBoundingClientRect();
        return rect.width > 0 && rect.height > 0 && el.scrollWidth > el.clientWidth + 4;
      })
      .map((el) => ({
        text: (el.textContent || "").trim().slice(0, 60),
        className: el.className,
        scrollWidth: el.scrollWidth,
        clientWidth: el.clientWidth,
      }));
    const fixedInsideRoot = root
      ? Array.from(root.querySelectorAll("*")).filter((el) => {
          const position = getComputedStyle(el).position;
          return position === "fixed" || position === "sticky";
        }).length
      : 0;
    const controls = root
      ? root.querySelectorAll("a[href], button, input, select, textarea").length
      : 0;
    return {
      documentScrollWidth: document.documentElement.scrollWidth,
      viewportWidth: window.innerWidth,
      rootScrollWidth: root ? root.scrollWidth : 0,
      rootClientWidth: root ? root.clientWidth : 0,
      mainScrollWidth: main ? main.scrollWidth : 0,
      mainClientWidth: main ? main.clientWidth : 0,
      areas,
      clippedControls,
      fixedInsideRoot,
      lightPanelsInDark,
      controls,
      background: getComputedStyle(document.documentElement).getPropertyValue("--background").trim(),
      card: getComputedStyle(document.documentElement).getPropertyValue("--card").trim(),
    };
  }, { pageSpec: spec, activeTheme: theme });
}

before(async () => {
  fs.mkdirSync(EVIDENCE_DIR, { recursive: true });
  server = spawn(PYTHON, [
    "tests/content_centre_final_consistency_browser_harness.py",
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
    for (const spec of pages) {
      test(`content centre final consistency ${spec.name} ${theme} ${viewport.name}`, async () => {
        const page = await browser.newPage({ viewport });
        const externalRequests = [];
        page.on("request", (request) => {
          const url = request.url();
          if (!url.startsWith(baseUrl) && !url.startsWith("data:") && !url.startsWith("about:")) {
            externalRequests.push(url);
          }
        });
        await page.goto(new URL(`${theme}/${spec.path}`, baseUrl).toString(), { waitUntil: "networkidle" });
        if (spec.name === "sync") {
          await page.locator("[data-drive-log-toggle]").click();
        }
        const snapshotPath = path.join(
          EVIDENCE_DIR,
          `content-centre-final-consistency-${spec.name}-${theme}-${viewport.name}.png`
        );
        await page.screenshot({ path: snapshotPath, fullPage: true });
        const state = await measure(page, spec, theme);
        assert.ok(fs.existsSync(snapshotPath), `missing screenshot ${snapshotPath}`);
        for (const area of state.areas) {
          assert.equal(area.found, true, `${area.selector} missing`);
          assert.ok(area.width > 0 && area.height > 0, `${area.selector} has empty layout`);
        }
        assert.ok(state.controls > 0, `${spec.root} has no interactive or linked controls`);
        assert.ok(
          state.rootScrollWidth <= state.rootClientWidth + 4,
          `root overflow: ${JSON.stringify(state)}`
        );
        assert.ok(
          state.mainScrollWidth <= state.mainClientWidth + 4,
          `main overflow: ${JSON.stringify(state)}`
        );
        assert.equal(state.fixedInsideRoot, 0, `unexpected fixed/sticky element inside content root: ${JSON.stringify(state)}`);
        assert.deepEqual(state.lightPanelsInDark, [], `light panels in dark theme: ${JSON.stringify(state)}`);
        assert.deepEqual(state.clippedControls, [], `clipped control text: ${JSON.stringify(state)}`);
        assert.deepEqual(externalRequests, [], `external requests occurred: ${externalRequests.join(", ")}`);
        assert.notEqual(state.background, "");
        assert.notEqual(state.card, "");
        await page.close();
      });
    }
  }
}
