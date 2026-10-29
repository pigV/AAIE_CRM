#!/usr/bin/env node

const assert = require("node:assert/strict");
const { chromium } = require("playwright");

const baseUrl = process.argv[2] || "http://127.0.0.1:8912";
const viewports = {
  desktop: { width: 1440, height: 900 },
  mobile: { width: 390, height: 844 },
};
const contracts = {
  ordinary: {
    token: "ordinary-final-signed-20260905",
    nickname: "普通合同验收家长",
    openid: "startup-readonly-ordinary-openid",
  },
  transfer: {
    token: "transfer-final-signed-20260905",
    nickname: "浏览器验收家长",
    openid: "startup-readonly-transfer-openid",
  },
};

async function exercise(browser, viewportName, viewport, contractName, fixture) {
  const context = await browser.newContext({ viewport });
  try {
    const page = await context.newPage();
    const portalUrl = `${baseUrl}/contract-sign/${fixture.token}`;
    const response = await page.goto(portalUrl, { waitUntil: "networkidle" });
    assert.equal(response.status(), 200, `${contractName}/${viewportName}: portal HTTP 200`);

    if (await page.getByRole("heading", { name: "请先完成微信身份验证" }).count()) {
      await page.getByLabel("微信昵称").fill(fixture.nickname);
      await page.getByLabel(/OpenID/).fill(fixture.openid);
      await page.getByRole("button", { name: "微信验证并查看合同" }).click();
      await page.waitForLoadState("networkidle");
    }

    await page.getByRole("heading", { name: "合同已完成签署" }).waitFor();
    assert.equal(await page.getByRole("link", { name: "查看已签署合同" }).count(), 1);
    assert.equal(await page.getByRole("link", { name: "下载已签署合同" }).count(), 1);
    assert.equal(await page.locator('img[alt^="合同预览第 "]').count(), 3);
    const layout = await page.evaluate(() => ({
      innerWidth: window.innerWidth,
      scrollWidth: document.documentElement.scrollWidth,
    }));
    assert.ok(
      layout.scrollWidth <= layout.innerWidth + 1,
      `${contractName}/${viewportName}: unexpected horizontal overflow`,
    );

    const finalViewUrl = `${baseUrl}/contract-sign/${fixture.token}/final/view`;
    const finalResponse = await context.request.get(finalViewUrl);
    assert.equal(finalResponse.status(), 200, `${contractName}/${viewportName}: final view HTTP 200`);
    assert.match(
      finalResponse.headers()["content-type"] || "",
      /^application\/pdf/i,
      `${contractName}/${viewportName}: final view is a PDF`,
    );
    assert.ok((await finalResponse.body()).length > 1000);

    return {
      contract: contractName,
      viewport: viewportName,
      width: viewport.width,
      height: viewport.height,
      portal_http: 200,
      final_view_http: 200,
      final_pages: 3,
      horizontal_overflow: false,
      result: "PASS",
    };
  } finally {
    await context.close();
  }
}

(async () => {
  const browser = await chromium.launch({
    headless: true,
    executablePath: "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
  });
  const evidence = [];
  try {
    for (const [viewportName, viewport] of Object.entries(viewports)) {
      for (const [contractName, fixture] of Object.entries(contracts)) {
        evidence.push(await exercise(
          browser,
          viewportName,
          viewport,
          contractName,
          fixture,
        ));
      }
    }
  } finally {
    await browser.close();
  }
  process.stdout.write(`${JSON.stringify({ ok: true, evidence }, null, 2)}\n`);
})().catch((error) => {
  console.error(error.stack || error);
  process.exitCode = 1;
});
