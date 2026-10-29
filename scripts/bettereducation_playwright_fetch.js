#!/usr/bin/env node
const fs = require("fs");
const path = require("path");
const { chromium } = require("playwright");

const [payloadPath, outputPath, artifactDir] = process.argv.slice(2);

function compact(value) {
  return String(value || "").replace(/\s+/g, " ").trim();
}

function detectAccessProblem(text, status) {
  const lower = String(text || "").toLowerCase();
  if (lower.includes("captcha") || lower.includes("verify you are human")) return "captcha_detected";
  if (status === 403 || lower.includes("403 forbidden") || lower.includes("access denied")) return "access_denied";
  if (status === 429 || lower.includes("too many requests") || lower.includes("rate limit")) return "rate_limited";
  return "";
}

function classifyWorkerError(error) {
  const message = error && error.stack ? error.stack : String(error || "");
  if (
    message.includes("MachPortRendezvous") ||
    message.includes("browserType.launch") ||
    message.toLowerCase().includes("permission denied")
  ) {
    return "browser_launch_failed";
  }
  return "page_structure_changed";
}

async function saveEvidence(page, artifactDir, prefix = "error") {
  fs.mkdirSync(artifactDir, { recursive: true });
  const htmlPath = path.join(artifactDir, "snapshot.html");
  const screenshotPath = path.join(artifactDir, `${prefix}.png`);
  try {
    fs.writeFileSync(htmlPath, await page.content(), "utf8");
  } catch (_) {}
  try {
    await page.screenshot({ path: screenshotPath, fullPage: true });
  } catch (_) {}
  return { html_snapshot_path: htmlPath, screenshot_path: screenshotPath };
}

async function firstVisible(page, selectors) {
  for (const selector of selectors) {
    const locator = page.locator(selector).first();
    try {
      if (await locator.isVisible({ timeout: 1500 })) return locator;
    } catch (_) {}
  }
  return null;
}

async function firstMatchingLocator(page, selectors) {
  for (const selector of selectors) {
    const locator = page.locator(selector).first();
    try {
      if (await locator.count()) return locator;
    } catch (_) {}
  }
  return null;
}

async function fillFirstMatchingInput(page, selectors, value) {
  for (const selector of selectors) {
    const locator = page.locator(selector).first();
    try {
      if (await locator.isVisible({ timeout: 1500 })) {
        await locator.fill(value);
        return locator;
      }
    } catch (_) {}
  }
  const fallback = await firstMatchingLocator(page, selectors);
  if (!fallback) return null;
  try {
    await fallback.evaluate((element, text) => {
      element.value = text;
      element.dispatchEvent(new Event("input", { bubbles: true }));
      element.dispatchEvent(new Event("change", { bubbles: true }));
    }, value);
    return fallback;
  } catch (_) {
    return null;
  }
}

function rowValue(cells, aliases) {
  for (const alias of aliases) {
    if (compact(alias) === "%") {
      const match = cells.find((cell) => compact(cell.header) === "%");
      if (match) return match.value;
    }
    const match = cells.find((cell) => cell.key.includes(alias));
    if (match) return match.value;
  }
  return "";
}

function stripPercent(value) {
  return compact(value).replace(/%$/, "").trim();
}

function rankingSystemForState(state) {
  const stateKey = String(state || "VIC").trim().toUpperCase();
  if (stateKey === "NSW") return "HSC";
  if (stateKey === "VIC") return "VCE";
  return "Year 12";
}

function resultMetricNameForState(state) {
  return `Better Education ${rankingSystemForState(state)} Compare/Trend Rank`;
}

function parseRowsFromTables(tables, payload, sourceUrl) {
  const records = [];
  const rankingSystem = rankingSystemForState(payload.state);
  const resultMetricName = resultMetricNameForState(payload.state);
  for (const table of tables) {
    if (!table.headers.length || !table.rows.length) continue;
    const headerText = table.headers.join(" ").toLowerCase();
    const looksRelevant =
      headerText.includes("year") ||
      headerText.includes("rank") ||
      headerText.includes("median") ||
      headerText.includes("40") ||
      headerText.includes("percentile");
    if (!looksRelevant) continue;
    for (const values of table.rows) {
      const cells = table.headers.map((header, index) => ({
        header: compact(header),
        key: compact(header).toLowerCase().replace(/[^a-z0-9]+/g, ""),
        value: compact(values[index] || ""),
      }));
      const dataYear = rowValue(cells, ["year", "datayear"]);
      if (!/^20\d{2}$/.test(dataYear)) continue;
      const yearNumber = Number(dataYear);
      if (yearNumber < Number(payload.year_start) || yearNumber > Number(payload.year_end)) continue;
      const betterEducationRank = rowValue(cells, ["bettereducationrank", "rank"]);
      const medianVceScore = rowValue(cells, ["medianvcescore", "medianscore", "median"]);
      const topScores40Plus = stripPercent(rowValue(cells, ["scoresof40", "studyscores40", "percentage", "%"]));
      const medianScorePercentile = rowValue(cells, ["bepercentile", "medianscorepercentile", "percentile"]);
      const unit34Cohort = rowValue(cells, ["year12students", "unit34cohort", "cohort", "students", "enrolments"]);
      records.push({
        crm_school_id: payload.school_id || null,
        crm_school_name: payload.crm_school_name || "",
        source_school_name: rowValue(cells, ["school"]) || payload.source_school_name || payload.crm_school_name || "",
        state: payload.state || "VIC",
        data_year: dataYear,
        bettereducation_rank: betterEducationRank,
        score: rowValue(cells, ["da", "distinguishedachievers"]),
        bettereducation_percentile: medianScorePercentile,
        median_vce_score: medianVceScore,
        percentage_study_scores_40_plus: topScores40Plus,
        result_metric_name: resultMetricName,
        result_metric_value: betterEducationRank,
        students: unit34Cohort,
        exams_or_entries: rowValue(cells, ["examssat", "exams", "entries"]),
        ranking_system: rankingSystem,
        source_url: sourceUrl,
        match_status: payload.school_id ? "Matched" : "Unmatched",
        confidence_score: payload.school_id ? 0.95 : 0,
        raw_row_json: {
          headers: table.headers,
          values,
        },
      });
    }
  }
  return records;
}

async function extractTables(page) {
  return page.evaluate(() => {
    return Array.from(document.querySelectorAll("table")).map((table) => {
      const rows = Array.from(table.querySelectorAll("tr")).map((tr) =>
        Array.from(tr.querySelectorAll("th,td")).map((cell) => cell.textContent.replace(/\s+/g, " ").trim())
      ).filter((row) => row.some(Boolean));
      if (!rows.length) return { headers: [], rows: [] };
      return { headers: rows[0], rows: rows.slice(1) };
    });
  });
}

async function main() {
  if (!payloadPath || !outputPath || !artifactDir) {
    throw new Error("Usage: bettereducation_playwright_fetch.js payload.json output.json artifact_dir");
  }
  fs.mkdirSync(artifactDir, { recursive: true });
  const payload = JSON.parse(fs.readFileSync(payloadPath, "utf8"));
  const sourceUrl = payload.source_url || process.env.BETTEREDUCATION_COMPARE_URL || "https://bettereducation.com.au/";
  let browser;
  try {
    browser = await chromium.launch({ headless: true, timeout: 30000 });
  } catch (error) {
    return {
      status: "failed",
      error_type: classifyWorkerError(error),
      error_message: error && error.stack ? error.stack : String(error),
    };
  }
  const page = await browser.newPage({
    viewport: { width: 1440, height: 1000 },
    userAgent: "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome Safari",
  });
  try {
    const response = await page.goto(sourceUrl, { waitUntil: "domcontentloaded", timeout: 45000 });
    await page.waitForLoadState("networkidle", { timeout: 20000 }).catch(() => {});
    const bodyText = await page.locator("body").innerText({ timeout: 5000 }).catch(() => "");
    const accessProblem = detectAccessProblem(bodyText, response ? response.status() : 0);
    if (accessProblem) {
      const evidence = await saveEvidence(page, artifactDir, accessProblem);
      return {
        status: "failed",
        error_type: accessProblem,
        error_message: `Better Education returned ${accessProblem}.`,
        ...evidence,
      };
    }

    const searchBox = await fillFirstMatchingInput(page, [
      "input[id*='TextBoxSchool']",
      "input[name*='TextBoxSchool']",
      "input[type='search']",
      "input[name*='School' i]",
      "input[id*='School' i]",
      "input[placeholder*='School' i]",
      "input[type='text']",
    ], payload.crm_school_name || "");
    if (!searchBox) {
      const evidence = await saveEvidence(page, artifactDir, "page_structure_changed");
      return {
        status: "failed",
        error_type: "page_structure_changed",
        error_message: "School search box not found.",
        ...evidence,
      };
    }
    const yearSelector = await firstMatchingLocator(page, [
      "select[name*='ComboYear']",
      "select[id*='ComboYear']",
    ]);
    if (yearSelector) {
      await yearSelector.selectOption("Trend").catch(() => {});
    }
    const chartSelector = await firstMatchingLocator(page, [
      "select[name*='ComboCharts']",
      "select[id*='ComboCharts']",
    ]);
    if (chartSelector) {
      await chartSelector.selectOption("None").catch(() => {});
    }
    await page.waitForTimeout(1200);
    const suggestion = await firstVisible(page, [
      ".ui-menu-item",
      "[role='option']",
      ".autocomplete-item",
      ".suggestion",
      "li:has-text('" + String(payload.crm_school_name || "").replace(/'/g, "\\'") + "')",
    ]);
    if (suggestion) {
      await suggestion.click().catch(() => {});
    } else {
      await searchBox.press("Enter").catch(() => {});
    }
    const compareButton = await firstVisible(page, [
      "input[value='Go' i]",
      "input[type='submit'][value*='Compare' i]",
      "button:has-text('Compare')",
      "button:has-text('Go')",
      "a:has-text('Compare')",
      "input[type='button'][value*='Compare' i]",
    ]);
    if (compareButton) {
      await compareButton.click().catch(() => {});
      await page.waitForLoadState("networkidle", { timeout: 20000 }).catch(() => {});
    }
    const trendAll = await firstVisible(page, [
      "a:has-text('Trend(all)')",
      "a:has-text('Trend (all)')",
      "button:has-text('Trend(all)')",
      "button:has-text('Trend (all)')",
      "input[value*='Trend' i]",
    ]);
    if (trendAll) {
      await trendAll.click().catch(() => {});
      await page.waitForLoadState("networkidle", { timeout: 20000 }).catch(() => {});
    }

    const postText = await page.locator("body").innerText({ timeout: 5000 }).catch(() => "");
    const postAccessProblem = detectAccessProblem(postText, 0);
    if (postAccessProblem) {
      const evidence = await saveEvidence(page, artifactDir, postAccessProblem);
      return {
        status: "failed",
        error_type: postAccessProblem,
        error_message: `Better Education returned ${postAccessProblem}.`,
        ...evidence,
      };
    }
    const tables = await extractTables(page);
    const records = parseRowsFromTables(tables, payload, page.url());
    if (!records.length) {
      const evidence = await saveEvidence(page, artifactDir, "page_structure_changed");
      return {
        status: "failed",
        error_type: "page_structure_changed",
        error_message: "Trend(all) data could not be parsed.",
        ...evidence,
      };
    }
    return {
      status: "success",
      source_url: page.url(),
      source_school_name: payload.crm_school_name || "",
      records,
    };
  } finally {
    if (browser) await browser.close();
  }
}

main()
  .then((result) => {
    fs.writeFileSync(outputPath, JSON.stringify(result, null, 2), "utf8");
  })
  .catch((error) => {
    const errorType = classifyWorkerError(error);
    const result = {
      status: "failed",
      error_type: errorType,
      error_message: error && error.stack ? error.stack : String(error),
    };
    fs.mkdirSync(path.dirname(outputPath), { recursive: true });
    fs.writeFileSync(outputPath, JSON.stringify(result, null, 2), "utf8");
    process.exit(1);
  });
